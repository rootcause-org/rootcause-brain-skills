"""Microsoft Outlook Mail connector — reads messages and folders via Microsoft Graph v1.0.

Force-code triggers:
  (a) Field pre-selection: Graph message objects are enormous (full HTML body, all recipient
      arrays, internet message headers, extended properties). Support needs only ~8 fields per
      message; this connector pre-selects them so a raw API dump never floods model context.
  (d) Non-standard pagination: Graph paginates with ``@odata.nextLink`` — an absolute URL in the
      JSON body. lib.api's ``link`` style follows RFC 8288 ``Link:`` response headers; ``cursor``
      style sends the cursor value as a query param. Neither maps to Graph's "next absolute URL in
      the response body" pattern, so this thin script runs the loop and delegates retry/backoff/auth
      to ``lib.api``.

Read-only: only ever issues GETs. Imports ``lib.api`` — never re-implements retry/backoff/auth.

CLI:
    python -m lib.connectors.msoutlookmail messages [--user UPN] [--folder NAME] [--top N]
    python -m lib.connectors.msoutlookmail search QUERY [--user UPN] [--top N]
    python -m lib.connectors.msoutlookmail folders [--user UPN]
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from lib import api

BASE = "https://graph.microsoft.com/v1.0"

# Registered so `python -m lib.api get msoutlookmail …` and the connector CLI both resolve the
# same manifest instance.  Matches manifest.yaml exactly.
MANIFEST = api.register(
    api.Manifest(
        key="msoutlookmail",
        base_url=BASE,
        auth=api.Auth(strategy="bearer"),
        # style=none: the generic paginator never drives list calls; collect_odata() handles paging.
        pagination=api.Pagination(style="none", items_field="value", page_size=50),
        rate_limit_remaining_header="",  # Graph uses 429 + Retry-After
    )
)


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="msoutlookmail")


def _collect_odata(path: str, *, query: dict[str, Any] | None = None, max_items: int = 200) -> list[dict]:
    """GET a Graph collection endpoint and follow ``@odata.nextLink`` until exhausted or max_items.

    Graph pagination: each response carries ``value`` (the item array) and optionally
    ``@odata.nextLink`` (an absolute URL for the next page). This is the force-code trigger (d):
    lib.api has no pagination style that follows an absolute URL from the JSON body.
    """
    c = _client()
    items: list[dict] = []

    body = c.get(path, query=query)
    items.extend(body.get("value") or [])

    next_url: str | None = body.get("@odata.nextLink")
    while next_url and len(items) < max_items:
        # _send_url follows the absolute URL verbatim, applying bearer auth and retries.
        resp = c._send_url("GET", next_url)  # noqa: SLF001 — internal lib.api seam for absolute URLs
        body = json.loads(resp.text)
        items.extend(body.get("value") or [])
        next_url = body.get("@odata.nextLink")

    return items[:max_items]


# ---------------------------------------------------------------------------
# Support-relevant field sets (force-code trigger a: field pre-selection)
# ---------------------------------------------------------------------------

# Minimal fields via $select — avoids fetching the full HTML body on every message.
# bodyPreview is a 255-char plain-text excerpt; body would be full HTML (very large).
_MSG_SELECT = "id,subject,from,toRecipients,receivedDateTime,sentDateTime,hasAttachments,isRead,isDraft,importance,bodyPreview,webLink,conversationId,parentFolderId"

# Paths to extract via api.pick from each raw message object (force-code trigger a).
_MSG_PICK = (
    "id,subject,"
    "from.emailAddress.name,from.emailAddress.address,"
    "receivedDateTime,sentDateTime,"
    "hasAttachments,isRead,isDraft,importance,"
    "bodyPreview,webLink,conversationId,parentFolderId"
)

_FOLDER_PICK = "id,displayName,totalItemCount,unreadItemCount,isHidden,parentFolderId"


# ---------------------------------------------------------------------------
# High-level reads
# ---------------------------------------------------------------------------


def list_messages(
    user: str = "me",
    *,
    folder: str = "",
    top: int = 25,
    filter_expr: str = "",
) -> list[dict]:
    """Return recent messages from ``user``'s mailbox with support-relevant fields pre-selected.

    ``folder`` is a well-known name (``inbox``, ``sentitems``, ``drafts``, …) or a folder ID.
    If omitted, queries ``/messages`` (all mail). ``filter_expr`` is an OData ``$filter`` string.
    """
    base = f"users/{user}" if user != "me" else "me"
    path = f"{base}/mailFolders/{folder}/messages" if folder else f"{base}/messages"
    q: dict[str, Any] = {
        "$select": _MSG_SELECT,
        "$top": min(top, 100),
        "$orderby": "receivedDateTime desc",
    }
    if filter_expr:
        q["$filter"] = filter_expr
    raw = _collect_odata(path, query=q, max_items=top)
    return [api.pick(m, _MSG_PICK) for m in raw]


def search_messages(query_str: str, user: str = "me", *, top: int = 25) -> list[dict]:
    """Search messages using Graph ``$search`` (KQL) and return support-relevant fields.

    ``$search`` and ``$orderby`` cannot be combined on the messages endpoint; results are ranked
    by relevance. ``$search`` also requires Mail.Read (not just Mail.ReadBasic).
    """
    base = f"users/{user}" if user != "me" else "me"
    q: dict[str, Any] = {
        "$search": f'"{query_str}"',
        "$select": _MSG_SELECT,
        "$top": min(top, 25),  # Graph limits $search results to 25 per page
    }
    raw = _collect_odata(f"{base}/messages", query=q, max_items=top)
    return [api.pick(m, _MSG_PICK) for m in raw]


def list_folders(user: str = "me") -> list[dict]:
    """Return all top-level mail folders for ``user`` with support-relevant fields pre-selected."""
    base = f"users/{user}" if user != "me" else "me"
    raw = _collect_odata(f"{base}/mailFolders", query={"$top": 100}, max_items=200)
    return [api.pick(f, _FOLDER_PICK) for f in raw]


# ---------------------------------------------------------------------------
# Markdown rendering (concise support output)
# ---------------------------------------------------------------------------


def messages_to_markdown(messages: list[dict], title: str = "Outlook Messages") -> str:
    if not messages:
        return f"# {title}\n(no messages)"
    lines = [f"# {title}", ""]
    for m in messages:
        subject = m.get("subject") or "(no subject)"
        sender_name = m.get("from.emailAddress.name") or ""
        sender_addr = m.get("from.emailAddress.address") or ""
        sender = f"{sender_name} <{sender_addr}>" if sender_name else sender_addr
        received = (m.get("receivedDateTime") or "")[:16].replace("T", " ")
        is_read = m.get("isRead")
        read_flag = "" if is_read else " **[UNREAD]**"
        has_att = " 📎" if m.get("hasAttachments") else ""
        importance = m.get("importance") or ""
        imp_flag = f" [{importance.upper()}]" if importance and importance != "normal" else ""
        preview = (m.get("bodyPreview") or "")[:150]
        web_link = m.get("webLink") or ""
        lines.append(f"## {subject}{read_flag}{imp_flag}{has_att}")
        lines.append(f"- From: {sender}")
        lines.append(f"- Received: {received}")
        if preview:
            lines.append(f"- Preview: {preview}")
        if web_link:
            lines.append(f"- Link: {web_link}")
        lines.append("")
    return "\n".join(lines)


def folders_to_markdown(folders: list[dict]) -> str:
    if not folders:
        return "# Outlook Mail Folders\n(none found)"
    lines = ["# Outlook Mail Folders", ""]
    for f in folders:
        name = f.get("displayName") or "?"
        total = f.get("totalItemCount") or 0
        unread = f.get("unreadItemCount") or 0
        fid = f.get("id") or "?"
        hidden = " *(hidden)*" if f.get("isHidden") else ""
        lines.append(f"- **{name}**{hidden}  total={total} unread={unread}  id=`{fid}`")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.msoutlookmail",
        description="Read Microsoft Outlook Mail via Graph v1.0 (read-only).",
    )
    parser.add_argument(
        "--user", default="me", metavar="UPN",
        help="user principal name or 'me' (default: me; app token requires explicit UPN)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    msg_p = sub.add_parser("messages", help="list recent messages")
    msg_p.add_argument("--folder", default="", metavar="NAME",
                       help="well-known folder name (inbox, sentitems, drafts, …) or folder id")
    msg_p.add_argument("--top", type=int, default=25, metavar="N",
                       help="max messages to return (default 25)")
    msg_p.add_argument("--filter", default="", dest="filter_expr", metavar="EXPR",
                       help="OData $filter expression, e.g. \"isRead eq false\"")

    srch_p = sub.add_parser("search", help="search messages by keyword (KQL)")
    srch_p.add_argument("query", help="keyword search string (KQL)")
    srch_p.add_argument("--top", type=int, default=25, metavar="N",
                        help="max results (Graph caps $search pages at 25; default 25)")

    sub.add_parser("folders", help="list top-level mail folders")

    args = parser.parse_args(argv)
    user = args.user

    if args.cmd == "messages":
        msgs = list_messages(user, folder=args.folder, top=args.top, filter_expr=args.filter_expr)
        print(messages_to_markdown(msgs))
        return 0
    if args.cmd == "search":
        msgs = search_messages(args.query, user, top=args.top)
        print(messages_to_markdown(msgs, title=f"Search: {args.query}"))
        return 0
    if args.cmd == "folders":
        fldrs = list_folders(user)
        print(folders_to_markdown(fldrs))
        return 0

    parser.error("unknown command")
    return 2
