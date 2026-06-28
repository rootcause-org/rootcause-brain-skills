"""Datadog support connector — script connector for exotic dual-header auth.

Force-code trigger (c): exotic auth. Datadog requires two separate secrets on every request:
  DD-API-KEY  (org authentication)
  DD-APPLICATION-KEY  (grants read access to org data)

lib.api injects a single credential per RC_CONN_* env var, so both keys are stored
colon-separated ("api_key:app_key") and this script splits them into the two required headers.
No single lib.api auth strategy can express a two-header credential.

All reads are read-only (GET only). The script pre-selects support-relevant fields so raw
Datadog monitor/incident JSON (which can be hundreds of fields) never floods model context.

CLI:
    python -m lib.connectors.datadog monitors [--query name=X] [--id ID]
    python -m lib.connectors.datadog incidents [--query state=active]
    python -m lib.connectors.datadog events [--query filter[from]=now-1h]
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from lib import api, oauth

# Default US region base; overridden by DATADOG_BASE_URL env var for EU/AP regions.
_DEFAULT_BASE = "https://api.datadoghq.com"

# Manifest row — registered so `python -m lib.api get datadog ...` also works (the auth strategy
# is "none" because the script injects both headers itself via _client()).
MANIFEST = api.register(
    api.Manifest(
        key="datadog",
        base_url=os.environ.get("DATADOG_BASE_URL") or _DEFAULT_BASE,
        auth=api.Auth(strategy="none"),  # dual-header auth handled by _client()
        pagination=api.Pagination(
            style="offset",
            offset_param="page[offset]",
            limit_param="page[limit]",
            items_field="data",
            page_size=50,
        ),
        rate_limit_remaining_header="X-RateLimit-Remaining",
    )
)


def _parse_credential() -> tuple[str, str]:
    """Split RC_CONN_DATADOG ("api_key:app_key") into (api_key, app_key).

    The colon separator mirrors lib.api's ``basic`` strategy ("user:pass"), keeping the
    credential opaque to argv, logs, and model context — only the env var sees it.
    Raises RuntimeError when the variable is absent or malformed.
    """
    raw = oauth.token("datadog")  # raises if RC_CONN_DATADOG is not set
    if ":" not in raw:
        raise RuntimeError(
            "RC_CONN_DATADOG must be 'api_key:app_key' (colon-separated); "
            "got a value with no colon"
        )
    api_key, _, app_key = raw.partition(":")
    if not api_key or not app_key:
        raise RuntimeError(
            "RC_CONN_DATADOG is malformed: both api_key and app_key must be non-empty"
        )
    return api_key, app_key


def _client() -> tuple[api.Client, dict[str, str]]:
    """Build a lib.api Client + the two Datadog auth headers.

    Returns (client, auth_headers) so callers pass auth_headers to every request.
    The client uses auth strategy "none" (credential is never placed automatically);
    callers inject both headers explicitly so lib.api's single-credential model is bypassed.
    """
    api_key, app_key = _parse_credential()
    base = os.environ.get("DATADOG_BASE_URL") or _DEFAULT_BASE
    # Rebuild manifest with current base URL (respects runtime env changes in tests).
    mani = api.Manifest(
        key="datadog",
        base_url=base,
        auth=api.Auth(strategy="none"),
        pagination=api.Pagination(
            style="offset",
            offset_param="page[offset]",
            limit_param="page[limit]",
            items_field="data",
            page_size=50,
        ),
        rate_limit_remaining_header="X-RateLimit-Remaining",
    )
    c = api.Client(manifest=mani, credential="")
    auth_headers: dict[str, str] = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
    }
    return c, auth_headers


# ---------------------------------------------------------------------------
# Reads (each pre-selects the support-relevant fields)
# ---------------------------------------------------------------------------

# Fields from a v1 monitor object that are useful for support grounding.
_MONITOR_PICK = "id,name,type,query,overall_state,message,tags,created,modified"

# Fields from a v2 incident "data" item.
_INCIDENT_PICK = (
    "id,"
    "attributes.title,"
    "attributes.status,"
    "attributes.severity,"
    "attributes.created,"
    "attributes.modified,"
    "attributes.public_id,"
    "attributes.state,"
    "attributes.commander_user"
)

# Fields from a v2 event "data" item.
_EVENT_PICK = (
    "id,"
    "attributes.title,"
    "attributes.message,"
    "attributes.timestamp,"
    "attributes.status,"
    "attributes.priority,"
    "attributes.source_type_name,"
    "attributes.tags"
)


def get_monitors(*, query: dict[str, Any] | None = None) -> list[dict]:
    """GET /api/v1/monitor — returns all monitors, pre-selected to support fields.

    v1 monitors return a bare JSON array (no pagination envelope). For most orgs the full list
    fits in one call; page via ``count``/``start`` only if the org has thousands of monitors.
    """
    c, h = _client()
    body = c.get("/api/v1/monitor", query=query, headers=h)
    if isinstance(body, list):
        return [api.pick(m, _MONITOR_PICK) for m in body]
    # Some accounts return {"monitors": [...]} — unwrap defensively.
    items = body.get("monitors", body) if isinstance(body, dict) else []
    return [api.pick(m, _MONITOR_PICK) for m in (items if isinstance(items, list) else [])]


def get_incidents(*, query: dict[str, Any] | None = None, max_items: int = 100) -> dict:
    """GET /api/v2/incidents — paginate with offset, pre-select support fields.

    Returns ``{"items": [...], "incomplete": bool, "reason": str}``.
    """
    c, h = _client()
    result = c.collect("/api/v2/incidents", query=query, headers=h, max_items=max_items)
    result["items"] = [api.pick(it, _INCIDENT_PICK) for it in result["items"]]
    return result


def get_events(*, query: dict[str, Any] | None = None, max_items: int = 100) -> dict:
    """GET /api/v2/events — paginate with offset, pre-select support fields.

    Returns ``{"items": [...], "incomplete": bool, "reason": str}``.
    Pass ``filter[from]``/``filter[to]`` to narrow the time window (e.g. ``filter[from]=now-1h``).
    """
    c, h = _client()
    result = c.collect("/api/v2/events", query=query, headers=h, max_items=max_items)
    result["items"] = [api.pick(it, _EVENT_PICK) for it in result["items"]]
    return result


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _monitors_to_markdown(monitors: list[dict]) -> str:
    if not monitors:
        return "# Datadog monitors\n\nNo monitors found matching the query."
    lines = [f"# Datadog monitors ({len(monitors)} results)\n"]
    for m in monitors:
        state = m.get("overall_state") or "unknown"
        name = m.get("name") or m.get("id") or "—"
        mtype = m.get("type") or ""
        mid = m.get("id", "")
        lines.append(f"## {name}")
        lines.append(f"- ID: `{mid}` | Type: {mtype} | State: **{state}**")
        if m.get("tags"):
            lines.append(f"- Tags: {', '.join(m['tags'])}")
        if m.get("message"):
            # Trim long messages to avoid flooding context.
            msg = str(m["message"])
            lines.append(f"- Message: {msg[:300]}{'…' if len(msg) > 300 else ''}")
        lines.append("")
    return "\n".join(lines)


def _incidents_to_markdown(result: dict) -> str:
    items = result.get("items") or []
    if not items:
        return "# Datadog incidents\n\nNo incidents found."
    lines = [f"# Datadog incidents ({len(items)} results)\n"]
    if result.get("incomplete"):
        lines.append(f"> **Note:** results may be incomplete — {result.get('reason')}\n")
    for it in items:
        attrs = it.get("attributes") or {}
        title = attrs.get("title") or it.get("id") or "—"
        status = attrs.get("status") or attrs.get("state") or "unknown"
        severity = attrs.get("severity") or ""
        pub_id = attrs.get("public_id") or it.get("id") or ""
        lines.append(f"## {title}")
        lines.append(f"- ID: `{pub_id}` | Status: **{status}**" + (f" | Severity: {severity}" if severity else ""))
        if attrs.get("created"):
            lines.append(f"- Created: {attrs['created']}")
        if attrs.get("modified"):
            lines.append(f"- Modified: {attrs['modified']}")
        lines.append("")
    return "\n".join(lines)


def _events_to_markdown(result: dict) -> str:
    items = result.get("items") or []
    if not items:
        return "# Datadog events\n\nNo events found."
    lines = [f"# Datadog events ({len(items)} results)\n"]
    if result.get("incomplete"):
        lines.append(f"> **Note:** results may be incomplete — {result.get('reason')}\n")
    for it in items:
        attrs = it.get("attributes") or {}
        title = attrs.get("title") or attrs.get("message") or "—"
        status = attrs.get("status") or attrs.get("priority") or ""
        source = attrs.get("source_type_name") or ""
        ts = attrs.get("timestamp") or ""
        lines.append(f"## {title[:80]}")
        if ts or status or source:
            meta = " | ".join(x for x in [ts, status, source] if x)
            lines.append(f"- {meta}")
        if attrs.get("tags"):
            lines.append(f"- Tags: {', '.join(attrs['tags'][:10])}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import json

    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.datadog",
        description="Read Datadog monitors / incidents / events for support grounding.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # monitors sub-command
    mon = sub.add_parser("monitors", help="list monitors, optionally filtered")
    mon.add_argument("--id", dest="monitor_id", default="", help="fetch a single monitor by ID")
    mon.add_argument("--query", action="append", default=[], metavar="K=V",
                     help="query param (repeatable), e.g. --query name=my-svc --query tags=env:prod")
    mon.add_argument("--json", dest="as_json", action="store_true", help="print raw JSON instead of markdown")

    # incidents sub-command
    inc = sub.add_parser("incidents", help="list incidents (v2, paginated)")
    inc.add_argument("--query", action="append", default=[], metavar="K=V",
                     help="query param, e.g. --query state=active")
    inc.add_argument("--max-items", type=int, default=100)
    inc.add_argument("--json", dest="as_json", action="store_true")

    # events sub-command
    ev = sub.add_parser("events", help="list events (v2, paginated)")
    ev.add_argument("--query", action="append", default=[], metavar="K=V",
                    help="query param, e.g. --query 'filter[from]=now-1h'")
    ev.add_argument("--max-items", type=int, default=100)
    ev.add_argument("--json", dest="as_json", action="store_true")

    args = parser.parse_args(argv)
    q = dict(kv.split("=", 1) for kv in getattr(args, "query", []) if "=" in kv)

    if args.cmd == "monitors":
        if args.monitor_id:
            c, h = _client()
            body = c.get(f"/api/v1/monitor/{args.monitor_id}", headers=h)
            if args.as_json:
                print(json.dumps(api.pick(body, _MONITOR_PICK), indent=2, default=str))
            else:
                print(_monitors_to_markdown([api.pick(body, _MONITOR_PICK)]))
        else:
            monitors = get_monitors(query=q or None)
            if args.as_json:
                print(json.dumps(monitors, indent=2, default=str))
            else:
                print(_monitors_to_markdown(monitors))

    elif args.cmd == "incidents":
        result = get_incidents(query=q or None, max_items=args.max_items)
        if args.as_json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(_incidents_to_markdown(result))

    elif args.cmd == "events":
        result = get_events(query=q or None, max_items=args.max_items)
        if args.as_json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(_events_to_markdown(result))

    else:
        parser.error("unknown command")
        return 2

    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
