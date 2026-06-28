"""Resend support connector.

Force-code trigger (d): Resend cursor pagination uses the **last item's id** as the next ``after``
param — there is no scalar next-cursor field in the response body, so ``lib.api``'s cursor_field
can't express it. Pattern mirrors the Stripe connector: ``cursor_field=""`` in the manifest,
manual next-token derivation here.

Read-only (GET only). Token: ``RC_CONN_RESEND`` (bearer ``re_…``).

CLI:
    python -m lib.connectors.resend emails            # recent sent emails with delivery status
    python -m lib.connectors.resend emails --limit 50
    python -m lib.connectors.resend email <id>        # single email with full detail
    python -m lib.connectors.resend domains           # sending domain list + status
"""

from __future__ import annotations

import argparse
from typing import Any

from lib import api

API_BASE = "https://api.resend.com"

# Bearer auth, cursor pagination (last item id → after), items under "data", has_more gate.
# cursor_field is intentionally empty — see module docstring and _resend_next().
MANIFEST = api.register(
    api.Manifest(
        key="resend",
        base_url=API_BASE,
        auth=api.Auth(strategy="bearer"),
        pagination=api.Pagination(
            style="cursor",
            cursor_field="",         # no scalar cursor in the body; script derives from data[-1].id
            cursor_param="after",
            has_more_field="has_more",
            items_field="data",
            page_size=100,
        ),
        rate_limit_remaining_header="",  # Resend uses 429 + Retry-After; no remaining header
    )
)


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="resend")


def _resend_next(body: dict) -> Any | None:
    """Derive the next cursor from the last item's id — gated by ``has_more``.

    Resend emits no scalar next-cursor field; the cursor for the next page is the ``id`` of the
    final item in ``data``. Mirrors the Stripe connector's ``_stripe_next`` pattern.
    """
    if not body.get("has_more"):
        return None
    data = body.get("data") or []
    return data[-1]["id"] if data else None


def _list(path: str, query: dict, *, limit_items: int) -> list[dict]:
    """Page a Resend list endpoint up to ``limit_items`` items, following the after-cursor."""
    c = _client()
    out: list[dict] = []
    q = dict(query, limit=min(limit_items, 100))
    while len(out) < limit_items:
        page = c.fetch_page(path, query=q)
        out.extend(page.items)
        nxt = _resend_next(page.body)
        if nxt is None:
            break
        q = dict(q, after=nxt)
    return out[:limit_items]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def list_emails(limit: int = 50) -> list[dict]:
    """Fetch recent sent emails, trimmed to support-relevant fields."""
    raw = _list("/emails", {}, limit_items=limit)
    return [
        api.pick(e, "id,to,from,subject,last_event,created_at,scheduled_at")
        for e in raw
    ]


def get_email(email_id: str) -> dict:
    """Fetch a single sent email by id — includes body and tags."""
    c = _client()
    raw = c.get(f"/emails/{email_id}")
    return api.pick(raw, "id,to,from,subject,last_event,created_at,html,text,tags,bcc,cc,reply_to")


def list_domains() -> list[dict]:
    """Fetch all sending domains with their verification status."""
    raw = _list("/domains", {}, limit_items=200)
    return [
        api.pick(d, "id,name,status,region,created_at")
        for d in raw
    ]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def emails_to_markdown(rows: list[dict]) -> str:
    if not rows:
        return "# Resend emails\n\n(no sent emails found)"
    lines = ["# Resend sent emails\n", "| id | to | subject | status | sent_at |",
             "|----|----|---------|---------|---------| "]
    for e in rows:
        to = ", ".join(e.get("to") or []) if isinstance(e.get("to"), list) else (e.get("to") or "—")
        lines.append(
            f"| `{e.get('id', '—')}` "
            f"| {to} "
            f"| {e.get('subject', '—')} "
            f"| **{e.get('last_event', '—')}** "
            f"| {(e.get('created_at') or '—')[:19]} |"
        )
    return "\n".join(lines)


def email_to_markdown(e: dict) -> str:
    to = ", ".join(e.get("to") or []) if isinstance(e.get("to"), list) else (e.get("to") or "—")
    lines = [
        f"# Resend email `{e.get('id', '—')}`",
        f"- **To**: {to}",
        f"- **From**: {e.get('from', '—')}",
        f"- **Subject**: {e.get('subject', '—')}",
        f"- **Status**: **{e.get('last_event', '—')}**",
        f"- **Sent at**: {(e.get('created_at') or '—')[:19]}",
    ]
    tags = e.get("tags")
    if tags:
        tag_str = ", ".join(f"{t['name']}={t['value']}" for t in tags if isinstance(t, dict))
        if tag_str:
            lines.append(f"- **Tags**: {tag_str}")
    return "\n".join(lines)


def domains_to_markdown(rows: list[dict]) -> str:
    if not rows:
        return "# Resend domains\n\n(no domains found)"
    lines = ["# Resend sending domains\n", "| domain | status | region | id |",
             "|--------|--------|--------|----|"]
    for d in rows:
        lines.append(
            f"| {d.get('name', '—')} "
            f"| **{d.get('status', '—')}** "
            f"| {d.get('region', '—')} "
            f"| `{d.get('id', '—')}` |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.resend")
    sub = parser.add_subparsers(dest="cmd", required=True)

    lst = sub.add_parser("emails", help="list recent sent emails with delivery status")
    lst.add_argument("--limit", type=int, default=50, help="max emails to retrieve (default 50)")

    em = sub.add_parser("email", help="get a single email by id")
    em.add_argument("id", help="email id")

    sub.add_parser("domains", help="list sending domains and their status")

    args = parser.parse_args(argv)

    if args.cmd == "emails":
        print(emails_to_markdown(list_emails(limit=args.limit)))
    elif args.cmd == "email":
        print(email_to_markdown(get_email(args.id)))
    elif args.cmd == "domains":
        print(domains_to_markdown(list_domains()))
    else:
        parser.error("unknown command")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
