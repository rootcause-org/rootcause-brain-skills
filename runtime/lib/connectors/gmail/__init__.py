"""Gmail connector — read-only grounding for a Gmail mailbox.

Force-code triggers:
  (a) Field pre-selection: messages.get returns a deeply-nested MIME payload; only
      subject/from/date/snippet/labelIds/body-excerpt are support-relevant.
  (b) Multi-call join: messages.list returns only {id, threadId}; the script fetches
      messages.get per ID so the agent gets meaningful content in one command.

All reads use ``lib.api`` for auth/retry/rate-limit. The connector never writes to Gmail.

Importable functions: ``list_messages``, ``get_message``, ``get_thread``, ``list_labels``.
CLI (``python -m lib.connectors.gmail``): prints concise markdown, see ``--help``.
"""

from __future__ import annotations

import base64
from typing import Any

from lib import api as _api

# ---------------------------------------------------------------------------
# Manifest — registered so `python -m lib.api get gmail …` works too
# ---------------------------------------------------------------------------

_MANIFEST = _api.load_manifests().get("gmail")
if _MANIFEST is None:
    # Fallback: build inline (shouldn't happen when the package is installed correctly)
    _MANIFEST = _api.Manifest(
        key="gmail",
        base_url="https://gmail.googleapis.com/gmail/v1",
        auth=_api.Auth(strategy="bearer"),
        pagination=_api.Pagination(
            style="cursor",
            cursor_field="nextPageToken",
            cursor_param="pageToken",
            items_field="messages",
            page_size=100,
        ),
    )
    _api.register(_MANIFEST)


def _client() -> _api.Client:
    return _api.client(_MANIFEST)


# ---------------------------------------------------------------------------
# Support-relevant field extraction
# ---------------------------------------------------------------------------

_HEADER_NAMES = {"subject", "from", "to", "date", "cc"}


def _extract_headers(payload: dict) -> dict[str, str]:
    """Pick the few named headers from a message payload."""
    out: dict[str, str] = {}
    for h in payload.get("headers") or []:
        name = (h.get("name") or "").lower()
        if name in _HEADER_NAMES:
            out[name] = h.get("value") or ""
    return out


def _extract_body(payload: dict, *, max_chars: int = 500) -> str:
    """Walk the MIME tree and return the first readable text part (plain > html), truncated.

    Gmail nests multipart bodies arbitrarily deep; we walk breadth-first to find text/plain
    first, then text/html as a fallback. Returns "" when no text part is found.
    """
    queue: list[dict] = [payload]
    plain: str = ""
    html: str = ""
    while queue and not plain:
        node = queue.pop(0)
        mime = (node.get("mimeType") or "").lower()
        data = (node.get("body") or {}).get("data") or ""
        if data:
            try:
                text = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                text = ""
            if mime == "text/plain":
                plain = text
                break
            if mime == "text/html" and not html:
                html = text
        queue.extend(node.get("parts") or [])
    body = plain or html
    if not body:
        return ""
    body = body.strip()
    if len(body) > max_chars:
        body = body[:max_chars] + "…"
    return body


def _shape_message(raw: dict) -> dict:
    """Reduce a raw messages.get response to support-relevant fields."""
    payload = raw.get("payload") or {}
    headers = _extract_headers(payload)
    body_excerpt = _extract_body(payload)
    return {
        "id": raw.get("id", ""),
        "threadId": raw.get("threadId", ""),
        "labelIds": raw.get("labelIds") or [],
        "snippet": raw.get("snippet", ""),
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "date": headers.get("date", ""),
        "body_excerpt": body_excerpt,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_messages(
    *,
    q: str = "",
    limit: int = 20,
    label_ids: list[str] | None = None,
    user_id: str = "me",
) -> list[dict]:
    """List + hydrate messages: calls messages.list then messages.get for each result.

    Returns a list of shaped message dicts (subject, from, date, snippet, body_excerpt, …).
    ``limit`` caps the total number of hydrated messages (not just the first page).
    """
    c = _client()
    query: dict[str, Any] = {"maxResults": min(limit, 500)}
    if q:
        query["q"] = q
    if label_ids:
        query["labelIds"] = label_ids

    result = c.collect(
        f"users/{user_id}/messages",
        query=query,
        max_items=limit,
    )
    ids = [m["id"] for m in result["items"] if "id" in m]
    messages = [get_message(mid, user_id=user_id) for mid in ids]
    return messages


def get_message(message_id: str, *, user_id: str = "me") -> dict:
    """Fetch and shape one message by ID (full payload, support-relevant fields only)."""
    c = _client()
    raw = c.get(f"users/{user_id}/messages/{message_id}", query={"format": "full"})
    return _shape_message(raw)


def get_thread(thread_id: str, *, user_id: str = "me") -> dict:
    """Fetch a thread and shape each of its messages.

    Returns ``{"id": …, "messages": [shaped_message, …]}``.
    """
    c = _client()
    raw = c.get(f"users/{user_id}/threads/{thread_id}")
    shaped = [_shape_message(m) for m in (raw.get("messages") or [])]
    return {"id": thread_id, "messages": shaped}


def list_labels(*, user_id: str = "me") -> list[dict]:
    """List all labels in the mailbox. Returns id, name, type for each."""
    c = _client()
    raw = c.get(f"users/{user_id}/labels")
    labels = raw.get("labels") or []
    return [{"id": lb.get("id", ""), "name": lb.get("name", ""), "type": lb.get("type", "")} for lb in labels]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _render_message(m: dict) -> str:
    lines = [
        f"**{m['subject'] or '(no subject)'}**",
        f"From: {m['from']}  |  Date: {m['date']}",
        f"To: {m['to']}",
        f"Labels: {', '.join(m['labelIds']) or '—'}",
        f"Snippet: {m['snippet']}",
    ]
    if m["body_excerpt"]:
        lines.append(f"\n```\n{m['body_excerpt']}\n```")
    lines.append(f"ID: `{m['id']}` · Thread: `{m['threadId']}`")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    p = argparse.ArgumentParser(prog="python -m lib.connectors.gmail", description="Gmail read-only grounding connector")
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("messages", help="List + hydrate recent messages")
    pm.add_argument("--query", default="", help="Gmail search query (e.g. 'is:unread')")
    pm.add_argument("--limit", type=int, default=20, help="Max messages to fetch (default 20)")
    pm.add_argument("--label", action="append", dest="labels", default=[], metavar="LABEL_ID")
    pm.add_argument("--user", default="me", help="Mailbox user ID (default: me)")
    pm.add_argument("--json", action="store_true", dest="as_json")

    pg = sub.add_parser("message", help="Get one message by ID")
    pg.add_argument("id", help="Message ID")
    pg.add_argument("--user", default="me")
    pg.add_argument("--json", action="store_true", dest="as_json")

    pt = sub.add_parser("thread", help="Get all messages in a thread")
    pt.add_argument("id", help="Thread ID")
    pt.add_argument("--user", default="me")
    pt.add_argument("--json", action="store_true", dest="as_json")

    pl = sub.add_parser("labels", help="List all mailbox labels")
    pl.add_argument("--user", default="me")

    args = p.parse_args(argv)

    if args.cmd == "messages":
        msgs = list_messages(q=args.query, limit=args.limit, label_ids=args.labels or None, user_id=args.user)
        if args.as_json:
            print(json.dumps(msgs, indent=2, default=str))
        else:
            print(f"# Gmail messages ({len(msgs)})\n")
            for m in msgs:
                print(_render_message(m))
                print()

    elif args.cmd == "message":
        m = get_message(args.id, user_id=args.user)
        if args.as_json:
            print(json.dumps(m, indent=2, default=str))
        else:
            print(_render_message(m))

    elif args.cmd == "thread":
        t = get_thread(args.id, user_id=args.user)
        if args.as_json:
            print(json.dumps(t, indent=2, default=str))
        else:
            print(f"# Thread `{t['id']}` — {len(t['messages'])} message(s)\n")
            for m in t["messages"]:
                print(_render_message(m))
                print("---")

    elif args.cmd == "labels":
        labels = list_labels(user_id=args.user)
        print("# Gmail labels\n")
        for lb in labels:
            print(f"- `{lb['id']}` **{lb['name']}** ({lb['type']})")

    return 0
