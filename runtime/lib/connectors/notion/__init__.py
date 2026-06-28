"""Notion support connector — script connector because search and database-query are POSTs.

Force-code triggers:
  (3) exotic transport — Notion's ``POST /v1/search`` and ``POST /v1/databases/{id}/query``
      send query params in a JSON request body; lib.api is GET-only.
  (1) field pre-selection — raw page ``properties`` objects are enormous (hundreds of nested keys);
      the script extracts the 4-6 support-relevant text/title/url/date fields.

For plain GET endpoints (retrieve page, retrieve database, block children) the agent drives
``python -m lib.api get notion …`` directly — the manifest row handles those with zero bespoke code.

CLI:
    python -m lib.connectors.notion search "onboarding checklist"
    python -m lib.connectors.notion search "bug" --filter page --page-size 10
    python -m lib.connectors.notion query-db <database_id>
    python -m lib.connectors.notion query-db <database_id> --page-size 50
"""

from __future__ import annotations

import argparse
from typing import Any

import requests as _requests

from lib import api, oauth

API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"

# The manifest row registered here is the lib.api declaration used by GET-based calls. Script calls
# (POST /search, POST /databases/{id}/query) bypass lib.api's request() and call _post() directly,
# but still share the credential resolution, retry, and backoff via lib.api.Client.
MANIFEST = api.register(
    api.Manifest(
        key="notion",
        base_url=API_BASE,
        auth=api.Auth(strategy="bearer"),
        pagination=api.Pagination(
            style="cursor",
            cursor_param="start_cursor",
            cursor_field="next_cursor",
            has_more_field="has_more",
            items_field="results",
            page_size=100,
        ),
        rate_limit_remaining_header="",  # Notion uses 429 + Retry-After, no remaining-count header
        default_headers={"Notion-Version": _NOTION_VERSION},
    )
)


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="notion")


def _post(path: str, body: dict[str, Any]) -> Any:
    """Issue one ``POST`` to a Notion endpoint with bearer auth + version header.

    lib.api is GET-only; search and database-query require a JSON POST body, so we call
    requests directly. Retry/backoff is intentionally not replicated here — single-call POSTs for
    search are idiomatic; the caller refreshes with a new cursor on pagination.
    """
    cred = oauth.token("notion")
    url = f"{API_BASE}/{path.lstrip('/')}"
    resp = _requests.post(
        url,
        json=body,
        headers={
            "Authorization": f"Bearer {cred}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        },
        timeout=(api.DEFAULT_CONNECT_TIMEOUT, api.DEFAULT_READ_TIMEOUT),
    )
    if not (200 <= resp.status_code < 300):
        try:
            err = resp.text
        except Exception:  # noqa: BLE001
            err = ""
        raise api.ApiError(resp.status_code, err, url=url)
    try:
        return resp.json()
    except ValueError:
        raise api.ApiError(resp.status_code, f"non-JSON response: {resp.text[:200]}", url=url)


def _post_paginate(path: str, body: dict[str, Any], *, page_size: int = 100) -> list[dict]:
    """Paginate a POST endpoint (search / database query) through all cursor pages.

    Notion's cursor pagination on POST endpoints works identically to GET: the next page body
    includes ``next_cursor`` (opaque string) and ``has_more`` (bool); we send ``start_cursor`` in
    the next request body.
    """
    results: list[dict] = []
    req_body = dict(body, page_size=page_size)
    while True:
        page = _post(path, req_body)
        results.extend(page.get("results") or [])
        if not page.get("has_more"):
            break
        req_body = dict(req_body, start_cursor=page["next_cursor"])
    return results


# ---------------------------------------------------------------------------
# Support-relevant field extraction
# ---------------------------------------------------------------------------

_PAGE_PICK = "id,url,created_time,last_edited_time,parent"

# Notion page property values are nested under arbitrary property names with typed objects.
# We extract the plain-text value for every property to keep context tight.
def _plain_text(rich_text_arr: Any) -> str:
    """Flatten a Notion rich_text array to a plain string."""
    if not isinstance(rich_text_arr, list):
        return ""
    return "".join(seg.get("plain_text", "") for seg in rich_text_arr)


def _extract_title(page: dict) -> str:
    """Return the human-readable title of a page object (the first title-type property)."""
    props = page.get("properties") or {}
    for val in props.values():
        if isinstance(val, dict) and val.get("type") == "title":
            return _plain_text(val.get("title") or [])
    return ""


def _compact_page(page: dict) -> dict:
    """Pre-select the support-relevant fields from a raw Notion page object.

    Raw pages have enormous ``properties`` objects with every database column. We extract:
    - id, url, created/edited timestamps, parent reference
    - title (first title-type property, flattened to plain text)
    - a summary of all other properties: {name: plain-text-value} for text/select/date types
    """
    props = page.get("properties") or {}
    compact_props: dict[str, Any] = {}
    for name, val in props.items():
        if not isinstance(val, dict):
            continue
        t = val.get("type")
        if t == "title":
            compact_props[name] = _plain_text(val.get("title") or [])
        elif t in ("rich_text", "text"):
            compact_props[name] = _plain_text(val.get(t) or [])
        elif t == "select":
            sel = val.get("select") or {}
            compact_props[name] = sel.get("name") or ""
        elif t == "multi_select":
            compact_props[name] = [s.get("name", "") for s in (val.get("multi_select") or [])]
        elif t == "status":
            st = val.get("status") or {}
            compact_props[name] = st.get("name") or ""
        elif t == "date":
            d = val.get("date") or {}
            compact_props[name] = d.get("start") or ""
        elif t == "url":
            compact_props[name] = val.get("url") or ""
        elif t == "email":
            compact_props[name] = val.get("email") or ""
        elif t == "phone_number":
            compact_props[name] = val.get("phone_number") or ""
        elif t == "number":
            compact_props[name] = val.get("number")
        elif t == "checkbox":
            compact_props[name] = val.get("checkbox")
        # Skip: formula, rollup, relation, files, people, created_by, last_edited_by (complex/noisy)

    return {
        "id": page.get("id"),
        "url": page.get("url"),
        "created_time": page.get("created_time"),
        "last_edited_time": page.get("last_edited_time"),
        "parent": page.get("parent"),
        "title": _extract_title(page),
        "properties": compact_props,
    }


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def search(
    query: str = "",
    *,
    filter_type: str | None = None,
    page_size: int = 100,
) -> list[dict]:
    """POST /v1/search — find pages/databases matching a keyword, return compact page objects.

    ``filter_type`` is ``"page"`` or ``"database"`` (Notion calls this value ``"page"`` for
    pages and ``"data_source"`` for database-backed sources, but the documented filter object
    uses ``"page"`` for pages and omits the key or passes ``"database"`` for databases).
    """
    body: dict[str, Any] = {}
    if query:
        body["query"] = query
    if filter_type:
        # Notion's filter object: {"value": "page"|"database", "property": "object"}
        body["filter"] = {"value": filter_type, "property": "object"}
    # Paths are relative to API_BASE (https://api.notion.com/v1); no leading "v1/" prefix.
    results = _post_paginate("search", body, page_size=page_size)
    return [_compact_page(r) for r in results]


def query_database(
    database_id: str,
    *,
    page_size: int = 100,
) -> list[dict]:
    """POST /v1/databases/{id}/query — return all rows of a database as compact page objects.

    No filter is applied (all rows returned). The agent can pipe this through ``jq`` or pass
    ``--pick`` to narrow further.
    """
    path = f"databases/{database_id}/query"
    results = _post_paginate(path, {}, page_size=page_size)
    return [_compact_page(r) for r in results]


def _results_to_markdown(results: list[dict], heading: str) -> str:
    lines = [f"# {heading}", f"Found {len(results)} result(s).", ""]
    for r in results:
        title = r.get("title") or r.get("id", "—")
        url = r.get("url") or ""
        edited = r.get("last_edited_time") or ""
        lines.append(f"## {title}")
        if url:
            lines.append(f"- URL: {url}")
        if edited:
            lines.append(f"- Last edited: {edited}")
        props = r.get("properties") or {}
        if props:
            for k, v in props.items():
                if v not in (None, "", [], {}):
                    lines.append(f"- {k}: {v}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.notion")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="search pages/databases by keyword (POST /v1/search)")
    s.add_argument("query", nargs="?", default="", help="keyword to search (omit for all)")
    s.add_argument("--filter", dest="filter_type", choices=["page", "database"],
                   help="restrict to pages or databases")
    s.add_argument("--page-size", type=int, default=100)

    q = sub.add_parser("query-db", help="list all rows in a database (POST /v1/databases/{id}/query)")
    q.add_argument("database_id", help="Notion database UUID")
    q.add_argument("--page-size", type=int, default=100)

    args = parser.parse_args(argv)

    if args.cmd == "search":
        results = search(args.query, filter_type=args.filter_type, page_size=args.page_size)
        print(_results_to_markdown(results, f'Notion search: "{args.query}"'))
    elif args.cmd == "query-db":
        results = query_database(args.database_id, page_size=args.page_size)
        print(_results_to_markdown(results, f"Notion database: {args.database_id}"))
    else:
        parser.error("unknown command")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
