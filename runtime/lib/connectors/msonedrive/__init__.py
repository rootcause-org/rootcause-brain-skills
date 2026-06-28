"""Microsoft OneDrive / Microsoft Graph connector.

Force-code trigger (d): Graph paginates via ``@odata.nextLink`` — a full absolute URL in the JSON
body. ``lib.api``'s ``link`` style reads RFC 8288 ``Link:`` response headers; ``cursor`` style sends
a token back as a query param. Neither can follow an opaque body URL, so this thin script handles
multi-page traversal with ``collect_odata()`` and exposes a concise CLI for the support agent.

Read-only: only ever issues GETs. Imports ``lib.api`` — never re-implements retry/backoff/auth.

CLI:
    python -m lib.connectors.msonedrive drive
    python -m lib.connectors.msonedrive children me/drive/root
    python -m lib.connectors.msonedrive children drives/{drive-id}/items/{item-id}
    python -m lib.connectors.msonedrive search "quarterly report"
    python -m lib.connectors.msonedrive recent
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from lib import api

# ---------------------------------------------------------------------------
# Manifest (registered so `python -m lib.api get msonedrive ...` also works)
# ---------------------------------------------------------------------------

_MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.yaml"
_raw = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))

MANIFEST = api.register(
    api.Manifest(
        key="msonedrive",
        base_url=_raw["base_url"],
        auth=api.Auth(strategy="bearer"),
        # style=none: the generic paginator won't drive list calls; collect_odata() does it.
        pagination=api.Pagination(style="none", items_field="value", page_size=200),
        rate_limit_remaining_header="",
    )
)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Support-relevant fields to extract from a DriveItem (avoids flooding context with full objects).
_ITEM_FIELDS = "id,name,size,lastModifiedDateTime,createdDateTime,webUrl,file,folder,parentReference.path"


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="msonedrive")


# ---------------------------------------------------------------------------
# @odata.nextLink pagination helper
# ---------------------------------------------------------------------------


def collect_odata(
    path: str,
    *,
    query: dict[str, Any] | None = None,
    max_items: int = 2000,
) -> dict:
    """Collect all items from a Graph collection endpoint, following ``@odata.nextLink`` pages.

    Returns ``{"items": [...], "incomplete": bool, "reason": str}`` — same shape as
    ``lib.api.Client.collect`` so callers are interchangeable. Graph throttles via 429 +
    Retry-After which lib.api's retry layer already honours.
    """
    c = _client()
    items: list = []
    incomplete = False
    reason = ""

    # First page via lib.api (handles auth + retry).
    page = c.fetch_page(path, query=query)
    items.extend(page.items)
    next_url: str | None = _odata_next(page.body)

    while next_url and len(items) < max_items:
        # Follow the absolute nextLink URL directly — the token/skip info is already embedded.
        # _send_url applies auth headers (bearer) to the follow request, matching lib.api's
        # link-style behaviour for RFC 8288 follow, but sourcing the URL from the body.
        resp_raw = c._send_url("GET", next_url)  # noqa: SLF001 — internal pagination helper
        body = api._parse_json(resp_raw)  # noqa: SLF001
        page_items = c._extract_items(body)  # noqa: SLF001
        items.extend(page_items)
        next_url = _odata_next(body)

    if len(items) > max_items:
        items = items[:max_items]
        incomplete = True
        reason = f"reached max_items={max_items}"
    elif next_url:
        incomplete = True
        reason = "pagination stopped at max_items limit"

    return {"items": items, "incomplete": incomplete, "reason": reason}


def _odata_next(body: Any) -> str | None:
    """Extract ``@odata.nextLink`` from a Graph response body, or None when exhausted."""
    if not isinstance(body, dict):
        return None
    return body.get("@odata.nextLink") or None


# ---------------------------------------------------------------------------
# Public API surface (each function returns a plain dict for easy piping/testing)
# ---------------------------------------------------------------------------


def get_drive(drive_path: str = "me/drive") -> dict:
    """Fetch drive metadata. ``drive_path`` defaults to the user's default OneDrive."""
    c = _client()
    return c.get(drive_path)


def list_children(item_path: str, *, max_items: int = 500) -> dict:
    """List all children of a folder at ``item_path`` (e.g. ``me/drive/root`` or
    ``drives/{id}/items/{id}``), following ``@odata.nextLink`` pages.

    ``item_path`` is joined onto ``/children``; the agent can pass any valid Graph DriveItem path.
    Picks the support-relevant subset of each DriveItem to keep output compact.
    """
    path = item_path.rstrip("/") + "/children"
    result = collect_odata(path, query={"$top": 200}, max_items=max_items)
    result["items"] = [api.pick(it, _ITEM_FIELDS) for it in result["items"]]
    return result


def search_files(query_text: str, *, drive_path: str = "me/drive/root", max_items: int = 200) -> dict:
    """Search for files matching ``query_text`` across the drive rooted at ``drive_path``.

    Uses the Graph ``search(q='...')`` OData function; results include items from shared folders
    when ``drive_path`` is ``me/drive``. Picks support-relevant fields from each hit.
    """
    # The q value is an OData function parameter, embedded in the path literal.
    # Single quotes in the search term are escaped as '' per OData convention.
    safe_q = (query_text or "").replace("'", "''")
    path = f"{drive_path}/search(q='{safe_q}')"
    result = collect_odata(path, query={"$top": 200}, max_items=max_items)
    result["items"] = [api.pick(it, _ITEM_FIELDS) for it in result["items"]]
    return result


def list_recent(*, max_items: int = 50) -> dict:
    """List the signed-in user's recently accessed files (single-page, no @odata.nextLink)."""
    c = _client()
    body = c.get("me/drive/recent")
    items = body.get("value") or []
    picked = [api.pick(it, _ITEM_FIELDS) for it in items[:max_items]]
    return {"items": picked, "incomplete": len(items) > max_items, "reason": ""}


# ---------------------------------------------------------------------------
# Markdown rendering helpers
# ---------------------------------------------------------------------------


def _fmt_size(size: Any) -> str:
    if not isinstance(size, (int, float)):
        return ""
    kb = size / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024:.1f} MB"


def _item_line(it: dict) -> str:
    name = it.get("name", "(unnamed)")
    url = it.get("webUrl", "")
    kind = "📁" if it.get("folder") else "📄"
    size_str = _fmt_size(it.get("size"))
    modified = (it.get("lastModifiedDateTime") or "")[:10]  # date only
    link = f"[{name}]({url})" if url else name
    parts = [f"- {kind} {link}"]
    if size_str:
        parts.append(size_str)
    if modified:
        parts.append(modified)
    return "  ".join(parts)


def items_to_markdown(result: dict, title: str) -> str:
    """Render a collect_odata result as a concise markdown list."""
    lines = [f"# {title}"]
    items = result.get("items") or []
    if not items:
        lines.append("_(no items)_")
    else:
        lines.extend(_item_line(it) for it in items)
    if result.get("incomplete"):
        lines.append(f"\n_(truncated — {result.get('reason', 'more items exist')})_")
    return "\n".join(lines)


def drive_to_markdown(drive: dict) -> str:
    """Render drive metadata as a concise markdown block."""
    name = drive.get("name") or drive.get("id", "")
    dtype = drive.get("driveType", "")
    owner = ""
    o = drive.get("owner") or {}
    user = o.get("user") or {}
    owner = user.get("displayName") or user.get("email") or ""
    quota = drive.get("quota") or {}
    used = _fmt_size(quota.get("used"))
    total = _fmt_size(quota.get("total"))
    lines = [f"# OneDrive: {name}"]
    if dtype:
        lines.append(f"- Type: {dtype}")
    if owner:
        lines.append(f"- Owner: {owner}")
    if used or total:
        lines.append(f"- Storage: {used} used / {total} total")
    url = drive.get("webUrl")
    if url:
        lines.append(f"- URL: {url}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.msonedrive")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("drive", help="show default OneDrive metadata")

    ch = sub.add_parser("children", help="list children of a folder")
    ch.add_argument(
        "item_path",
        help="Graph path to a folder, e.g. me/drive/root or drives/{id}/items/{id}",
    )
    ch.add_argument("--max", type=int, default=500, dest="max_items")

    sr = sub.add_parser("search", help="search files by keyword")
    sr.add_argument("query_text", help="search term")
    sr.add_argument("--drive", default="me/drive/root", dest="drive_path")
    sr.add_argument("--max", type=int, default=200, dest="max_items")

    sub.add_parser("recent", help="list recently accessed files")

    args = parser.parse_args(argv)

    if args.cmd == "drive":
        print(drive_to_markdown(get_drive()))
    elif args.cmd == "children":
        result = list_children(args.item_path, max_items=args.max_items)
        print(items_to_markdown(result, f"Children of {args.item_path}"))
    elif args.cmd == "search":
        result = search_files(args.query_text, drive_path=args.drive_path, max_items=args.max_items)
        print(items_to_markdown(result, f'Search: "{args.query_text}"'))
    elif args.cmd == "recent":
        result = list_recent()
        print(items_to_markdown(result, "Recent files"))
    else:
        parser.error("unknown command")
        return 2

    return 0
