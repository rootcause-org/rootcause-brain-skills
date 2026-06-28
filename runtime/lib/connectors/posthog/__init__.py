"""PostHog support connector.

Force-code trigger (d): PostHog embeds the next-page URL in the JSON body
(``{"next": "<full URL>", "results": [...]}``) rather than an HTTP ``Link`` header.
lib.api's built-in ``link`` style reads RFC 8288 HTTP headers only, so the generic paginator
can't drive this. This connector reuses lib.api's ``_send_url`` for link-follow requests
(so auth rides along) and ``_extract_items`` for the ``results`` field, but owns the
while-has-next loop — same approach as lib.connectors.honeybadger.

Read-only: only ever issues GETs. No writes to PostHog.

CLI:
    python -m lib.connectors.posthog persons PROJECT_ID [--query k=v ...] [--pick a,b]
    python -m lib.connectors.posthog feature-flags PROJECT_ID [--query k=v ...] [--pick a,b]
    python -m lib.connectors.posthog session-recordings PROJECT_ID [--query k=v ...] [--pick a,b]
    python -m lib.connectors.posthog events PROJECT_ID [--query k=v ...] [--pick a,b]
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from lib import api

# Register the manifest so `python -m lib.api get posthog <path>` works for single-item GETs
# (e.g. fetching one person by id) even when callers don't import this module directly.
MANIFEST = api.register(
    api.Manifest(
        key="posthog",
        base_url="https://us.posthog.com",
        auth=api.Auth(strategy="bearer"),
        pagination=api.Pagination(
            style="none",       # script owns the while-has-next loop
            items_field="results",
            page_size=100,
        ),
        rate_limit_remaining_header="",  # 429 + Retry-After handled by lib.api; no count header
    )
)


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="posthog")


def collect_pages(
    path: str,
    *,
    query: dict[str, Any] | None = None,
    max_pages: int = 1000,
) -> dict:
    """Paginate a PostHog list endpoint by following ``next`` in the JSON body.

    Returns ``{"items": [...], "incomplete": bool, "reason": str}`` — same contract as
    ``api.Client.collect`` so callers can treat them uniformly.

    PostHog's response envelope:
        {"count": N, "next": "<full URL or null>", "previous": "…", "results": [...]}

    ``next`` is a full URL when a further page exists, or null/absent when done. We call
    ``_send_url`` for link-follow requests so auth (Bearer token) rides along — identical to
    lib.api's internal link-header follow path.
    """
    c = _client()
    items: list = []
    incomplete = False
    reason = ""
    pages = 0
    next_url: str | None = None
    is_first = True
    try:
        while pages < max_pages:
            if is_first:
                page = c.fetch_page(path, query=query)
                is_first = False
            else:
                # Follow the absolute next URL verbatim; _send_url applies auth + retry.
                resp = c._send_url("GET", next_url)  # noqa: SLF001 — intentional; shares auth/retry
                body = _parse_body(resp)
                page = api.Page(
                    body=body,
                    items=c._extract_items(body),  # noqa: SLF001
                    next=None,
                )
            items.extend(page.items)
            pages += 1
            # next URL lives directly under "next" in the PostHog envelope.
            next_url = _next_from_body(page.body)
            if not next_url:
                break
        else:
            incomplete = True
            reason = f"reached max_pages={max_pages}"
    except api.ApiError as e:
        incomplete = True
        reason = f"page fetch failed after {len(items)} item(s): {e}"
    return {"items": items, "incomplete": incomplete, "reason": reason}


def _next_from_body(body: Any) -> str | None:
    """Extract the next-page URL from a PostHog paginated envelope."""
    if not isinstance(body, dict):
        return None
    nxt = body.get("next")
    return str(nxt) if nxt else None


def _parse_body(resp: Any) -> Any:
    """Parse a raw requests.Response returned by _send_url."""
    if hasattr(resp, "json"):
        try:
            return resp.json()
        except ValueError:
            pass
    return resp


def _print_result(result: dict, pick_paths: str) -> None:
    if pick_paths:
        result = dict(result)
        result["items"] = [api.pick(it, pick_paths) for it in result["items"]]
    print(json.dumps(result, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the PostHog connector."""
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.posthog")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add_list_cmd(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("project_id", help="numeric PostHog project ID")
        p.add_argument("--query", action="append", default=[], metavar="K=V",
                       help="query param (repeatable); e.g. search=user@example.com")
        p.add_argument("--pick", default="", help="comma-separated dotted paths to select")
        return p

    _add_list_cmd("persons", "list persons by search/email/distinct_id")
    _add_list_cmd("feature-flags", "list all feature flags with rollout config")
    _add_list_cmd("session-recordings", "list session recordings, optionally filtered by person")
    _add_list_cmd("events", "list recent events by distinct_id or event name")

    args = parser.parse_args(argv)
    query = dict(kv.split("=", 1) for kv in args.query if "=" in kv)
    pid = args.project_id

    path_map = {
        "persons": f"api/projects/{pid}/persons",
        "feature-flags": f"api/projects/{pid}/feature_flags",
        "session-recordings": f"api/projects/{pid}/session_recordings",
        "events": f"api/projects/{pid}/events",
    }
    path = path_map.get(args.cmd)
    if path is None:
        parser.error(f"unknown command: {args.cmd}")

    result = collect_pages(path, query=query or None)
    _print_result(result, args.pick)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
