"""Help Scout Mailbox API v2 connector — reads conversations, threads, customers, mailboxes.

Force-code trigger: pagination is page-number based (``?page=N``) with termination signalled
by absence of ``_links.next`` in the response BODY (HAL+JSON). lib.api's ``link`` style follows
RFC 8288 ``Link:`` *headers*; ``offset`` increments by item count, not page number. Neither fits,
so this connector hand-rolls the page loop using ``fetch_page`` + ``_links``/``page`` metadata.

Auth: OAuth 2.0 client-credentials; the host mints a bearer and injects it as ``RC_CONN_HELPSCOUT``.
Tokens are valid ~48 h; lib.api presents the injected bearer via ``oauth2_client_credentials``.

Read-only: only GET requests, never mutates customer data.

CLI:
    python -m lib.connectors.helpscout conversations [--status open|closed|all] [--mailbox ID]
    python -m lib.connectors.helpscout conversation <id>
    python -m lib.connectors.helpscout customer <email-or-id>
    python -m lib.connectors.helpscout mailboxes
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from lib import api

# Load the manifest from the sibling YAML so the Manifest dataclass is the single source of truth.
_MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.yaml"
MANIFEST = api.register(api._parse_manifest_file(_MANIFEST_PATH))

# Page size for list endpoints — Help Scout defaults: 25 for conversations, 50 for customers.
_PAGE_SIZE = 25
_MAX_PAGES = 200  # hard ceiling against runaway loops


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="helpscout")


def _paginate_list(path: str, *, query: dict | None = None, max_items: int = 500) -> list[dict]:
    """Page through a Help Scout list endpoint that uses ``?page=N`` / ``_links.next`` in body.

    Returns up to ``max_items`` items. Stops when ``_links.next`` is absent (server-side exhaustion)
    or when we hit our internal ceiling.  Raises ``ApiError`` if any page fails — caller decides
    whether to surface partial results.
    """
    c = _client()
    out: list[dict] = []
    q = dict(query or {})
    q.setdefault("page", 1)

    for _ in range(_MAX_PAGES):
        page = c.fetch_page(path, query=q)
        # Help Scout wraps items under _embedded.<resource> — items_field="" means body IS items,
        # but the body is a dict with _embedded. We extract manually.
        body = page.body
        items = _extract_items(body)
        out.extend(items)
        if len(out) >= max_items:
            out = out[:max_items]
            break
        # Advance only if _links.next is present in the response body.
        links = body.get("_links") if isinstance(body, dict) else None
        if not links or not links.get("next"):
            break
        # Increment page number for the next request.
        q = dict(q, page=int(q.get("page", 1)) + 1)

    return out


def _extract_items(body: Any) -> list[dict]:
    """Extract the item list from a HAL+JSON body (``_embedded.<resource>``).

    Help Scout wraps every list inside ``_embedded`` under a resource-specific key (conversations,
    customers, mailboxes, threads, tags). We pull the first list value we find.
    """
    if not isinstance(body, dict):
        return []
    embedded = body.get("_embedded")
    if not isinstance(embedded, dict):
        return []
    for val in embedded.values():
        if isinstance(val, list):
            return val
    return []


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

_CONV_PICK = "id,number,subject,status,mailboxId,assignee.email,customer.email,createdAt,closedAt,tags"
_CUSTOMER_PICK = "id,firstName,lastName,email,organization,jobTitle,conversationCount,createdAt"


def list_conversations(
    *,
    status: str = "active",
    mailbox: int | None = None,
    max_items: int = 50,
) -> list[dict]:
    """Fetch recent conversations, pre-selecting support-relevant fields."""
    q: dict[str, Any] = {"status": status, "sortField": "modifiedAt", "sortOrder": "desc"}
    if mailbox:
        q["mailbox"] = mailbox
    convs = _paginate_list("conversations", query=q, max_items=max_items)
    return [api.pick(c, _CONV_PICK) for c in convs]


def get_conversation(conversation_id: int | str) -> dict:
    """Fetch one conversation (no threads) pre-selected to support-relevant fields."""
    c = _client()
    body = c.get(f"conversations/{conversation_id}")
    return api.pick(body, _CONV_PICK + ",threads")


def list_threads(conversation_id: int | str, *, max_items: int = 100) -> list[dict]:
    """Fetch the reply/note threads for a conversation, newest-first (API default)."""
    threads = _paginate_list(
        f"conversations/{conversation_id}/threads", max_items=max_items
    )
    _THREAD_PICK = "id,type,status,body,author.email,customer.email,createdAt,openedAt"
    return [api.pick(t, _THREAD_PICK) for t in threads]


def resolve_customer(ref: str) -> dict | None:
    """Resolve a customer by numeric id or email address."""
    ref = (ref or "").strip()
    if not ref:
        raise RuntimeError("customer reference (email or id) is required")
    c = _client()
    if ref.isdigit():
        body = c.get(f"customers/{ref}")
        return api.pick(body, _CUSTOMER_PICK)
    # Email search via the list endpoint.
    page = c.fetch_page("customers", query={"email": ref})
    items = _extract_items(page.body)
    return api.pick(items[0], _CUSTOMER_PICK) if items else None


def list_mailboxes() -> list[dict]:
    """Fetch all mailboxes (inboxes) visible to the token."""
    c = _client()
    page = c.fetch_page("mailboxes")
    items = _extract_items(page.body)
    return [api.pick(m, "id,name,email,slug,createdAt,updatedAt") for m in items]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _conversation_to_md(convs: list[dict], heading: str) -> str:
    if not convs:
        return f"# Help Scout: {heading}\n_(no conversations found)_"
    lines = [f"# Help Scout: {heading}", f"_{len(convs)} conversation(s)_\n"]
    for c in convs:
        subj = c.get("subject") or "(no subject)"
        status = c.get("status", "?")
        cust = (c.get("customer") or {}).get("email") or "?"
        assignee = (c.get("assignee") or {}).get("email") or "unassigned"
        tags = ", ".join(c.get("tags") or []) or "—"
        lines.append(f"## #{c.get('number')} — {subj}")
        lines.append(f"- Status: **{status}** | Customer: {cust} | Assignee: {assignee}")
        lines.append(f"- Tags: {tags} | Created: {c.get('createdAt', '?')}")
    return "\n".join(lines)


def _threads_to_md(threads: list[dict], conv_id: str) -> str:
    if not threads:
        return f"# Help Scout conversation {conv_id}\n_(no threads)_"
    lines = [f"# Help Scout conversation {conv_id}", f"_{len(threads)} thread(s)_\n"]
    for t in threads:
        ttype = t.get("type", "?")
        author = (t.get("author") or {}).get("email") or (t.get("customer") or {}).get("email") or "?"
        lines.append(f"## [{ttype}] {t.get('createdAt', '?')} — {author}")
        body = (t.get("body") or "").strip()
        if body:
            # Truncate very long thread bodies for context efficiency.
            preview = body[:1000] + ("…" if len(body) > 1000 else "")
            lines.append(preview)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import json

    parser = argparse.ArgumentParser(prog="python -m lib.connectors.helpscout")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_convs = sub.add_parser("conversations", help="list recent conversations")
    p_convs.add_argument("--status", default="active", help="active|open|closed|pending|spam|all")
    p_convs.add_argument("--mailbox", type=int, default=None, help="filter by mailbox ID")
    p_convs.add_argument("--max-items", type=int, default=50)
    p_convs.add_argument("--json", action="store_true", dest="as_json", help="output raw JSON")

    p_conv = sub.add_parser("conversation", help="get one conversation + threads")
    p_conv.add_argument("id", help="conversation ID")
    p_conv.add_argument("--json", action="store_true", dest="as_json")

    p_cust = sub.add_parser("customer", help="resolve a customer by email or id")
    p_cust.add_argument("ref", help="email address or numeric customer id")
    p_cust.add_argument("--json", action="store_true", dest="as_json")

    p_mb = sub.add_parser("mailboxes", help="list all mailboxes")
    p_mb.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args(argv)

    if args.cmd == "conversations":
        convs = list_conversations(
            status=args.status, mailbox=args.mailbox, max_items=args.max_items
        )
        if args.as_json:
            print(json.dumps(convs, indent=2, default=str))
        else:
            label = args.status + (f" mailbox={args.mailbox}" if args.mailbox else "")
            print(_conversation_to_md(convs, label))
        return 0

    if args.cmd == "conversation":
        conv = get_conversation(args.id)
        threads = list_threads(args.id)
        if args.as_json:
            print(json.dumps({"conversation": conv, "threads": threads}, indent=2, default=str))
        else:
            print(_threads_to_md(threads, str(args.id)))
        return 0

    if args.cmd == "customer":
        cust = resolve_customer(args.ref)
        if args.as_json:
            print(json.dumps(cust, indent=2, default=str))
        else:
            if cust is None:
                print(f"# Help Scout: customer not found\nNo customer matched `{args.ref}`.")
            else:
                name = f"{cust.get('firstName', '')} {cust.get('lastName', '')}".strip()
                print(f"# Help Scout customer: {name or args.ref}")
                for k, v in cust.items():
                    print(f"- {k}: {v}")
        return 0

    if args.cmd == "mailboxes":
        mailboxes = list_mailboxes()
        if args.as_json:
            print(json.dumps(mailboxes, indent=2, default=str))
        else:
            lines = ["# Help Scout mailboxes"]
            for m in mailboxes:
                lines.append(f"- **{m.get('name')}** (`{m.get('id')}`) — {m.get('email')}")
            print("\n".join(lines))
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
