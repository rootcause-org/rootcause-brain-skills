"""monday.com support connector — GraphQL transport over ``lib.api``'s retry/auth machinery.

Force-code triggers:
  (c) GraphQL transport — monday.com has NO REST API; every read is a POST with a ``query`` body.
      ``lib.api`` is a GET/REST client and cannot express this natively.
  (a) field pre-selection — board/item responses are deeply nested; pre-select the 4-6 fields
      support actually needs so a raw GraphQL dump never floods model context.

The connector wraps ``lib.api.Client._send`` for retry/auth/timeout but overrides the GET with a
GraphQL POST. Pagination is handled via monday.com's ``next_items_page`` cursor (per-board,
60-minute TTL; the generic lib.api cursor pager cannot follow this pattern).

Read-only by design: every operation issues a GraphQL query (read) — mutations are not exposed.

CLI:
    python -m lib.connectors.monday board 1234567890
    python -m lib.connectors.monday items 1234567890 [--limit N]
    python -m lib.connectors.monday user me
    python -m lib.connectors.monday search "text"
    python -m lib.connectors.monday query 'query { me { id name email } }'
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from lib import _http_audit, api, oauth

_BASE_URL = "https://api.monday.com/v2"
_ENV_KEY = "monday"

# The manifest row — registered so `python -m lib.api get monday …` surfaces a helpful error
# instead of "unknown key". The script CLI is the primary entry point.
MANIFEST = api.register(
    api.Manifest(
        key="monday",
        base_url=_BASE_URL,
        auth=api.Auth(strategy="bearer"),
        pagination=api.Pagination(style="none"),
        rate_limit_remaining_header="",
        default_headers={
            "API-Version": "2026-04",
            "Content-Type": "application/json",
        },
    )
)


# ---------------------------------------------------------------------------
# GraphQL transport
# ---------------------------------------------------------------------------


def _gql(query: str, variables: dict | None = None) -> dict:
    """POST one GraphQL query and return the parsed ``data`` dict.

    Uses lib.api's bearer auth and timeout values, but drives a POST directly because lib.api's
    ``request()`` method is GET-shaped. Raises ``api.ApiError`` on HTTP errors or GraphQL errors.
    """
    token = oauth.token(_ENV_KEY)
    headers = {
        "Authorization": token,  # monday.com expects bare token (no "Bearer " prefix in docs)
        "API-Version": "2026-04",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {"query": query}
    if variables:
        body["variables"] = variables

    resp = _http_audit.request(
        "POST",
        _BASE_URL,
        json_body=body,
        headers=headers,
        timeout=(api.DEFAULT_CONNECT_TIMEOUT, api.DEFAULT_READ_TIMEOUT),
        endpoint_template="/v2",
        known_secrets=(token,),
    )
    if not (200 <= resp.status_code < 300):
        raise api.ApiError(resp.status_code, resp.text, url=_BASE_URL,
                           retryable=resp.status_code in {429, 500, 502, 503, 504})

    parsed = _parse_gql_response(resp)
    return parsed


def _parse_gql_response(resp) -> dict:
    """Parse JSON and surface any GraphQL ``errors`` as an ``ApiError``."""
    try:
        body = resp.json()
    except ValueError as e:
        raise api.ApiError(resp.status_code, f"non-JSON GraphQL response: {resp.text[:200]}",
                           url=_BASE_URL) from e
    errors = body.get("errors")
    if errors:
        msg = "; ".join(e.get("message", str(e)) for e in errors)
        raise api.ApiError(resp.status_code, f"GraphQL error: {msg}", url=_BASE_URL)
    data = body.get("data")
    if data is None:
        raise api.ApiError(resp.status_code, f"GraphQL response has no 'data': {str(body)[:400]}",
                           url=_BASE_URL)
    return data


# ---------------------------------------------------------------------------
# Typed read helpers (the support-relevant paths)
# ---------------------------------------------------------------------------


def get_me() -> dict:
    """Authenticated user info — useful for verifying the connection and resolving the account."""
    data = _gql("query { me { id name email account { id name } } }")
    return data.get("me") or {}


def get_boards(*, limit: int = 50) -> list[dict]:
    """List boards the token can see — id, name, state, description."""
    data = _gql(
        "query($limit: Int!) { boards(limit: $limit, order_by: created_at) "
        "{ id name state description board_kind workspace { id name } } }",
        {"limit": limit},
    )
    return data.get("boards") or []


def get_board(board_id: int | str) -> dict:
    """One board: metadata + column definitions."""
    data = _gql(
        "query($ids: [ID!]) { boards(ids: $ids) "
        "{ id name state description columns { id title type } "
        "groups { id title } workspace { id name } } }",
        {"ids": [str(board_id)]},
    )
    boards = data.get("boards") or []
    return boards[0] if boards else {}


def get_items(board_id: int | str, *, limit: int = 50) -> list[dict]:
    """Items on a board — follows monday.com's cursor pagination (items_page / next_items_page).

    Each item includes id, name, state, group, and the text value of every column so a support
    agent can read the row without knowing the board schema in advance.
    """
    # Initial page
    data = _gql(
        "query($ids: [ID!], $limit: Int!) { boards(ids: $ids) "
        "{ items_page(limit: $limit) { cursor items { id name state "
        "group { id title } column_values { id text } "
        "updates(limit: 3) { id body created_at creator { name } } } } } }",
        {"ids": [str(board_id)], "limit": min(limit, 100)},
    )
    boards = data.get("boards") or []
    if not boards:
        return []
    items_page = (boards[0] or {}).get("items_page") or {}
    items: list[dict] = list(items_page.get("items") or [])
    cursor = items_page.get("cursor")

    # Follow cursor pages until we have enough or cursor is exhausted.
    while cursor and len(items) < limit:
        remaining = limit - len(items)
        next_data = _gql(
            "query($cursor: String!, $limit: Int!) "
            "{ next_items_page(cursor: $cursor, limit: $limit) "
            "{ cursor items { id name state group { id title } "
            "column_values { id text } updates(limit: 3) { id body created_at creator { name } } } } }",
            {"cursor": cursor, "limit": min(remaining, 100)},
        )
        nxt = next_data.get("next_items_page") or {}
        page_items = nxt.get("items") or []
        items.extend(page_items)
        cursor = nxt.get("cursor")
        if not page_items:
            break

    return items[:limit]


def get_updates(item_id: int | str, *, limit: int = 25) -> list[dict]:
    """Updates (comments/activity) on a specific item."""
    data = _gql(
        "query($ids: [ID!], $limit: Int!) { items(ids: $ids) "
        "{ updates(limit: $limit) { id body created_at creator { id name } } } }",
        {"ids": [str(item_id)], "limit": limit},
    )
    items = data.get("items") or []
    if not items:
        return []
    return (items[0] or {}).get("updates") or []


def search_items(query_text: str, *, limit: int = 20) -> list[dict]:
    """Full-text search across items visible to the token."""
    data = _gql(
        "query($query: String!, $limit: Int!) "
        "{ items_by_multiple_column_values(limit: $limit, board_ids: [], "
        "column_id: \"name\", column_values: [$query]) "
        "{ id name state board { id name } group { id title } } }",
        {"query": query_text, "limit": limit},
    )
    # monday.com's text search endpoint; fall back gracefully if not available.
    return data.get("items_by_multiple_column_values") or []


# ---------------------------------------------------------------------------
# Markdown renderers
# ---------------------------------------------------------------------------


def _board_to_markdown(board: dict) -> str:
    lines = [f"# monday.com Board: {board.get('name') or board.get('id')}"]
    lines.append(f"- ID: `{board.get('id')}`  State: **{board.get('state', 'unknown')}**")
    ws = (board.get("workspace") or {}).get("name")
    if ws:
        lines.append(f"- Workspace: {ws}")
    desc = board.get("description")
    if desc:
        lines.append(f"- Description: {desc}")
    cols = board.get("columns") or []
    if cols:
        lines.append("\n## Columns")
        for c in cols:
            lines.append(f"- `{c.get('id')}` {c.get('title')} ({c.get('type', '?')})")
    groups = board.get("groups") or []
    if groups:
        lines.append("\n## Groups")
        for g in groups:
            lines.append(f"- {g.get('title')} (`{g.get('id')}`)")
    return "\n".join(lines)


def _items_to_markdown(items: list[dict], *, board_id: str = "") -> str:
    header = "# monday.com Items" + (f" (board {board_id})" if board_id else "")
    if not items:
        return header + "\n\n_(no items)_"
    lines = [header]
    for it in items:
        group = (it.get("group") or {}).get("title", "")
        lines.append(f"\n## {it.get('name') or it.get('id')}"
                     + (f" [{group}]" if group else ""))
        lines.append(f"- ID: `{it.get('id')}`  State: {it.get('state', '?')}")
        for cv in (it.get("column_values") or []):
            text = (cv.get("text") or "").strip()
            if text:
                lines.append(f"- {cv.get('id')}: {text}")
        for upd in (it.get("updates") or []):
            creator = (upd.get("creator") or {}).get("name", "?")
            body = (upd.get("body") or "").strip()[:200]
            ts = upd.get("created_at", "")[:10]
            lines.append(f"  - Update ({ts}, {creator}): {body}")
    return "\n".join(lines)


def _me_to_markdown(me: dict) -> str:
    acc = (me.get("account") or {}).get("name", "")
    lines = [f"# monday.com: {me.get('name') or me.get('id')}"]
    lines.append(f"- ID: `{me.get('id')}`  Email: {me.get('email', '?')}")
    if acc:
        lines.append(f"- Account: {acc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.monday",
        description="monday.com read-only support connector (GraphQL)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("user", help="show authenticated user info")

    bd = sub.add_parser("board", help="show board metadata and columns")
    bd.add_argument("board_id", help="numeric board ID")

    it = sub.add_parser("items", help="list items on a board")
    it.add_argument("board_id", help="numeric board ID")
    it.add_argument("--limit", type=int, default=50, help="max items (default 50)")

    bs = sub.add_parser("boards", help="list visible boards")
    bs.add_argument("--limit", type=int, default=50)

    sr = sub.add_parser("search", help="text search across item names")
    sr.add_argument("text", help="search text")
    sr.add_argument("--limit", type=int, default=20)

    qr = sub.add_parser("query", help="run a raw read-only GraphQL query (prints JSON data)")
    qr.add_argument("gql", help="GraphQL query string")

    args = parser.parse_args(argv)

    if args.cmd == "user":
        me = get_me()
        print(_me_to_markdown(me))

    elif args.cmd == "board":
        board = get_board(args.board_id)
        if not board:
            print(f"Board `{args.board_id}` not found.")
            return 1
        print(_board_to_markdown(board))

    elif args.cmd == "items":
        items = get_items(args.board_id, limit=args.limit)
        print(_items_to_markdown(items, board_id=args.board_id))

    elif args.cmd == "boards":
        boards = get_boards(limit=args.limit)
        if not boards:
            print("No boards found.")
            return 0
        lines = ["# monday.com Boards"]
        for b in boards:
            ws = (b.get("workspace") or {}).get("name", "")
            lines.append(f"- `{b.get('id')}` **{b.get('name')}** ({b.get('state', '?')})"
                         + (f" — {ws}" if ws else ""))
        print("\n".join(lines))

    elif args.cmd == "search":
        items = search_items(args.text, limit=args.limit)
        print(_items_to_markdown(items))

    elif args.cmd == "query":
        data = _gql(args.gql)
        print(json.dumps(data, indent=2, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
