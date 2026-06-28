"""Dropbox support connector — script connector because all API v2 endpoints are RPC POSTs.

Force-code triggers:
  (3) exotic transport — Dropbox API v2 is entirely POST-based (JSON body); lib.api is GET-only.
  (4) non-standard pagination — cursor continuation requires a POST to a *different* path
      (files/list_folder/continue) with the cursor in the request body; lib.api's generic cursor
      paginator only adjusts query params on the same path.
  (1) field pre-selection — raw list_folder entries carry full metadata (client_modified,
      server_modified, rev, content_hash, symlink_info, export_info…); 5-6 fields answer support Qs.

Read-only: every call here only reads metadata; we never create, copy, move, or delete.

Dropbox OAuth access tokens start with "sl" + "." (short-lived); long-lived tokens are opaque
strings injected via RC_CONN_DROPBOX. The token itself is never printed or logged here.

CLI:
    python -m lib.connectors.dropbox list-folder /Documents
    python -m lib.connectors.dropbox list-folder "" --recursive
    python -m lib.connectors.dropbox get-metadata /Documents/report.pdf
    python -m lib.connectors.dropbox search "quarterly report"
    python -m lib.connectors.dropbox search "invoice" --path /Finance
    python -m lib.connectors.dropbox shared-links
    python -m lib.connectors.dropbox shared-links --path /Public/file.pdf
    python -m lib.connectors.dropbox account
"""

from __future__ import annotations

import argparse
from typing import Any

import requests as _requests

from lib import api, oauth

_API_BASE = "https://api.dropboxapi.com/2"

# The manifest row provides the catalog shape; POST calls bypass lib.api.request() and go through
# _post() directly. Bearer auth from RC_CONN_DROPBOX is the same credential either way.
MANIFEST = api.register(
    api.Manifest(
        key="dropbox",
        base_url=_API_BASE,
        auth=api.Auth(strategy="bearer"),
        pagination=api.Pagination(style="none"),  # POST-based pagination is handled in the script
        rate_limit_remaining_header="",
    )
)


def _post(path: str, body: dict[str, Any]) -> Any:
    """Issue one POST to a Dropbox RPC endpoint with bearer auth.

    Dropbox API v2 is entirely RPC-style: all calls are POST with a JSON body. lib.api is GET-only,
    so this helper handles auth + error normalization for the script's calls. Retry is not replicated
    here — reads are fast and single-shot pagination is the pattern; the caller loops with cursors.
    """
    cred = oauth.token("dropbox")
    url = f"{_API_BASE}/{path.lstrip('/')}"
    resp = _requests.post(
        url,
        json=body,
        headers={
            "Authorization": f"Bearer {cred}",
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


# ---------------------------------------------------------------------------
# Field pre-selection (trigger 1)
# ---------------------------------------------------------------------------

def _compact_entry(entry: dict) -> dict:
    """Extract the 6 support-relevant fields from a raw Dropbox file/folder metadata entry.

    Raw entries include rev, content_hash, symlink_info, export_info, is_downloadable, and more
    that is noise for support. We keep: tag, name, path, size, modified time, id.
    """
    return {
        "type": entry.get(".tag"),          # "file" | "folder" | "deleted"
        "name": entry.get("name"),
        "path": entry.get("path_display"),
        "id": entry.get("id"),
        "size": entry.get("size"),           # bytes; None for folders
        "modified": entry.get("server_modified"),  # ISO-8601; None for folders
    }


def _compact_shared_link(link: dict) -> dict:
    """Pre-select support fields from a shared link object."""
    return {
        "url": link.get("url"),
        "name": link.get("name"),
        "path": link.get("path_lower"),
        "link_type": link.get(".tag"),       # "file" | "folder"
        "visibility": (link.get("link_permissions") or {}).get("resolved_visibility", {}).get(".tag"),
        "expires": link.get("expires"),      # ISO-8601 or None if no expiry
    }


# ---------------------------------------------------------------------------
# Pagination: two-phase POST cursor (trigger 4)
# ---------------------------------------------------------------------------

def _list_folder_all(path: str, *, recursive: bool = False) -> list[dict]:
    """Paginate files/list_folder → files/list_folder/continue until has_more is False.

    Dropbox cursor pagination is a POST-based two-phase pattern: the first call POSTs to
    files/list_folder with the desired options; subsequent pages POST to files/list_folder/continue
    with only the cursor from the previous response. lib.api's generic cursor loop only changes a
    query param on the same URL — it cannot express this, so we drive the loop here.
    """
    body: dict[str, Any] = {
        "path": path,
        "recursive": recursive,
        "include_deleted": False,
        "include_has_explicit_shared_members": False,
        "include_media_info": False,
    }
    result = _post("files/list_folder", body)
    entries: list[dict] = list(result.get("entries") or [])

    while result.get("has_more"):
        cursor = result.get("cursor")
        result = _post("files/list_folder/continue", {"cursor": cursor})
        entries.extend(result.get("entries") or [])

    return entries


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def list_folder(path: str, *, recursive: bool = False) -> list[dict]:
    """List files and folders at ``path`` (use "" for Dropbox root) as compact dicts."""
    entries = _list_folder_all(path, recursive=recursive)
    return [_compact_entry(e) for e in entries]


def get_metadata(path: str) -> dict:
    """Return compact metadata for a single file or folder at ``path``."""
    raw = _post("files/get_metadata", {"path": path})
    return _compact_entry(raw)


def search(query: str, *, path: str = "", max_results: int = 20) -> list[dict]:
    """Search for files/folders matching ``query`` (full text + filename search).

    Uses files/search/v2 which supports scoped path search and returns file metadata in ``matches``.
    """
    options: dict[str, Any] = {"max_results": max_results}
    if path:
        options["path"] = path
    body: dict[str, Any] = {"query": query, "options": options}
    result = _post("files/search/v2", body)
    matches = result.get("matches") or []
    # Each match has a {"metadata": {"metadata": <file_metadata>}} structure (files/search/v2)
    entries = []
    for m in matches:
        meta_wrapper = m.get("metadata") or {}
        meta = meta_wrapper.get("metadata") or {}
        if meta:
            entries.append(_compact_entry(meta))
    return entries


def shared_links(*, path: str = "") -> list[dict]:
    """List shared links, optionally filtered to ``path``."""
    body: dict[str, Any] = {"direct_only": True}
    if path:
        body["path"] = path
    result = _post("sharing/list_shared_links", body)
    links = result.get("links") or []
    return [_compact_shared_link(lk) for lk in links]


def account_info() -> dict:
    """Return current account display name, email, and quota."""
    raw = _post("users/get_current_account", {})
    quota_raw = _post("users/get_space_usage", {})
    used = quota_raw.get("used") or 0
    alloc = (quota_raw.get("allocation") or {}).get("allocated") or 0
    return {
        "name": (raw.get("name") or {}).get("display_name"),
        "email": raw.get("email"),
        "account_id": raw.get("account_id"),
        "account_type": (raw.get("account_type") or {}).get(".tag"),
        "quota_used_bytes": used,
        "quota_allocated_bytes": alloc,
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _entries_to_markdown(entries: list[dict], heading: str) -> str:
    lines = [f"# {heading}", f"Found {len(entries)} item(s).", ""]
    for e in entries:
        tag = e.get("type") or "?"
        name = e.get("name") or e.get("path") or "—"
        lines.append(f"## [{tag}] {name}")
        if e.get("path"):
            lines.append(f"- Path: `{e['path']}`")
        if e.get("size") is not None:
            lines.append(f"- Size: {e['size']:,} bytes")
        if e.get("modified"):
            lines.append(f"- Modified: {e['modified']}")
        if e.get("id"):
            lines.append(f"- ID: `{e['id']}`")
        lines.append("")
    return "\n".join(lines)


def _links_to_markdown(links: list[dict]) -> str:
    lines = ["# Dropbox shared links", f"Found {len(links)} link(s).", ""]
    for lk in links:
        name = lk.get("name") or lk.get("path") or "—"
        lines.append(f"## {name}")
        if lk.get("url"):
            lines.append(f"- URL: {lk['url']}")
        if lk.get("path"):
            lines.append(f"- Path: `{lk['path']}`")
        if lk.get("visibility"):
            lines.append(f"- Visibility: {lk['visibility']}")
        if lk.get("expires"):
            lines.append(f"- Expires: {lk['expires']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.dropbox")
    sub = parser.add_subparsers(dest="cmd", required=True)

    lf = sub.add_parser("list-folder", help="list files/folders at a path")
    lf.add_argument("path", nargs="?", default="",
                    help='Dropbox path (e.g. /Documents); omit or "" for root')
    lf.add_argument("--recursive", action="store_true", help="recurse into sub-folders")

    gm = sub.add_parser("get-metadata", help="metadata for a single file or folder")
    gm.add_argument("path", help="Dropbox path (e.g. /Documents/report.pdf)")

    sr = sub.add_parser("search", help="search for files/folders by keyword")
    sr.add_argument("query", help="search keywords")
    sr.add_argument("--path", default="", help="restrict search to this folder path")
    sr.add_argument("--max-results", type=int, default=20)

    sl = sub.add_parser("shared-links", help="list shared links")
    sl.add_argument("--path", default="", help="filter to this path")

    sub.add_parser("account", help="current account name, email, quota")

    args = parser.parse_args(argv)

    if args.cmd == "list-folder":
        entries = list_folder(args.path, recursive=args.recursive)
        path_label = args.path or "(root)"
        print(_entries_to_markdown(entries, f"Dropbox: {path_label}"))
    elif args.cmd == "get-metadata":
        entry = get_metadata(args.path)
        print(_entries_to_markdown([entry], f"Dropbox metadata: {args.path}"))
    elif args.cmd == "search":
        entries = search(args.query, path=args.path, max_results=args.max_results)
        print(_entries_to_markdown(entries, f'Dropbox search: "{args.query}"'))
    elif args.cmd == "shared-links":
        links = shared_links(path=args.path)
        print(_links_to_markdown(links))
    elif args.cmd == "account":
        import json
        print(json.dumps(account_info(), indent=2))
    else:
        parser.error("unknown command")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
