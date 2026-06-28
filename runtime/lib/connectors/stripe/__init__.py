"""Stripe support connector — the reference "needs a script" connector over ``lib.api``.

This is the canonical force-code case: answering "what's going on with this customer's billing?"
takes a MULTI-CALL JOIN (customer → subscription → latest invoice → last failed charge) and FIELD
PRE-SELECTION (a raw Stripe customer/invoice is enormous; the support-relevant facts are a handful
of fields). So instead of the agent piping ``python -m lib.api get stripe ...`` five times and
sifting JSON, this connector does the join and renders ONE concise markdown block.

It deliberately uses ``lib.api`` (not the Stripe SDK) to exercise the shared client: bearer auth
with the restricted ``rk_`` key injected as ``RC_CONN_STRIPE``, cursor pagination, retry/backoff.
Read-only by the key's restriction AND by only ever issuing GETs. Validate against Stripe TEST mode.

CLI:
    python -m lib.connectors.stripe customer cus_123
    python -m lib.connectors.stripe customer founder@example.com   # resolved by email
"""

from __future__ import annotations

import argparse
from typing import Any

from lib import api

API_BASE = "https://api.stripe.com/v1"

# Stripe is bearer-auth (the rk_ restricted key) with cursor pagination via starting_after over the
# opaque last object id; list bodies carry the page under "data" and "has_more". One manifest row.
MANIFEST = api.register(
    api.Manifest(
        key="stripe",
        base_url=API_BASE,
        auth=api.Auth(strategy="bearer"),
        pagination=api.Pagination(
            style="cursor",
            # Stripe's cursor is the id of the LAST item in `data` (sent as starting_after) — the
            # generic cursor_field can't express "last element id", so list calls page manually via
            # _stripe_next/fetch_page below. Left empty so the generic lib.api paginator doesn't
            # silently stop after page 1 thinking there's no cursor.
            cursor_field="",
            cursor_param="starting_after",
            has_more_field="has_more",
            items_field="data",
            page_size=100,
        ),
        rate_limit_remaining_header="",  # Stripe uses 429 + Retry-After, handled by lib.api
    )
)


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="stripe")


def _stripe_next(body: dict) -> Any | None:
    """Stripe cursor = the id of the LAST object in ``data`` (sent as ``starting_after``), gated by
    ``has_more``. The generic cursor_field can't express "last element id", so list calls compute
    the next token here and page manually with ``fetch_page``."""
    if not body.get("has_more"):
        return None
    data = body.get("data") or []
    return data[-1]["id"] if data else None


def _list(path: str, query: dict, *, limit_items: int) -> list[dict]:
    """Paginate a Stripe list endpoint up to ``limit_items``, following ``starting_after``."""
    c = _client()
    out: list[dict] = []
    q = dict(query, limit=min(limit_items, 100))
    while len(out) < limit_items:
        page = c.fetch_page(path, query=q)
        out.extend(page.items)
        nxt = _stripe_next(page.body)
        if nxt is None:
            break
        q = dict(q, starting_after=nxt)
    return out[:limit_items]


def resolve_customer(ref: str) -> dict | None:
    """Resolve a customer by id (``cus_…``) or email. Email uses Stripe's search; falls back to the
    list+filter on accounts where search isn't enabled."""
    ref = (ref or "").strip()
    if not ref:
        raise RuntimeError("customer reference (id or email) is required")
    c = _client()
    if ref.startswith("cus_"):
        return c.get(f"customers/{ref}")
    # Email path: the list endpoint filters by email directly (no search index needed).
    page = c.fetch_page("customers", query={"email": ref, "limit": 1})
    return page.items[0] if page.items else None


def support_summary(ref: str) -> dict:
    """Join the support-relevant billing facts for a customer into a compact dict.

    Multi-call: customer → its subscriptions → latest invoice → most recent failed charge. Each call
    pre-selects only the fields support needs, so the result is small and stable.
    """
    customer = resolve_customer(ref)
    if customer is None:
        return {"found": False, "ref": ref}
    cid = customer["id"]

    subs = _list("subscriptions", {"customer": cid, "status": "all"}, limit_items=10)
    sub = subs[0] if subs else None

    invoices = _list("invoices", {"customer": cid}, limit_items=1)
    invoice = invoices[0] if invoices else None

    # A failed payment shows up as a charge with status "failed"; pull the most recent.
    failed = None
    charges = _list("charges", {"customer": cid}, limit_items=10)
    for ch in charges:
        if ch.get("status") == "failed" or ch.get("paid") is False:
            failed = ch
            break

    return {
        "found": True,
        "customer": api.pick(customer, "id,email,name,delinquent,balance,currency"),
        "subscription": api.pick(sub, "id,status,current_period_end,cancel_at_period_end,plan.id,plan.nickname,plan.amount,plan.currency,plan.interval") if sub else None,
        "latest_invoice": api.pick(invoice, "id,number,status,amount_due,amount_paid,currency,created,hosted_invoice_url") if invoice else None,
        "last_failed_charge": api.pick(failed, "id,amount,currency,created,failure_code,failure_message,outcome.seller_message") if failed else None,
    }


def _money(amount: Any, currency: Any) -> str:
    """Render a Stripe minor-unit amount (cents) as a readable "12.50 USD"; pass non-ints through."""
    if not isinstance(amount, int):
        return str(amount)
    cur = (currency or "").upper()
    return f"{amount / 100:.2f} {cur}".strip()


def summary_to_markdown(s: dict) -> str:
    """Render the support summary as concise grounding markdown."""
    if not s.get("found"):
        return f"# Stripe customer not found\nNo customer matched `{s.get('ref', '')}`."

    cust = s["customer"]
    lines = [f"# Stripe: {cust.get('email') or cust.get('id')}"]
    lines.append(f"- Customer: `{cust.get('id')}`" + (f" — {cust['name']}" if cust.get("name") else ""))
    if cust.get("delinquent"):
        lines.append("- **Delinquent**: yes")
    bal = cust.get("balance")
    if isinstance(bal, int) and bal != 0:
        # Positive balance = the customer owes (will be added to next invoice); negative = credit.
        lines.append(f"- Account balance: {_money(bal, cust.get('currency'))}")

    sub = s.get("subscription")
    lines.append("\n## Subscription")
    if sub:
        plan_label = sub.get("plan.nickname") or sub.get("plan.id") or "—"
        price = _money(sub.get("plan.amount"), sub.get("plan.currency"))
        interval = sub.get("plan.interval")
        per = f"/{interval}" if interval else ""
        lines.append(f"- Status: **{sub.get('status', 'unknown')}**")
        lines.append(f"- Plan: {plan_label} ({price}{per})")
        if sub.get("cancel_at_period_end"):
            lines.append("- Cancels at period end: yes")
    else:
        lines.append("- (no subscription)")

    inv = s.get("latest_invoice")
    lines.append("\n## Latest invoice")
    if inv:
        lines.append(f"- {inv.get('number') or inv.get('id')}: **{inv.get('status', 'unknown')}**")
        lines.append(f"- Amount due: {_money(inv.get('amount_due'), inv.get('currency'))}, paid: {_money(inv.get('amount_paid'), inv.get('currency'))}")
        if inv.get("hosted_invoice_url"):
            lines.append(f"- Link: {inv['hosted_invoice_url']}")
    else:
        lines.append("- (no invoices)")

    failed = s.get("last_failed_charge")
    if failed:
        lines.append("\n## Last failed payment")
        lines.append(f"- {_money(failed.get('amount'), failed.get('currency'))}")
        reason = failed.get("failure_message") or failed.get("outcome.seller_message") or failed.get("failure_code")
        if reason:
            lines.append(f"- Reason: {reason}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.stripe")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cust = sub.add_parser("customer", help="render a customer's support billing summary")
    cust.add_argument("ref", help="customer id (cus_…) or email")
    args = parser.parse_args(argv)

    if args.cmd == "customer":
        print(summary_to_markdown(support_summary(args.ref)))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
