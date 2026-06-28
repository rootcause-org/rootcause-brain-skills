"""PayPal support connector — script connector over ``lib.api``.

Force-code triggers that fire:
  (a) Field pre-selection: PayPal order/subscription/dispute objects are large HATEOAS responses
      (dozens of nested fields + ``links`` arrays); the support-relevant facts are 5-8 fields.
  (b) Multi-call join: a useful support summary requires order + dispute lookup (by transaction id)
      + subscription in a single coherent view.

Auth: ``oauth2_client_credentials`` — the host mints the access token via the client_credentials
grant with client_id+secret; the workspace receives a ready bearer as ``RC_CONN_PAYPAL``.

PayPal uses page-number pagination (page=1,2,3...) not item-count offsets, so the generic
lib.api ``offset`` paginator can't express it. List calls that need to page use ``_paginate``
below, which advances the ``page`` query param manually.

CLI:
    python -m lib.connectors.paypal order 5O190127TN364715T
    python -m lib.connectors.paypal dispute PP-D-12345
    python -m lib.connectors.paypal subscription I-BW452GLLEP1G
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from lib import api

# Register the manifest so `python -m lib.api get paypal …` works for ad-hoc single-object reads.
_MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.yaml"
MANIFEST = api.register(api._parse_manifest_file(_MANIFEST_PATH))

API_BASE = "https://api-m.paypal.com"


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="paypal")


def _paginate(path: str, items_field: str, query: dict, *, page_size: int = 20) -> list[dict]:
    """Page through a PayPal list endpoint that uses page-number pagination (page=1,2,3...).

    PayPal's list endpoints use ``page`` (1-based page number) and ``page_size``; the list stops
    when a page returns fewer items than the page_size or ``total_pages`` is reached. The generic
    lib.api offset paginator can't express this (it sends item-count offsets, not page numbers).
    """
    c = _client()
    out: list[dict] = []
    page_num = 1
    while True:
        q = dict(query, page=page_num, page_size=page_size)
        body = c.get(path, query=q)
        items = body.get(items_field) or []
        out.extend(items)
        total_pages = body.get("total_pages")
        # total_pages is authoritative when present: advance until we've fetched every page.
        if total_pages is not None:
            if page_num >= total_pages:
                break
            page_num += 1
            continue
        # No total_pages: stop when the server returns a short (or empty) page.
        if not items or len(items) < page_size:
            break
        page_num += 1
    return out


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


def get_order(order_id: str) -> dict | None:
    """Fetch a single PayPal order by ID. Returns None when not found (404)."""
    order_id = (order_id or "").strip()
    if not order_id:
        raise RuntimeError("order_id is required")
    c = _client()
    try:
        return c.get(f"v2/checkout/orders/{order_id}")
    except api.ApiError as e:
        if e.status == 404:
            return None
        raise


def order_summary(order_id: str) -> dict:
    """Return support-relevant fields for a PayPal order."""
    order = get_order(order_id)
    if order is None:
        return {"found": False, "order_id": order_id}
    payer = order.get("payer") or {}
    payer_name = payer.get("name") or {}
    purchase_units = order.get("purchase_units") or []
    first_unit = purchase_units[0] if purchase_units else {}
    amount = first_unit.get("amount") or {}
    return {
        "found": True,
        "order": api.pick(
            order,
            "id,status,create_time,update_time,intent",
        ),
        "payer": {
            "email": payer.get("email_address"),
            "name": f"{payer_name.get('given_name', '')} {payer_name.get('surname', '')}".strip() or None,
            "payer_id": payer.get("payer_id"),
        },
        "amount": {
            "value": amount.get("value"),
            "currency_code": amount.get("currency_code"),
        },
    }


# ---------------------------------------------------------------------------
# Dispute
# ---------------------------------------------------------------------------


def get_dispute(dispute_id: str) -> dict | None:
    """Fetch a single PayPal dispute by ID. Returns None when not found."""
    dispute_id = (dispute_id or "").strip()
    if not dispute_id:
        raise RuntimeError("dispute_id is required")
    c = _client()
    try:
        return c.get(f"v1/customer/disputes/{dispute_id}")
    except api.ApiError as e:
        if e.status == 404:
            return None
        raise


def list_disputes(*, dispute_state: str = "") -> list[dict]:
    """List disputes, optionally filtered by state (REQUIRED_ACTION, UNDER_PAYPAL_REVIEW, etc.)."""
    q: dict[str, Any] = {"page_size": 20}
    if dispute_state:
        q["dispute_state"] = dispute_state
    return _paginate("v1/customer/disputes", "items", q, page_size=20)


def dispute_summary(dispute_id: str) -> dict:
    """Return support-relevant fields for a PayPal dispute."""
    dispute = get_dispute(dispute_id)
    if dispute is None:
        return {"found": False, "dispute_id": dispute_id}
    outcome = dispute.get("dispute_outcome") or {}
    txns = dispute.get("disputed_transactions") or []
    first_txn = txns[0] if txns else {}
    return {
        "found": True,
        "dispute": api.pick(
            dispute,
            "dispute_id,reason,status,dispute_state,dispute_life_cycle_stage,create_time,update_time",
        ),
        "amount": api.pick(dispute.get("dispute_amount") or {}, "value,currency_code"),
        "outcome": {
            "code": outcome.get("outcome_code"),
            "refunded": api.pick(outcome.get("amount_refunded") or {}, "value,currency_code"),
        },
        "transaction_id": first_txn.get("buyer_transaction_id") or first_txn.get("seller_transaction_id"),
    }


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------


def get_subscription(subscription_id: str) -> dict | None:
    """Fetch a single PayPal subscription by ID. Returns None when not found."""
    subscription_id = (subscription_id or "").strip()
    if not subscription_id:
        raise RuntimeError("subscription_id is required")
    c = _client()
    try:
        return c.get(f"v1/billing/subscriptions/{subscription_id}")
    except api.ApiError as e:
        if e.status == 404:
            return None
        raise


def subscription_summary(subscription_id: str) -> dict:
    """Return support-relevant fields for a PayPal subscription."""
    sub = get_subscription(subscription_id)
    if sub is None:
        return {"found": False, "subscription_id": subscription_id}
    subscriber = sub.get("subscriber") or {}
    sub_name = subscriber.get("name") or {}
    billing_info = sub.get("billing_info") or {}
    last_payment = billing_info.get("last_payment") or {}
    next_billing = billing_info.get("next_billing_time")
    return {
        "found": True,
        "subscription": api.pick(
            sub,
            "id,plan_id,status,quantity,create_time,update_time,start_time",
        ),
        "subscriber": {
            "email": subscriber.get("email_address"),
            "name": f"{sub_name.get('given_name', '')} {sub_name.get('surname', '')}".strip() or None,
            "payer_id": subscriber.get("payer_id"),
        },
        "billing": {
            "next_billing_time": next_billing,
            "last_payment_amount": api.pick(last_payment.get("amount") or {}, "value,currency_code"),
            "last_payment_time": last_payment.get("time"),
            "failed_payments_count": billing_info.get("failed_payments_count"),
            "outstanding_balance": api.pick(
                billing_info.get("outstanding_balance") or {}, "value,currency_code"
            ),
        },
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def order_to_markdown(s: dict) -> str:
    if not s.get("found"):
        return f"# PayPal order not found\nNo order matched `{s.get('order_id', '')}`."
    o = s["order"]
    p = s["payer"]
    a = s["amount"]
    lines = [f"# PayPal Order: {o.get('id')}"]
    lines.append(f"- Status: **{o.get('status', 'unknown')}**")
    lines.append(f"- Intent: {o.get('intent', '—')}")
    lines.append(f"- Created: {o.get('create_time', '—')}")
    if a.get("value"):
        lines.append(f"- Amount: {a['value']} {a.get('currency_code', '')}")
    lines.append("\n## Payer")
    if p.get("email"):
        lines.append(f"- Email: {p['email']}")
    if p.get("name"):
        lines.append(f"- Name: {p['name']}")
    if p.get("payer_id"):
        lines.append(f"- Payer ID: `{p['payer_id']}`")
    return "\n".join(lines)


def dispute_to_markdown(s: dict) -> str:
    if not s.get("found"):
        return f"# PayPal dispute not found\nNo dispute matched `{s.get('dispute_id', '')}`."
    d = s["dispute"]
    a = s.get("amount") or {}
    o = s.get("outcome") or {}
    lines = [f"# PayPal Dispute: {d.get('dispute_id')}"]
    lines.append(f"- Reason: **{d.get('reason', 'unknown')}**")
    lines.append(f"- Status: **{d.get('status', 'unknown')}**")
    lines.append(f"- Stage: {d.get('dispute_life_cycle_stage', '—')}")
    lines.append(f"- Created: {d.get('create_time', '—')}")
    if a.get("value"):
        lines.append(f"- Dispute amount: {a['value']} {a.get('currency_code', '')}")
    if o.get("code"):
        lines.append(f"- Outcome: {o['code']}")
    if s.get("transaction_id"):
        lines.append(f"- Transaction ID: `{s['transaction_id']}`")
    return "\n".join(lines)


def subscription_to_markdown(s: dict) -> str:
    if not s.get("found"):
        return f"# PayPal subscription not found\nNo subscription matched `{s.get('subscription_id', '')}`."
    sub = s["subscription"]
    sub_email = s["subscriber"].get("email")
    billing = s.get("billing") or {}
    lines = [f"# PayPal Subscription: {sub.get('id')}"]
    lines.append(f"- Status: **{sub.get('status', 'unknown')}**")
    lines.append(f"- Plan ID: `{sub.get('plan_id', '—')}`")
    if sub_email:
        lines.append(f"- Subscriber: {sub_email}")
    if s["subscriber"].get("name"):
        lines.append(f"- Name: {s['subscriber']['name']}")
    lines.append(f"- Started: {sub.get('start_time', '—')}")
    if billing.get("next_billing_time"):
        lines.append(f"- Next billing: {billing['next_billing_time']}")
    last_amt = billing.get("last_payment_amount") or {}
    if last_amt.get("value"):
        lines.append(f"- Last payment: {last_amt['value']} {last_amt.get('currency_code', '')}")
    failed = billing.get("failed_payments_count")
    if isinstance(failed, int) and failed > 0:
        lines.append(f"- **Failed payments**: {failed}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.paypal")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ord_p = sub.add_parser("order", help="show support summary for a PayPal order")
    ord_p.add_argument("order_id", help="PayPal order ID (e.g. 5O190127TN364715T)")

    disp_p = sub.add_parser("dispute", help="show support summary for a PayPal dispute")
    disp_p.add_argument("dispute_id", help="PayPal dispute ID (e.g. PP-D-12345)")

    sub_p = sub.add_parser("subscription", help="show support summary for a PayPal subscription")
    sub_p.add_argument("subscription_id", help="PayPal subscription ID (e.g. I-BW452GLLEP1G)")

    args = parser.parse_args(argv)

    if args.cmd == "order":
        print(order_to_markdown(order_summary(args.order_id)))
        return 0
    if args.cmd == "dispute":
        print(dispute_to_markdown(dispute_summary(args.dispute_id)))
        return 0
    if args.cmd == "subscription":
        print(subscription_to_markdown(subscription_summary(args.subscription_id)))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
