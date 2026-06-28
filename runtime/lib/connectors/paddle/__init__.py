"""Paddle support connector — billing grounding over lib.api.

Force-code triggers:
  (b) multi-call join: customer → subscriptions → recent transactions for a concise support view.
  (d) non-standard pagination: Paddle embeds the next page URL in the JSON body
      (``meta.pagination.next``) rather than an HTTP Link header or a standalone cursor token,
      so the generic lib.api cursor/link styles can't express it. ``_paddle_pages`` drives the
      while-has-more loop manually by following ``meta.pagination.next`` as an absolute URL.

CLI:
    python -m lib.connectors.paddle customer ctm_01h8441jn5pcwrfhwh78jqt8hk
    python -m lib.connectors.paddle customer alice@example.com
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterator

import yaml

from lib import api

# --------------------------------------------------------------------------
# Manifest registration (also loaded by the YAML auto-loader; register() wins)
# --------------------------------------------------------------------------

_MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.yaml"


def _load_manifest() -> api.Manifest:
    raw = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return api._manifest_from_dict(raw)


MANIFEST = api.register(_load_manifest())

API_BASE = MANIFEST.base_url  # https://api.paddle.com


# --------------------------------------------------------------------------
# Core: Paddle-specific pagination (trigger d)
# --------------------------------------------------------------------------


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="paddle")


def _paddle_pages(path: str, query: dict | None = None) -> Iterator[list[dict]]:
    """Yield pages (lists of items) following Paddle's body-embedded ``meta.pagination.next`` URL.

    Paddle returns ``{"data": [...], "meta": {"pagination": {"has_more": bool, "next": "<url>"}}}``
    where ``next`` is the full absolute URL for the next page — not an HTTP Link header and not a
    standalone cursor token. The generic lib.api link/cursor styles can't follow a body-embedded
    full URL, so we drive the loop here using ``Client._send_url`` for continuation pages.
    """
    c = _client()
    # First page: use the normal fetch_page path (applies auth + base_url join).
    page = c.fetch_page(path, query=query or {})
    yield page.items

    # Continuation pages: follow ``meta.pagination.next`` verbatim.
    body = page.body
    while _truthy_has_more(body):
        next_url = _next_url(body)
        if not next_url:
            break
        # _send_url is the Client method for absolute-URL GETs (used by link-style pagination
        # internally) — it applies auth and respects retry/rate-limit logic.
        resp = c._send_url("GET", next_url)
        body = _parse_body(resp)
        items = body.get("data") or []
        yield items
        if not _truthy_has_more(body):
            break


def _truthy_has_more(body: dict) -> bool:
    try:
        return bool(body["meta"]["pagination"]["has_more"])
    except (KeyError, TypeError):
        return False


def _next_url(body: dict) -> str | None:
    try:
        url = body["meta"]["pagination"]["next"]
        return str(url) if url else None
    except (KeyError, TypeError):
        return None


def _parse_body(resp: Any) -> dict:
    """Parse JSON from a raw requests.Response (returned by Client._send_url)."""
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        raise api.ApiError(resp.status_code, "non-JSON response", url=getattr(resp, "url", ""))


def _collect(path: str, query: dict | None = None, *, limit: int = 200) -> list[dict]:
    """Collect up to ``limit`` items across pages from a Paddle list endpoint."""
    out: list[dict] = []
    for batch in _paddle_pages(path, query):
        out.extend(batch)
        if len(out) >= limit:
            break
    return out[:limit]


# --------------------------------------------------------------------------
# Domain reads (trigger b: multi-call join)
# --------------------------------------------------------------------------


def resolve_customer(ref: str) -> dict | None:
    """Resolve a Paddle customer by id (``ctm_…``) or email.

    Email lookup uses the ``/customers?search=`` query param (Paddle supports fuzzy search on
    email/name). Falls back to ``None`` if no match.
    """
    ref = (ref or "").strip()
    if not ref:
        raise RuntimeError("customer reference (id or email) is required")
    c = _client()
    if ref.startswith("ctm_"):
        return c.get(f"/customers/{ref}").get("data")
    # Email search: Paddle's /customers accepts a `search` param that matches on email.
    items = _collect("/customers", {"search": ref, "per_page": 10}, limit=10)
    # Exact email match first; fall back to first result.
    for it in items:
        if (it.get("email") or "").lower() == ref.lower():
            return it
    return items[0] if items else None


def support_summary(ref: str) -> dict:
    """Join the support-relevant billing facts for a Paddle customer into a compact dict.

    Multi-call: customer → subscriptions → recent transactions. Pre-selects only the fields
    support needs so the result is small and stable.
    """
    customer = resolve_customer(ref)
    if customer is None:
        return {"found": False, "ref": ref}

    cid = customer.get("id", "")

    subs = _collect("/subscriptions", {"customer_id": cid, "per_page": 50}, limit=10)

    txns = _collect("/transactions", {"customer_id": cid, "per_page": 30}, limit=10)

    return {
        "found": True,
        "customer": api.pick(customer, "id,email,name,status,created_at,locale,marketing_consent"),
        "subscriptions": [
            api.pick(s, "id,status,billing_cycle.frequency,billing_cycle.interval,"
                        "next_billed_at,paused_at,canceled_at,"
                        "items.*.price.unit_price.amount,items.*.price.unit_price.currency_code,"
                        "items.*.price.description,management_urls.cancel,management_urls.update_payment_method")
            for s in subs
        ],
        "recent_transactions": [
            api.pick(t, "id,status,billed_at,created_at,"
                        "details.totals.total,details.totals.currency_code,"
                        "details.totals.tax,billing_period.starts_at,billing_period.ends_at")
            for t in txns
        ],
    }


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------


def summary_to_markdown(s: dict) -> str:
    """Render the support summary as concise grounding markdown."""
    if not s.get("found"):
        return f"# Paddle customer not found\nNo customer matched `{s.get('ref', '')}`."

    cust = s["customer"]
    email = cust.get("email") or cust.get("id")
    lines = [f"# Paddle: {email}"]
    lines.append(f"- Customer ID: `{cust.get('id')}`")
    if cust.get("name"):
        lines.append(f"- Name: {cust['name']}")
    lines.append(f"- Status: {cust.get('status', 'unknown')}")
    if cust.get("created_at"):
        lines.append(f"- Created: {cust['created_at']}")

    subs = s.get("subscriptions") or []
    lines.append("\n## Subscriptions")
    if subs:
        for sub in subs:
            lines.append(f"- `{sub.get('id')}` status=**{sub.get('status', '?')}**"
                         + (f" next_billed={sub['next_billed_at']}" if sub.get("next_billed_at") else "")
                         + (f" paused={sub['paused_at']}" if sub.get("paused_at") else "")
                         + (f" canceled={sub['canceled_at']}" if sub.get("canceled_at") else ""))
    else:
        lines.append("- (no subscriptions)")

    txns = s.get("recent_transactions") or []
    lines.append("\n## Recent transactions")
    if txns:
        for t in txns:
            total = t.get("details.totals.total", "?")
            currency = t.get("details.totals.currency_code", "")
            lines.append(f"- `{t.get('id')}` {t.get('status', '?')} {total} {currency}"
                         + (f" billed={t['billed_at']}" if t.get("billed_at") else ""))
    else:
        lines.append("- (no transactions)")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.paddle")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cust = sub.add_parser("customer", help="render a customer's billing support summary")
    cust.add_argument("ref", help="customer id (ctm_…) or email")
    args = parser.parse_args(argv)

    if args.cmd == "customer":
        print(summary_to_markdown(support_summary(args.ref)))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
