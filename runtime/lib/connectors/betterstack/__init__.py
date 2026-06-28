"""Better Stack Uptime connector — script connector over ``lib.api``.

Force-code trigger (d): Better Stack paginates via ``pagination.next`` — a full absolute URL (or
null) embedded in the JSON response body. lib.api's ``link`` style reads HTTP ``Link:`` response
headers (Better Stack sends none); ``cursor`` style would send the full URL as a query parameter
(wrong). The connector drives the while-has-more loop manually via ``_betterstack_pages()``,
delegating every HTTP concern (bearer auth, retry/backoff, rate-limit, timeouts) to ``lib.api``.

Support use-case: uptime status of monitors + open incidents, answering "is there an ongoing
outage / which monitors are down / when did the last incident start". The connector renders a
concise markdown block combining monitor statuses and open incidents.

All endpoints are JSON:API shaped: ``{ "data": [...], "pagination": { "next": url|null } }``.
Items carry support fields under ``attributes``; ``id`` and ``type`` are siblings.

CLI:
    python -m lib.connectors.betterstack monitors
    python -m lib.connectors.betterstack incidents
    python -m lib.connectors.betterstack incidents --monitor-id <id>
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterator

import yaml

from lib import api

# ---------------------------------------------------------------------------
# Manifest — loaded from manifest.yaml so the catalog row stays the single source of truth.
# register() makes ``python -m lib.api get betterstack …`` work for single-page reads.
# ---------------------------------------------------------------------------

_MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")


def _load_manifest() -> api.Manifest:
    raw = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return api._manifest_from_dict(raw)


MANIFEST = api.register(_load_manifest())


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="betterstack")


# ---------------------------------------------------------------------------
# Pagination — body-embedded ``pagination.next`` (force-code trigger d)
# ---------------------------------------------------------------------------

_DEFAULT_MAX_ITEMS = 500


def _pagination_next(body: Any) -> str | None:
    """Extract ``pagination.next`` from a Better Stack list response body.

    Returns the absolute next-page URL, or None when exhausted (null in the body).
    """
    if not isinstance(body, dict):
        return None
    pag = body.get("pagination")
    if not isinstance(pag, dict):
        return None
    nxt = pag.get("next")
    return str(nxt) if nxt else None


def _betterstack_pages(path: str, query: dict | None = None) -> Iterator[list[dict]]:
    """Yield batches of items from a Better Stack list endpoint, following ``pagination.next``.

    Uses ``lib.api`` for all HTTP (bearer auth, retry/backoff, rate-limit, timeouts). Items live
    under ``data`` in every list response. Pagination follows the absolute next URL verbatim via
    ``Client._send_url`` so auth rides every continuation request.
    """
    c = _client()

    # First page via the normal path so base query params are applied.
    page = c.fetch_page(path, query=query or {})
    yield page.items

    # Subsequent pages: follow pagination.next as an absolute URL (auth injected by _send_url).
    while True:
        next_url = _pagination_next(page.body)
        if not next_url:
            break
        resp = c._send_url("GET", next_url)
        body = api._parse_json(resp)
        items = body.get("data") or []
        page = api.Page(body=body, items=items, next=_pagination_next(body))
        yield items


def _collect(path: str, query: dict | None = None, *, max_items: int = _DEFAULT_MAX_ITEMS) -> list[dict]:
    """Collect all items from a Better Stack list endpoint up to ``max_items``."""
    out: list[dict] = []
    for batch in _betterstack_pages(path, query):
        out.extend(batch)
        if len(out) >= max_items:
            break
    return out[:max_items]


# ---------------------------------------------------------------------------
# Field pre-selection (JSON:API: attrs live under item.attributes; id is a sibling)
# ---------------------------------------------------------------------------


def _attrs(item: dict) -> dict:
    """JSON:API: support attributes live under ``attributes``; ``id`` and ``type`` are siblings."""
    return item.get("attributes") or {}


def _pick_monitor(item: dict) -> dict:
    a = _attrs(item)
    return {
        "id": item.get("id"),
        "name": a.get("pronounceable_name"),
        "url": a.get("url"),
        "monitor_type": a.get("monitor_type"),
        "status": a.get("status"),
        "last_checked_at": a.get("last_checked_at"),
        "check_frequency": a.get("check_frequency"),
        "paused_at": a.get("paused_at"),
    }


def _pick_incident(item: dict) -> dict:
    a = _attrs(item)
    return {
        "id": item.get("id"),
        "name": a.get("name"),
        "url": a.get("url"),
        "cause": a.get("cause"),
        "status": a.get("status"),
        "started_at": a.get("started_at"),
        "acknowledged_at": a.get("acknowledged_at"),
        "resolved_at": a.get("resolved_at"),
        "team_name": a.get("team_name"),
    }


# ---------------------------------------------------------------------------
# Support data fetches
# ---------------------------------------------------------------------------


def get_monitors(*, max_items: int = _DEFAULT_MAX_ITEMS) -> list[dict]:
    """Return all monitors, pre-selected to support-relevant fields."""
    return [_pick_monitor(item) for item in _collect("monitors", max_items=max_items)]


def get_incidents(
    monitor_id: str | None = None,
    *,
    max_items: int = _DEFAULT_MAX_ITEMS,
) -> list[dict]:
    """Return incidents pre-selected to support-relevant fields.

    When ``monitor_id`` is supplied, fetches incidents for that specific monitor via the nested
    path ``monitors/{id}/incidents`` (all historical + ongoing). When omitted, fetches the
    global incidents list (primarily open/ongoing incidents).
    """
    if monitor_id:
        path = f"monitors/{monitor_id}/incidents"
    else:
        path = "incidents"
    return [_pick_incident(item) for item in _collect(path, max_items=max_items)]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def monitors_to_markdown(monitors: list[dict]) -> str:
    """Render a monitor list as concise grounding markdown."""
    if not monitors:
        return "# Better Stack Monitors\n_(no monitors configured)_"
    lines = [f"# Better Stack Monitors ({len(monitors)})"]
    for m in monitors:
        status = (m.get("status") or "unknown").upper()
        name = m.get("name") or m.get("url") or m.get("id") or "?"
        url = m.get("url") or ""
        paused = " (paused)" if m.get("paused_at") else ""
        freq = m.get("check_frequency")
        freq_note = f" every {freq}s" if freq else ""
        lines.append(f"- **{status}**{paused} — {name}{freq_note}")
        if url and url != name:
            lines.append(f"  {url}")
        if m.get("last_checked_at"):
            lines.append(f"  last checked: {m['last_checked_at']}")
    return "\n".join(lines)


def incidents_to_markdown(incidents: list[dict], *, monitor_id: str | None = None) -> str:
    """Render an incident list as concise grounding markdown."""
    scope = f" for monitor {monitor_id}" if monitor_id else ""
    if not incidents:
        return f"# Better Stack Incidents{scope}\n_(no incidents found)_"
    lines = [f"# Better Stack Incidents{scope} ({len(incidents)})"]
    for inc in incidents:
        status = (inc.get("status") or "unknown")
        name = inc.get("name") or inc.get("url") or inc.get("id") or "?"
        cause = inc.get("cause") or ""
        started = (inc.get("started_at") or "?")[:19].replace("T", " ")
        resolved = inc.get("resolved_at")
        resolved_note = f" → resolved {resolved[:19].replace('T', ' ')}" if resolved else " → **UNRESOLVED**"
        lines.append(f"- [{status}] {name} — started {started}{resolved_note}")
        if cause:
            lines.append(f"  cause: {cause}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.betterstack",
        description="Better Stack Uptime connector — concise grounding for support runs.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("monitors", help="list all monitors with their current status")

    inc_p = sub.add_parser("incidents", help="list incidents (open by default; all for a specific monitor)")
    inc_p.add_argument("--monitor-id", default=None, help="filter incidents to a specific monitor id")

    args = parser.parse_args(argv)

    if args.cmd == "monitors":
        mons = get_monitors()
        print(monitors_to_markdown(mons))
        return 0

    if args.cmd == "incidents":
        incs = get_incidents(monitor_id=args.monitor_id)
        print(incidents_to_markdown(incs, monitor_id=args.monitor_id))
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
