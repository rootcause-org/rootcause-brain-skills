"""Honeybadger support connector.

Force-code trigger (d): Honeybadger embeds the next page URL in the JSON body
(``{"links": {"next": "<full URL>"}, "results": […]}``) rather than an HTTP ``Link`` header.
lib.api's built-in ``link`` style reads RFC 8288 HTTP headers only, so the generic paginator can't
drive this. This connector reuses lib.api's ``_send_url`` for the link-follow path and
``_extract_items`` for the ``results`` field, but owns the while-has-next loop.

Read-only: only ever issues GETs. No writes to Honeybadger.

CLI:
    python -m lib.connectors.honeybadger faults PROJECT_ID [--query k=v ...] [--pick a,b]
    python -m lib.connectors.honeybadger deploys PROJECT_ID [--query k=v ...] [--pick a,b]
    python -m lib.connectors.honeybadger projects [--pick a,b]
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from lib import api

# Manifest: reuses lib.api client (auth, retry, timeout) but not its pagination loop.
MANIFEST = api.register(
    api.Manifest(
        key="honeybadger",
        base_url="https://app.honeybadger.io/v2",
        auth=api.Auth(strategy="basic"),
        pagination=api.Pagination(
            style="none",       # script owns the loop
            items_field="results",
            page_size=25,
        ),
        rate_limit_remaining_header="X-RateLimit-Remaining",
    )
)


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="honeybadger")


def collect_pages(
    path: str,
    *,
    query: dict[str, Any] | None = None,
    max_pages: int = 1000,
) -> dict:
    """Paginate a Honeybadger list endpoint by following ``links.next`` in the JSON body.

    Returns ``{"items": [...], "incomplete": bool, "reason": str}`` — same contract as
    ``api.Client.collect`` so callers can treat them uniformly.

    Honeybadger's response envelope:
        {"links": {"self": "…", "next": "…"}, "results": […]}

    ``links.next`` is a full URL; absent when no further pages exist. We call ``_send_url`` for
    link-follow requests so auth rides along (same approach as lib.api's link-style path).
    """
    c = _client()
    items: list = []
    incomplete = False
    reason = ""
    pages = 0
    # First request: path-relative, so retry/auth/base_url join apply.
    next_url: str | None = None
    is_first = True
    try:
        while pages < max_pages:
            if is_first:
                page = c.fetch_page(path, query=query)
                is_first = False
            else:
                # link-follow: absolute URL from body, auth must ride along.
                resp = c._send_url("GET", next_url)  # noqa: SLF001 — intentional; shares retry/auth
                body = resp.json() if hasattr(resp, "json") else resp
                # _send_url returns a raw requests.Response; parse it the same way fetch_page does.
                page = api.Page(
                    body=body,
                    items=c._extract_items(body),  # noqa: SLF001
                    next=None,
                )
            items.extend(page.items)
            pages += 1
            # Resolve next URL from the body envelope (not from HTTP headers).
            links = page.body.get("links") if isinstance(page.body, dict) else None
            next_url = (links or {}).get("next") or None
            if not next_url:
                break
        else:
            incomplete = True
            reason = f"reached max_pages={max_pages}"
    except api.ApiError as e:
        incomplete = True
        reason = f"page fetch failed after {len(items)} item(s): {e}"
    return {"items": items, "incomplete": incomplete, "reason": reason}


def _print_result(result: dict, pick_paths: str) -> None:
    """Print collected items as JSON, optionally picking fields."""
    if pick_paths:
        result = dict(result)
        result["items"] = [api.pick(it, pick_paths) for it in result["items"]]
    print(json.dumps(result, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the Honeybadger connector."""
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.honeybadger")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # faults: list errors for a project
    p_faults = sub.add_parser("faults", help="list error faults for a project")
    p_faults.add_argument("project_id", help="Honeybadger project numeric ID")
    p_faults.add_argument("--query", action="append", default=[], metavar="K=V",
                          help="query param (repeatable); e.g. q=is:unresolved order=recent")
    p_faults.add_argument("--pick", default="", help="comma-separated dotted paths to select")

    # deploys: list deployments for a project
    p_deploys = sub.add_parser("deploys", help="list deployments for a project")
    p_deploys.add_argument("project_id", help="Honeybadger project numeric ID")
    p_deploys.add_argument("--query", action="append", default=[], metavar="K=V",
                           help="query param (repeatable); e.g. environment=production")
    p_deploys.add_argument("--pick", default="", help="comma-separated dotted paths to select")

    # projects: list all projects (to discover project IDs)
    p_projects = sub.add_parser("projects", help="list all projects in the account")
    p_projects.add_argument("--pick", default="", help="comma-separated dotted paths to select")

    args = parser.parse_args(argv)
    query = dict(kv.split("=", 1) for kv in getattr(args, "query", []) if "=" in kv)

    if args.cmd == "faults":
        result = collect_pages(f"projects/{args.project_id}/faults", query=query or None)
        _print_result(result, args.pick)
    elif args.cmd == "deploys":
        result = collect_pages(f"projects/{args.project_id}/deploys", query=query or None)
        _print_result(result, args.pick)
    elif args.cmd == "projects":
        result = collect_pages("projects", query=None)
        _print_result(result, args.pick)
    else:
        parser.error("unknown command")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
