"""Recurly support connector — script connector over ``lib.api``.

Force-code triggers:
  (a) Field pre-selection — Recurly account/subscription/invoice objects are large; the
      support-relevant facts are a handful of fields.
  (d) Non-standard pagination — Recurly v3 returns ``has_more`` (bool) + ``next`` (relative
      path string, e.g. ``/accounts?cursor=abc123&limit=200``) inside the JSON response body.
      Neither lib.api's ``cursor`` style (extracts an opaque token to re-send as a query param)
      nor ``link`` style (reads an HTTP ``Link`` header) maps to this. The connector drives
      lib.api for all HTTP concerns (basic auth, retry/backoff, rate-limit, timeouts) and handles
      paging itself via the ``next`` path.

Auth: HTTP Basic with the private API key as the username and an empty password. lib.api's
``basic`` strategy handles the base64 encoding from the ``RC_CONN_RECURLY`` env var.

Support use-case: joined account view — account → active subscriptions → latest invoice →
recent transactions — so the agent gets one concise markdown block instead of four JSON dumps.

CLI:
    python -m lib.connectors.recurly account <account-code-or-id>
    python -m lib.connectors.recurly subscriptions <account-code-or-id>
    python -m lib.connectors.recurly invoices <account-code-or-id>
    python -m lib.connectors.recurly transactions <account-code-or-id>
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from lib import api

# ---------------------------------------------------------------------------
# Manifest — loaded from manifest.yaml so the catalog row is the single source
# of truth; registered so `python -m lib.api get recurly …` also works.
# ---------------------------------------------------------------------------

_MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")


def _load_manifest() -> api.Manifest:
    raw = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return api._manifest_from_dict(raw)


MANIFEST = api.register(_load_manifest())


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="recurly")


# ---------------------------------------------------------------------------
# Pagination — body-embedded relative-path style
# ---------------------------------------------------------------------------

_DEFAULT_LIMIT = 200  # Recurly v3 max page size


def _paginate(path: str, query: dict | None = None, *, max_items: int = 400) -> list[dict]:
    """Collect items from a Recurly v3 list endpoint following body-embedded ``next`` paths.

    Drives the ``has_more`` + ``next`` pagination envelope that Recurly v3 returns in the
    response body. ``next`` is a relative path (``/accounts?cursor=…&limit=…``) which is passed
    directly to ``client.get`` so lib.api joins it to the base URL and applies auth+retry.
    Items live under ``data``.
    """
    c = _client()
    q = dict(query or {})
    q.setdefault("limit", _DEFAULT_LIMIT)
    out: list[dict] = []

    # First page via normal path so initial query params are applied.
    body = c.get(path, query=q)
    out.extend(body.get("data") or [])

    # Subsequent pages: follow body["next"] (relative path with cursor embedded).
    while len(out) < max_items and body.get("has_more"):
        next_path = body.get("next")
        if not next_path:
            break
        # next_path is a relative path like "/accounts?cursor=abc&limit=200"; lib.api's _join
        # handles stripping the leading "/" against base_url so auth and retry are preserved.
        body = c.get(next_path)
        out.extend(body.get("data") or [])

    return out[:max_items]


# ---------------------------------------------------------------------------
# Account lookup
# ---------------------------------------------------------------------------


def resolve_account(ref: str) -> dict | None:
    """Resolve an account by account code or Recurly UUID.

    ``ref`` is either an account code (any string your app assigned, e.g. ``"usr_42"``) or a
    Recurly-assigned UUID (``"aaaabbbb-0000-…"``). Both map to the same ``/accounts/{ref}``
    endpoint — Recurly accepts either form.
    """
    ref = (ref or "").strip()
    if not ref:
        raise RuntimeError("account reference (code or id) is required")
    c = _client()
    try:
        return c.get(f"/accounts/{ref}")
    except api.ApiError as e:
        if e.status == 404:
            return None
        raise


# ---------------------------------------------------------------------------
# Support joins
# ---------------------------------------------------------------------------


def support_summary(ref: str) -> dict:
    """Join support-relevant billing facts for an account into a compact dict.

    Multi-call: account → active subscriptions → latest invoice → recent transactions.
    Each call pre-selects the fields support needs, keeping the result small and stable.
    """
    account = resolve_account(ref)
    if account is None:
        return {"found": False, "ref": ref}

    code = account.get("code") or account.get("id") or ref

    subs = _paginate(f"/accounts/{code}/subscriptions", {"state": "active"}, max_items=10)
    if not subs:
        # Fall back to all states if no active subs — customer may be on a past-due or expired plan.
        subs = _paginate(f"/accounts/{code}/subscriptions", max_items=10)

    invoices = _paginate(f"/accounts/{code}/invoices", max_items=5)
    transactions = _paginate(f"/accounts/{code}/transactions", max_items=10)

    return {
        "found": True,
        "account": _pick_account(account),
        "subscriptions": [_pick_subscription(s) for s in subs],
        "latest_invoice": _pick_invoice(invoices[0]) if invoices else None,
        "recent_transactions": [_pick_transaction(t) for t in transactions],
    }


# ---------------------------------------------------------------------------
# Field pre-selection
# ---------------------------------------------------------------------------


def _pick_account(obj: dict) -> dict:
    """Account: id, code, email, state, balance, company."""
    return {
        "id": obj.get("id"),
        "code": obj.get("code"),
        "email": obj.get("email"),
        "state": obj.get("state"),
        "company": obj.get("company"),
        "balance": _pick_balance(obj.get("balance")),
        "created_at": obj.get("created_at"),
    }


def _pick_balance(balance: Any) -> Any:
    """Balance is a sub-object with amount + currency; flatten to a readable pair."""
    if not isinstance(balance, dict):
        return balance
    return {"amount": balance.get("amount"), "currency": balance.get("currency")}


def _pick_subscription(obj: dict) -> dict:
    """Subscription: id, state, plan, quantity, unit_amount, current period, trial, cancel."""
    plan = obj.get("plan") or {}
    return {
        "id": obj.get("id"),
        "state": obj.get("state"),
        "plan_code": plan.get("code"),
        "plan_name": plan.get("name"),
        "quantity": obj.get("quantity"),
        "unit_amount": obj.get("unit_amount"),
        "currency": obj.get("currency"),
        "current_period_started_at": obj.get("current_period_started_at"),
        "current_period_ends_at": obj.get("current_period_ends_at"),
        "trial_ends_at": obj.get("trial_ends_at"),
        "cancel_at_period_end": obj.get("cancel_at_period_end"),
        "canceled_at": obj.get("canceled_at"),
        "expires_at": obj.get("expires_at"),
    }


def _pick_invoice(obj: dict) -> dict:
    """Invoice: id, number, state, type, subtotal, tax, total, currency, due_at, closed_at."""
    return {
        "id": obj.get("id"),
        "number": obj.get("number"),
        "state": obj.get("state"),
        "type": obj.get("type"),
        "subtotal": obj.get("subtotal"),
        "tax": obj.get("tax"),
        "total": obj.get("total"),
        "currency": obj.get("currency"),
        "net_terms": obj.get("net_terms"),
        "due_at": obj.get("due_at"),
        "closed_at": obj.get("closed_at"),
        "created_at": obj.get("created_at"),
    }


def _pick_transaction(obj: dict) -> dict:
    """Transaction: id, type, status, amount, currency, gateway, payment method, created_at."""
    pm = obj.get("payment_method") or {}
    return {
        "id": obj.get("id"),
        "type": obj.get("type"),
        "status": obj.get("status"),
        "amount": obj.get("amount"),
        "currency": obj.get("currency"),
        "success": obj.get("success"),
        "payment_method_type": pm.get("object"),
        "card_type": pm.get("card_type"),
        "last_four": pm.get("last_four"),
        "gateway_message": obj.get("gateway_message"),
        "status_code": obj.get("status_code"),
        "created_at": obj.get("created_at"),
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _money(amount: Any, currency: Any = None) -> str:
    """Render a dollar amount as "12.50 USD". Recurly amounts are floats in major units."""
    try:
        val = f"{float(amount):.2f}"
    except (TypeError, ValueError):
        val = str(amount or "—")
    cur = (currency or "").upper()
    return f"{val} {cur}".strip() if cur else val


def summary_to_markdown(s: dict) -> str:
    """Render the support summary as concise grounding markdown."""
    if not s.get("found"):
        return f"# Recurly account not found\nNo account matched `{s.get('ref', '')}`."

    acct = s["account"]
    email = acct.get("email") or acct.get("code") or acct.get("id")
    lines = [f"# Recurly: {email}"]
    lines.append(f"- Account code: `{acct.get('code')}`  state: **{acct.get('state', 'unknown')}**")
    if acct.get("company"):
        lines.append(f"- Company: {acct['company']}")
    bal = acct.get("balance")
    if isinstance(bal, dict) and bal.get("amount") is not None:
        lines.append(f"- Balance: {_money(bal.get('amount'), bal.get('currency'))}")

    lines.append("\n## Subscriptions")
    subs = s.get("subscriptions") or []
    if subs:
        for sub in subs:
            plan_label = sub.get("plan_name") or sub.get("plan_code") or "—"
            price = _money(sub.get("unit_amount"), sub.get("currency"))
            cancel_note = " (cancels at period end)" if sub.get("cancel_at_period_end") else ""
            trial_note = f" (trial ends {sub['trial_ends_at'][:10]})" if sub.get("trial_ends_at") else ""
            ends = sub.get("current_period_ends_at", "")
            ends_note = f" — renews {ends[:10]}" if ends else ""
            lines.append(
                f"- `{sub.get('id')}` **{sub.get('state', '?')}** — {plan_label}"
                f" {price}{ends_note}{cancel_note}{trial_note}"
            )
    else:
        lines.append("- (no subscriptions)")

    lines.append("\n## Latest invoice")
    inv = s.get("latest_invoice")
    if inv:
        lines.append(
            f"- #{inv.get('number') or inv.get('id')} — **{inv.get('state', '?')}**"
            f" — {_money(inv.get('total'), inv.get('currency'))}"
            + (f" (due {inv['due_at'][:10]})" if inv.get("due_at") else "")
            + (f" (closed {inv['closed_at'][:10]})" if inv.get("closed_at") else "")
        )
    else:
        lines.append("- (no invoices)")

    lines.append("\n## Recent transactions")
    txns = s.get("recent_transactions") or []
    if txns:
        for t in txns:
            pm_parts = [p for p in [t.get("card_type"), t.get("last_four")] if p]
            pm_note = f" ({', '.join(pm_parts)})" if pm_parts else ""
            success = t.get("success")
            status_note = " ✓" if success is True else (" ✗" if success is False else "")
            gw = t.get("gateway_message")
            gw_note = f" — {gw}" if gw else ""
            lines.append(
                f"- `{t.get('id')}` {t.get('type', '?')}/{t.get('status', '?')}"
                f" {_money(t.get('amount'), t.get('currency'))}{pm_note}{status_note}{gw_note}"
                + (f" ({t['created_at'][:10]})" if t.get("created_at") else "")
            )
    else:
        lines.append("- (no transactions)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.recurly",
        description="Recurly support connector — concise grounding for support runs.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, help_text in [
        ("account", "render a joined support summary (subscriptions + invoice + transactions)"),
        ("subscriptions", "list subscriptions for an account (code or id)"),
        ("invoices", "list invoices for an account (code or id)"),
        ("transactions", "list transactions for an account (code or id)"),
    ]:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("ref", help="account code or Recurly UUID")

    args = parser.parse_args(argv)

    if args.cmd == "account":
        print(summary_to_markdown(support_summary(args.ref)))
        return 0

    account = resolve_account(args.ref)
    if account is None:
        print(f"No account found for: {args.ref}")
        return 1

    code = account.get("code") or account.get("id") or args.ref
    import json

    if args.cmd == "subscriptions":
        items = _paginate(f"/accounts/{code}/subscriptions", max_items=50)
        print(json.dumps([_pick_subscription(s) for s in items], indent=2, default=str))
    elif args.cmd == "invoices":
        items = _paginate(f"/accounts/{code}/invoices", max_items=50)
        print(json.dumps([_pick_invoice(i) for i in items], indent=2, default=str))
    elif args.cmd == "transactions":
        items = _paginate(f"/accounts/{code}/transactions", max_items=50)
        print(json.dumps([_pick_transaction(t) for t in items], indent=2, default=str))
    else:
        parser.error("unknown command")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
