"""Lemon Squeezy support connector — script connector over ``lib.api``.

Force-code trigger (d): Lemon Squeezy uses JSON:API page-number pagination — ``page[number]`` /
``page[size]`` — where the next page URL lives in ``links.next`` inside the response body (not in
an HTTP ``Link`` header). Neither lib.api's ``offset`` style (advances by item count) nor ``link``
style (reads the HTTP Link header) maps to this. The connector drives ``lib.api`` for all HTTP
concerns (bearer auth, retry/backoff, rate-limit, timeouts) and handles paging itself.

Support use-case: joined customer view — customer → their orders → active subscriptions → license
keys — so the agent gets one concise markdown block instead of four separate JSON dumps.

CLI:
    python -m lib.connectors.lemonsqueezy customer <customer-id-or-email>
    python -m lib.connectors.lemonsqueezy orders <customer-id-or-email>
    python -m lib.connectors.lemonsqueezy subscriptions <customer-id-or-email>
    python -m lib.connectors.lemonsqueezy licenses <customer-id-or-email>
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from lib import api

# ---------------------------------------------------------------------------
# Manifest — registered so `python -m lib.api get lemonsqueezy …` also works.
# Loaded from manifest.yaml rather than duplicated in code so the catalog row
# stays the single source of truth.
# ---------------------------------------------------------------------------

_MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")


def _load_manifest() -> api.Manifest:
    raw = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return api._manifest_from_dict(raw)


MANIFEST = api.register(_load_manifest())


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="lemonsqueezy")


# ---------------------------------------------------------------------------
# Pagination — JSON:API page-number style
# ---------------------------------------------------------------------------

_DEFAULT_PAGE_SIZE = 100


def _paginate(path: str, query: dict | None = None, *, max_items: int = 500) -> list[dict]:
    """Collect all items from a Lemon Squeezy JSON:API list endpoint.

    Drives page-number pagination via ``links.next`` in the response body (a full URL), calling
    lib.api's ``_send_url`` for authenticated link-following. Items live under ``data``.
    """
    c = _client()
    q = dict(query or {})
    q.setdefault("page[size]", _DEFAULT_PAGE_SIZE)
    out: list[dict] = []

    # First page via the normal path so the base query params are applied.
    page = c.fetch_page(path, query=q)
    out.extend(page.items)

    # Subsequent pages: follow links.next (absolute URL already includes all params).
    while len(out) < max_items:
        next_url = _links_next(page.body)
        if not next_url:
            break
        # Use the client's auth-bearing URL sender for authenticated link-following.
        resp = c._send_url("GET", next_url)
        body = api._parse_json(resp)
        items = body.get("data") or []
        out.extend(items)
        page = api.Page(body=body, items=items, next=_links_next(body))

    return out[:max_items]


def _links_next(body: Any) -> str | None:
    """Extract ``links.next`` from a JSON:API response body (body-embedded next-page URL)."""
    if not isinstance(body, dict):
        return None
    links = body.get("links")
    if not isinstance(links, dict):
        return None
    nxt = links.get("next")
    return str(nxt) if nxt else None


# ---------------------------------------------------------------------------
# Customer lookup
# ---------------------------------------------------------------------------


def resolve_customer(ref: str) -> dict | None:
    """Resolve a customer by Lemon Squeezy customer id or email.

    ``ref`` is either a numeric customer id (e.g. ``"12345"``) or an email address. The LS API
    supports ``filter[email]`` on ``/customers``; for a numeric id we fetch directly.
    """
    ref = (ref or "").strip()
    if not ref:
        raise RuntimeError("customer reference (id or email) is required")
    c = _client()
    if ref.isdigit():
        body = c.get(f"/customers/{ref}")
        return body.get("data")
    # Email lookup via filter.
    page = c.fetch_page("/customers", query={"filter[email]": ref, "page[size]": 1})
    return page.items[0] if page.items else None


# ---------------------------------------------------------------------------
# Support joins
# ---------------------------------------------------------------------------


def support_summary(ref: str) -> dict:
    """Join support-relevant billing facts for a customer into a compact dict.

    Multi-call: customer → orders → active subscriptions → license keys. Each call pre-selects
    the fields support needs, keeping the result small and stable.
    """
    customer = resolve_customer(ref)
    if customer is None:
        return {"found": False, "ref": ref}

    cid = _attr_id(customer)

    orders = _paginate("/orders", {"filter[user_id]": cid}, max_items=20)
    subs = _paginate("/subscriptions", {"filter[user_id]": cid}, max_items=20)
    licenses = _paginate("/license-keys", {"filter[user_id]": cid}, max_items=20)

    return {
        "found": True,
        "customer": _pick_customer(customer),
        "orders": [_pick_order(o) for o in orders],
        "subscriptions": [_pick_sub(s) for s in subs],
        "license_keys": [_pick_license(lk) for lk in licenses],
    }


# ---------------------------------------------------------------------------
# Field pre-selection (JSON:API attrs live under data.attributes)
# ---------------------------------------------------------------------------


def _attrs(obj: dict) -> dict:
    """JSON:API: data lives in ``attributes``; ``id`` and ``type`` are siblings."""
    return obj.get("attributes") or {}


def _attr_id(obj: dict) -> str:
    return str(obj.get("id") or "")


def _pick_customer(obj: dict) -> dict:
    a = _attrs(obj)
    return {
        "id": _attr_id(obj),
        "name": a.get("name"),
        "email": a.get("email"),
        "status": a.get("status"),
        "total_revenue_currency": a.get("total_revenue_currency"),
        "created_at": a.get("created_at"),
    }


def _pick_order(obj: dict) -> dict:
    a = _attrs(obj)
    return {
        "id": _attr_id(obj),
        "identifier": a.get("identifier"),
        "status": a.get("status"),
        "total": a.get("total"),
        "currency": a.get("currency"),
        "refunded": a.get("refunded"),
        "refunded_at": a.get("refunded_at"),
        "created_at": a.get("created_at"),
    }


def _pick_sub(obj: dict) -> dict:
    a = _attrs(obj)
    return {
        "id": _attr_id(obj),
        "status": a.get("status"),
        "product_name": a.get("product_name"),
        "variant_name": a.get("variant_name"),
        "billing_anchor": a.get("billing_anchor"),
        "renews_at": a.get("renews_at"),
        "ends_at": a.get("ends_at"),
        "cancelled": a.get("cancelled"),
        "pause": a.get("pause"),
        "created_at": a.get("created_at"),
    }


def _pick_license(obj: dict) -> dict:
    a = _attrs(obj)
    return {
        "id": _attr_id(obj),
        "key": a.get("key"),
        "status": a.get("status"),
        "activation_limit": a.get("activation_limit"),
        "activations_count": a.get("activations_count"),
        "expires_at": a.get("expires_at"),
        "created_at": a.get("created_at"),
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt_money(total: Any, currency: Any) -> str:
    """LS returns totals in minor units (cents). Format as readable "12.50 USD"."""
    if not isinstance(total, int):
        return str(total or "—")
    cur = (currency or "").upper()
    return f"{total / 100:.2f} {cur}".strip()


def summary_to_markdown(s: dict) -> str:
    """Render the support summary as concise grounding markdown."""
    if not s.get("found"):
        return f"# Lemon Squeezy customer not found\nNo customer matched `{s.get('ref', '')}`."

    cust = s["customer"]
    lines = [f"# Lemon Squeezy: {cust.get('email') or cust.get('id')}"]
    lines.append(f"- Customer ID: `{cust.get('id')}`" + (f" — {cust['name']}" if cust.get("name") else ""))
    lines.append(f"- Status: {cust.get('status', 'unknown')}")

    lines.append("\n## Orders")
    if s["orders"]:
        for o in s["orders"]:
            ref_flag = " (**REFUNDED**)" if o.get("refunded") else ""
            lines.append(
                f"- `{o.get('identifier') or o.get('id')}` — {o.get('status', '?')}"
                f" — {_fmt_money(o.get('total'), o.get('currency'))}{ref_flag}"
                f" ({o.get('created_at', '?')[:10]})"
            )
    else:
        lines.append("- (no orders)")

    lines.append("\n## Subscriptions")
    if s["subscriptions"]:
        for sub in s["subscriptions"]:
            cancel_note = " (cancels at period end)" if sub.get("cancelled") else ""
            pause_note = " (paused)" if sub.get("pause") else ""
            lines.append(
                f"- `{sub.get('id')}` — **{sub.get('status', '?')}**"
                f" — {sub.get('product_name') or '?'} / {sub.get('variant_name') or '?'}"
                f"{cancel_note}{pause_note}"
                f" (renews {sub.get('renews_at', '?')[:10] if sub.get('renews_at') else 'n/a'})"
            )
    else:
        lines.append("- (no subscriptions)")

    lines.append("\n## License Keys")
    if s["license_keys"]:
        for lk in s["license_keys"]:
            acts = f"{lk.get('activations_count', 0)}/{lk.get('activation_limit') or '∞'}"
            exp = lk.get("expires_at")
            exp_note = f" (expires {exp[:10]})" if exp else ""
            lines.append(
                f"- `{lk.get('key', lk.get('id'))}` — {lk.get('status', '?')} — {acts} activations{exp_note}"
            )
    else:
        lines.append("- (no license keys)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.lemonsqueezy",
        description="Lemon Squeezy support connector — concise grounding for support runs.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, help_text in [
        ("customer", "render a joined support summary (orders + subscriptions + licenses)"),
        ("orders", "list orders for a customer (id or email)"),
        ("subscriptions", "list subscriptions for a customer (id or email)"),
        ("licenses", "list license keys for a customer (id or email)"),
    ]:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("ref", help="customer id (numeric) or email")

    args = parser.parse_args(argv)

    if args.cmd == "customer":
        print(summary_to_markdown(support_summary(args.ref)))
        return 0

    customer = resolve_customer(args.ref)
    if customer is None:
        print(f"No customer found for: {args.ref}")
        return 1

    cid = _attr_id(customer)
    import json

    if args.cmd == "orders":
        items = _paginate("/orders", {"filter[user_id]": cid}, max_items=50)
        print(json.dumps([_pick_order(o) for o in items], indent=2, default=str))
    elif args.cmd == "subscriptions":
        items = _paginate("/subscriptions", {"filter[user_id]": cid}, max_items=50)
        print(json.dumps([_pick_sub(s) for s in items], indent=2, default=str))
    elif args.cmd == "licenses":
        items = _paginate("/license-keys", {"filter[user_id]": cid}, max_items=50)
        print(json.dumps([_pick_license(lk) for lk in items], indent=2, default=str))
    else:
        parser.error("unknown command")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
