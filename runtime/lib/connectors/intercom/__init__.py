"""Intercom support connector — grounding reads for conversations, contacts, companies, articles.

Force-code trigger (d): Intercom list responses embed items under a resource-type key that varies
per endpoint ("conversations", "contacts", "companies", "articles") — the generic ``items_field``
in the manifest cannot be fixed to a single value. This script extracts items dynamically by
inspecting the response envelope for the first list-valued key (aside from the pagination envelope).

Auth: bearer (access token or OAuth token injected as ``RC_CONN_INTERCOM``).
Pagination: cursor style — server returns ``pages.next.starting_after``; absent means last page.
Required header: ``Intercom-Version: 2.11`` (declared in manifest default_headers).

Read-only: only GETs are issued. We never write to customer Intercom workspaces.

CLI:
    python -m lib.connectors.intercom list conversations [--query k=v] [--max-pages N]
    python -m lib.connectors.intercom list contacts [--query k=v] [--max-pages N]
    python -m lib.connectors.intercom list companies [--query k=v] [--max-pages N]
    python -m lib.connectors.intercom list articles [--query k=v] [--max-pages N]
    python -m lib.connectors.intercom get conversation <id>
    python -m lib.connectors.intercom get contact <id>
    python -m lib.connectors.intercom get company <id>
    python -m lib.connectors.intercom get article <id>
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

# Intercom list responses use the resource type as the items key. Map the URL-path prefix to the
# envelope key so _extract_intercom_items knows where to look without hardcoding per-call.
_RESOURCE_ITEMS_KEY: dict[str, str] = {
    "conversations": "conversations",
    "contacts": "contacts",
    "companies": "companies",
    "articles": "articles",
    "tags": "tags",
    "teams": "teams",
    "admins": "admins",
    "segments": "segments",
}

# Support-relevant fields for each resource type. Dotted paths fed to api.pick().
_PICK_FIELDS: dict[str, str] = {
    "conversations": (
        "id,created_at,updated_at,state,read,waiting_since,"
        "source.type,source.subject,source.body,"
        "contacts.contacts.*.id,"
        "assignee.id,assignee.name,assignee.email,"
        "tags.tags.*.name"
    ),
    "contacts": (
        "id,external_id,email,phone,name,role,created_at,last_seen_at,"
        "last_replied_at,unsubscribed_from_emails,"
        "companies.companies.*.id,companies.companies.*.name,companies.companies.*.company_id"
    ),
    "companies": (
        "id,company_id,name,created_at,remote_created_at,last_request_at,"
        "monthly_spend,plan.name,user_count,session_count,custom_attributes"
    ),
    "articles": (
        "id,title,state,url,author_id,created_at,updated_at,parent_id,parent_type"
    ),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="intercom")


def _resource_key(path: str) -> str:
    """Derive the Intercom envelope items key from the endpoint path.

    Intercom embeds items under the resource type name, e.g. ``/conversations`` → ``conversations``.
    For unknown paths, fall back to inspecting the response (see ``_items_from_body``).
    """
    # Strip leading slash and take the first segment.
    segment = path.lstrip("/").split("/")[0]
    return _RESOURCE_ITEMS_KEY.get(segment, segment)


def _items_from_body(body: Any, resource_key: str) -> list:
    """Extract the items list from an Intercom list response envelope.

    Tries the declared resource_key first; if absent or not a list, walks the body to find the
    first list-valued key that is not the pagination envelope (``pages``, ``type``, …).
    """
    if isinstance(body, dict):
        candidate = body.get(resource_key)
        if isinstance(candidate, list):
            return list(candidate)
        # Fallback: first list-valued key in the envelope that isn't a pagination meta-key.
        _META_KEYS = frozenset({"pages", "type", "total_count"})
        for k, v in body.items():
            if k not in _META_KEYS and isinstance(v, list):
                return list(v)
    if isinstance(body, list):
        return list(body)
    return []


def _next_cursor(body: Any) -> str | None:
    """Extract the next cursor from ``pages.next.starting_after``; None if last page."""
    if not isinstance(body, dict):
        return None
    pages = body.get("pages")
    if not isinstance(pages, dict):
        return None
    nxt = pages.get("next")
    if not isinstance(nxt, dict):
        return None
    cursor = nxt.get("starting_after")
    return str(cursor) if cursor else None


def list_resource(
    path: str,
    *,
    query: dict[str, Any] | None = None,
    max_pages: int = 20,
    page_size: int = 50,
) -> dict:
    """Auto-page a list endpoint, dynamically extracting the items array from each page envelope.

    Returns ``{"items": [...], "incomplete": bool, "reason": str}`` — the same shape as
    ``api.Client.collect`` so callers are interchangeable.
    """
    c = _client()
    resource_key = _resource_key(path)
    q = dict(query or {}, per_page=page_size)
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
            q = dict(q, starting_after=cursor)
        else:
            incomplete = True
            reason = f"reached max_pages={max_pages}"
    except api.ApiError as e:
        incomplete = True
        reason = f"page fetch failed after {len(items)} item(s): {e}"

    return {"items": items, "incomplete": incomplete, "reason": reason}


def get_resource(path: str) -> Any:
    """GET a single resource by absolute path (e.g. ``/conversations/123``). Raises ApiError."""
    return _client().get(path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.intercom",
        description="Read-only Intercom grounding: conversations, contacts, companies, articles.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # -- list subcommand --
    ls = sub.add_parser("list", help="list a resource (auto-paginated, pre-selected fields)")
    ls.add_argument(
        "resource",
        choices=["conversations", "contacts", "companies", "articles", "tags", "admins"],
        help="resource type to list",
    )
    ls.add_argument("--query", action="append", default=[], metavar="K=V",
                    help="query param (repeatable, e.g. --query state=open)")
    ls.add_argument("--max-pages", type=int, default=10,
                    help="hard page cap (default 10)")
    ls.add_argument("--page-size", type=int, default=50,
                    help="items per page (default 50, max 150)")
    ls.add_argument("--no-pick", action="store_true",
                    help="return full objects instead of pre-selected support fields")

    # -- get subcommand --
    gt = sub.add_parser("get", help="fetch one resource by id")
    gt.add_argument("resource",
                    choices=["conversation", "contact", "company", "article"],
                    help="resource type")
    gt.add_argument("id", help="resource id")
    gt.add_argument("--no-pick", action="store_true",
                    help="return full object instead of pre-selected support fields")

    args = parser.parse_args(argv)

    if args.cmd == "list":
        path = f"/{args.resource}"
        query = dict(kv.split("=", 1) for kv in args.query if "=" in kv)
        result = list_resource(
            path, query=query, max_pages=args.max_pages, page_size=args.page_size
        )
        pick_key = args.resource  # "conversations" → pick key
        if not args.no_pick and pick_key in _PICK_FIELDS:
            result["items"] = [api.pick(it, _PICK_FIELDS[pick_key]) for it in result["items"]]
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.cmd == "get":
        # Map singular resource name → plural path prefix (e.g. "conversation" → "conversations").
        path_prefix = args.resource + "s"  # conversation→conversations, article→articles, etc.
        path = f"/{path_prefix}/{args.id}"
        body = get_resource(path)
        pick_key = path_prefix  # match _PICK_FIELDS keys
        if not args.no_pick and pick_key in _PICK_FIELDS:
            body = api.pick(body, _PICK_FIELDS[pick_key])
        print(json.dumps(body, indent=2, default=str))
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
