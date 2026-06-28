"""Zendesk support connector — grounding reads for tickets, users, organizations, comments.

Force-code trigger (d): Zendesk list responses embed items under a resource-type key that varies
per endpoint ("tickets", "users", "organizations", "groups", "comments", …) — the generic
``items_field`` in the manifest cannot be fixed to a single value. This script extracts items
dynamically from the response envelope by inspecting the first list-valued key (excluding the
pagination envelope keys ``meta`` and ``links``).

Auth: basic (``{email}/token:{api_token}`` injected as ``RC_CONN_ZENDESK``). lib.api basic
strategy splits on the first ":" giving user=``{email}/token`` and password=``{api_token}``,
which is the exact format Zendesk requires for API token authentication.

Pagination: Zendesk cursor pagination — server returns ``meta.after_cursor`` / ``meta.has_more``;
the next cursor is passed as ``page[after]`` query param. Hard-gated by ``meta.has_more``.

Per-account subdomain: the manifest base_url uses a ``{subdomain}`` placeholder. Callers must
supply an absolute URL or pass ``--base-url`` so the correct workspace is targeted.

Read-only: only GETs are issued. We never write to customer Zendesk workspaces.

CLI:
    python -m lib.connectors.zendesk list tickets --base-url https://acme.zendesk.com/api/v2
    python -m lib.connectors.zendesk list users --base-url https://acme.zendesk.com/api/v2
    python -m lib.connectors.zendesk get ticket 12345 --base-url https://acme.zendesk.com/api/v2
    python -m lib.connectors.zendesk search "type:ticket status:open" --base-url ...
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from lib import api

# ---------------------------------------------------------------------------
# Manifest registration — authoritative row for both lib.api CLI and this script.
# ---------------------------------------------------------------------------

# Load from the co-located manifest.yaml so the canonical source is the YAML, not duplicated here.
_MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.yaml"
_MANIFEST = api._parse_manifest_file(_MANIFEST_PATH)
api.register(_MANIFEST)
MANIFEST = _MANIFEST

# Zendesk list responses embed items under the resource type name.
# Maps the endpoint path segment to the items envelope key.
_RESOURCE_ITEMS_KEY: dict[str, str] = {
    "tickets": "tickets",
    "users": "users",
    "organizations": "organizations",
    "groups": "groups",
    "comments": "comments",
    "requests": "requests",
    "audits": "audits",
    "satisfaction_ratings": "satisfaction_ratings",
    "tags": "tags",
    "results": "results",  # search endpoint
}

# Pagination envelope keys to skip when dynamically detecting the items list.
_PAGINATION_META_KEYS = frozenset({"meta", "links", "next_page", "previous_page", "count"})

# Support-relevant fields for each resource type (dotted paths for api.pick).
_PICK_FIELDS: dict[str, str] = {
    "tickets": (
        "id,url,created_at,updated_at,subject,description,status,priority,type,"
        "requester_id,assignee_id,organization_id,group_id,tags"
    ),
    "users": (
        "id,url,name,email,created_at,updated_at,role,organization_id,"
        "phone,time_zone,locale,suspended,verified"
    ),
    "organizations": (
        "id,url,name,created_at,updated_at,domain_names,tags,notes"
    ),
    "groups": (
        "id,url,name,created_at,updated_at,deleted"
    ),
    "comments": (
        "id,type,body,html_body,created_at,public,author_id"
    ),
    "results": (
        "id,url,result_type,subject,status,created_at,updated_at"
    ),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _client(base_url: str | None = None) -> api.Client:
    """Build a lib.api Client for Zendesk, optionally overriding the base_url for per-account use."""
    if base_url:
        manifest = api.Manifest(
            key=MANIFEST.key,
            base_url=base_url.rstrip("/"),
            auth=MANIFEST.auth,
            pagination=MANIFEST.pagination,
            rate_limit_remaining_header=MANIFEST.rate_limit_remaining_header,
            default_headers=MANIFEST.default_headers,
        )
    else:
        manifest = MANIFEST
    return api.client(manifest, token_key="zendesk")


def _resource_key(path: str) -> str:
    """Derive the Zendesk envelope items key from the endpoint path segment.

    Zendesk embeds items under the resource type name, e.g. ``/tickets`` → ``tickets``,
    ``/search`` → ``results``. For unknown paths, falls back to the first path segment.
    """
    segment = path.lstrip("/").split("/")[0].split("?")[0]
    # /search → items are under "results"
    if segment == "search":
        return "results"
    return _RESOURCE_ITEMS_KEY.get(segment, segment)


def _items_from_body(body: Any, resource_key: str) -> list:
    """Extract the items list from a Zendesk list response envelope.

    Tries the declared resource_key first; if absent or not a list, walks the body to find the
    first list-valued key that is not a pagination meta-key.
    """
    if isinstance(body, dict):
        candidate = body.get(resource_key)
        if isinstance(candidate, list):
            return list(candidate)
        # Fallback: first list-valued key in the envelope that isn't a pagination meta-key.
        for k, v in body.items():
            if k not in _PAGINATION_META_KEYS and isinstance(v, list):
                return list(v)
    if isinstance(body, list):
        return list(body)
    return []


def _next_cursor(body: Any) -> str | None:
    """Extract the next cursor from ``meta.after_cursor`` gated by ``meta.has_more``; None if last page."""
    if not isinstance(body, dict):
        return None
    meta = body.get("meta")
    if not isinstance(meta, dict):
        return None
    if not meta.get("has_more"):
        return None
    cursor = meta.get("after_cursor")
    return str(cursor) if cursor else None


def list_resource(
    path: str,
    *,
    base_url: str | None = None,
    query: dict[str, Any] | None = None,
    max_pages: int = 20,
    page_size: int = 100,
) -> dict:
    """Auto-page a Zendesk list endpoint, dynamically extracting items from each page envelope.

    Returns ``{"items": [...], "incomplete": bool, "reason": str}`` — same shape as
    ``api.Client.collect`` so callers are interchangeable.
    """
    c = _client(base_url)
    resource_key = _resource_key(path)
    q = dict(query or {}, **{"page[size]": page_size})
    items: list = []
    pages_fetched = 0
    incomplete = False
    reason = ""

    try:
        while pages_fetched < max_pages:
            page = c.fetch_page(path, query=q)
            page_items = _items_from_body(page.body, resource_key)
            items.extend(page_items)
            pages_fetched += 1
            cursor = _next_cursor(page.body)
            if cursor is None:
                break
            q = dict(q, **{"page[after]": cursor})
        else:
            incomplete = True
            reason = f"reached max_pages={max_pages}"
    except api.ApiError as e:
        incomplete = True
        reason = f"page fetch failed after {len(items)} item(s): {e}"

    return {"items": items, "incomplete": incomplete, "reason": reason}


def get_resource(path: str, *, base_url: str | None = None) -> Any:
    """GET a single resource by path. Raises ApiError on non-2xx."""
    return _client(base_url).get(path)


def search(query_str: str, *, base_url: str | None = None, max_pages: int = 5) -> dict:
    """Search across Zendesk resources using the unified search API.

    ``query_str`` is a Zendesk search expression, e.g. ``type:ticket status:open``.
    Returns ``{"items": [...], "incomplete": bool, "reason": str}``.
    """
    return list_resource(
        "/search",
        base_url=base_url,
        query={"query": query_str},
        max_pages=max_pages,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.zendesk",
        description="Read-only Zendesk grounding: tickets, users, organizations, comments, search.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        metavar="URL",
        help="Zendesk API base URL, e.g. https://acme.zendesk.com/api/v2 (required for per-account use)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # -- list subcommand --
    ls = sub.add_parser("list", help="list a resource (auto-paginated, pre-selected fields)")
    ls.add_argument(
        "resource",
        choices=["tickets", "users", "organizations", "groups"],
        help="resource type to list",
    )
    ls.add_argument("--query", action="append", default=[], metavar="K=V",
                    help="query param (repeatable, e.g. --query status=open)")
    ls.add_argument("--max-pages", type=int, default=10,
                    help="hard page cap (default 10)")
    ls.add_argument("--page-size", type=int, default=100,
                    help="items per page (default 100, max 100)")
    ls.add_argument("--no-pick", action="store_true",
                    help="return full objects instead of pre-selected support fields")

    # -- get subcommand --
    gt = sub.add_parser("get", help="fetch one resource by id")
    gt.add_argument("resource",
                    choices=["ticket", "user", "organization", "group"],
                    help="resource type")
    gt.add_argument("id", help="resource id")
    gt.add_argument("--no-pick", action="store_true",
                    help="return full object instead of pre-selected support fields")

    # -- comments subcommand (ticket comments are a nested resource) --
    cm = sub.add_parser("comments", help="list comments on a ticket")
    cm.add_argument("ticket_id", help="ticket id")
    cm.add_argument("--max-pages", type=int, default=5)
    cm.add_argument("--no-pick", action="store_true")

    # -- search subcommand --
    sr = sub.add_parser("search", help="search across Zendesk resources")
    sr.add_argument("query_string", help="Zendesk search expression, e.g. 'type:ticket status:open'")
    sr.add_argument("--max-pages", type=int, default=5)
    sr.add_argument("--no-pick", action="store_true")

    args = parser.parse_args(argv)
    base_url = args.base_url

    if args.cmd == "list":
        path = f"/{args.resource}"
        query = dict(kv.split("=", 1) for kv in args.query if "=" in kv)
        result = list_resource(
            path, base_url=base_url, query=query,
            max_pages=args.max_pages, page_size=args.page_size,
        )
        if not args.no_pick and args.resource in _PICK_FIELDS:
            result["items"] = [api.pick(it, _PICK_FIELDS[args.resource]) for it in result["items"]]
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.cmd == "get":
        # Map singular → plural path (ticket→tickets, user→users, etc.).
        plural = args.resource + "s"  # ticket→tickets, group→groups, etc.
        path = f"/{plural}/{args.id}"
        body = get_resource(path, base_url=base_url)
        pick_key = plural
        if not args.no_pick and pick_key in _PICK_FIELDS:
            # Single-resource responses wrap the object: {"ticket": {...}} — unwrap first.
            singular = args.resource
            obj = body.get(singular, body) if isinstance(body, dict) else body
            obj = api.pick(obj, _PICK_FIELDS[pick_key])
            print(json.dumps(obj, indent=2, default=str))
        else:
            print(json.dumps(body, indent=2, default=str))
        return 0

    if args.cmd == "comments":
        path = f"/tickets/{args.ticket_id}/comments"
        result = list_resource(path, base_url=base_url, max_pages=args.max_pages)
        if not args.no_pick:
            result["items"] = [api.pick(it, _PICK_FIELDS["comments"]) for it in result["items"]]
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.cmd == "search":
        result = search(args.query_string, base_url=base_url, max_pages=args.max_pages)
        if not args.no_pick:
            result["items"] = [api.pick(it, _PICK_FIELDS["results"]) for it in result["items"]]
        print(json.dumps(result, indent=2, default=str))
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
