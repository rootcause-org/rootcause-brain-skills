"""Shopify Admin GraphQL connector — force-code: GraphQL transport (POST) + field pre-selection.

lib.api is a GET/REST client; Shopify's Admin API is GraphQL-over-POST with per-shop base URLs.
This connector wraps GraphQL POSTs, handles cursor pagination (pageInfo.endCursor / hasNextPage
embedded in query variables, not in HTTP headers/params), and pre-selects the support-relevant
fields from the heavily nested edges/nodes response shape before returning them to the agent.

Three entry points answer real support questions:
  orders   — recent orders: status, financial state, totals, customer email, fulfillment
  customer — lookup by email or GID: name, email, orders count, phone, tags
  product  — lookup by GID: title, handle, status, variants (price, inventory), tags

Auth: X-Shopify-Access-Token header (raw token, no "Bearer" prefix) injected as RC_CONN_SHOPIFY.
Retry/backoff: lib.api's Client._send_url is not reusable here (GraphQL POST); we keep the retry
layer minimal (one 429-aware retry) rather than re-implementing the full backoff — Shopify's cost
throttle is response-body-based, not header-based.

CLI:
    python -m lib.connectors.shopify orders   --shop SLUG [--limit N] [--query FILTER]
    python -m lib.connectors.shopify customer --shop SLUG --ref EMAIL_OR_GID
    python -m lib.connectors.shopify product  --shop SLUG --id PRODUCT_GID
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

import requests

from lib import oauth

# Shopify stable Admin GraphQL version — update annually when Shopify releases a new stable version.
_API_VERSION = "2025-01"
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Low-level GraphQL caller
# ---------------------------------------------------------------------------


def _graphql_url(shop: str) -> str:
    slug = shop.rstrip("/").removesuffix(".myshopify.com")
    return f"https://{slug}.myshopify.com/admin/api/{_API_VERSION}/graphql.json"


def _token() -> str:
    return oauth.token("shopify")


def _post(shop: str, query: str, variables: dict | None = None) -> dict:
    """POST a GraphQL query; handles 429 Retry-After with one retry. Raises on error."""
    url = _graphql_url(shop)
    headers = {
        "X-Shopify-Access-Token": _token(),
        "Content-Type": "application/json",
    }
    payload = {"query": query, "variables": variables or {}}

    for attempt in range(2):
        resp = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            wait = float(ra) if ra else 2.0
            time.sleep(min(wait, 60.0))
            if attempt == 0:
                continue
        if not (200 <= resp.status_code < 300):
            try:
                body = resp.text[:800]
            except Exception:  # noqa: BLE001
                body = ""
            raise RuntimeError(f"Shopify GraphQL HTTP {resp.status_code}: {body}")
        data = resp.json()
        if "errors" in data:
            errs = json.dumps(data["errors"])[:800]
            raise RuntimeError(f"Shopify GraphQL errors: {errs}")
        return data

    raise RuntimeError("Shopify GraphQL: exceeded retry limit")


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

_ORDERS_QUERY = """
query Orders($first: Int!, $after: String, $query: String) {
  orders(first: $first, after: $after, query: $query, sortKey: CREATED_AT, reverse: true) {
    edges {
      cursor
      node {
        id
        name
        createdAt
        updatedAt
        displayFinancialStatus
        displayFulfillmentStatus
        totalPriceSet { shopMoney { amount currencyCode } }
        customer { email displayName }
        shippingAddress { city countryCode }
        lineItems(first: 5) {
          edges {
            node { title quantity }
          }
        }
        tags
        cancelledAt
        cancelReason
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


def _order_node(node: dict) -> dict:
    """Pre-select support-relevant fields from a raw order node."""
    total = ""
    try:
        m = node["totalPriceSet"]["shopMoney"]
        total = f"{m['amount']} {m['currencyCode']}"
    except (KeyError, TypeError):
        pass

    customer_email = ""
    customer_name = ""
    try:
        c = node["customer"]
        if c:
            customer_email = c.get("email") or ""
            customer_name = c.get("displayName") or ""
    except (KeyError, TypeError):
        pass

    line_items = []
    try:
        for e in node.get("lineItems", {}).get("edges", []):
            n = e.get("node", {})
            line_items.append({"title": n.get("title"), "quantity": n.get("quantity")})
    except (KeyError, TypeError):
        pass

    ship = node.get("shippingAddress") or {}

    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "created_at": node.get("createdAt"),
        "financial_status": node.get("displayFinancialStatus"),
        "fulfillment_status": node.get("displayFulfillmentStatus"),
        "total": total,
        "customer_email": customer_email,
        "customer_name": customer_name,
        "shipping_city": ship.get("city"),
        "shipping_country": ship.get("countryCode"),
        "line_items": line_items,
        "tags": node.get("tags") or [],
        "cancelled_at": node.get("cancelledAt"),
        "cancel_reason": node.get("cancelReason"),
    }


def fetch_orders(shop: str, *, limit: int = 50, query_filter: str = "") -> list[dict]:
    """Fetch up to ``limit`` recent orders, auto-paging with GraphQL cursor pagination."""
    PAGE_SIZE = min(limit, 50)
    orders: list[dict] = []
    after: str | None = None

    while len(orders) < limit:
        remaining = limit - len(orders)
        variables: dict[str, Any] = {"first": min(PAGE_SIZE, remaining)}
        if after:
            variables["after"] = after
        if query_filter:
            variables["query"] = query_filter

        data = _post(shop, _ORDERS_QUERY, variables)
        conn = data.get("data", {}).get("orders", {})
        edges = conn.get("edges") or []
        for edge in edges:
            node = edge.get("node") or {}
            orders.append(_order_node(node))
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage") or not edges:
            break
        after = page_info.get("endCursor")

    return orders[:limit]


# ---------------------------------------------------------------------------
# Customer lookup
# ---------------------------------------------------------------------------

_CUSTOMER_BY_EMAIL_QUERY = """
query CustomerByEmail($query: String!) {
  customers(first: 1, query: $query) {
    edges {
      node {
        id
        displayName
        email
        phone
        numberOfOrders
        totalSpentV2 { amount currencyCode }
        tags
        createdAt
        state
      }
    }
  }
}
"""

_CUSTOMER_BY_GID_QUERY = """
query CustomerById($id: ID!) {
  customer(id: $id) {
    id
    displayName
    email
    phone
    numberOfOrders
    totalSpentV2 { amount currencyCode }
    tags
    createdAt
    state
  }
}
"""


def _customer_node(node: dict) -> dict:
    total = ""
    try:
        t = node.get("totalSpentV2") or {}
        total = f"{t['amount']} {t['currencyCode']}"
    except (KeyError, TypeError):
        pass
    return {
        "id": node.get("id"),
        "name": node.get("displayName"),
        "email": node.get("email"),
        "phone": node.get("phone"),
        "orders_count": node.get("numberOfOrders"),
        "total_spent": total,
        "tags": node.get("tags") or [],
        "created_at": node.get("createdAt"),
        "state": node.get("state"),
    }


def fetch_customer(shop: str, ref: str) -> dict | None:
    """Look up a customer by email or GID (gid://shopify/Customer/…). Returns None if not found."""
    ref = (ref or "").strip()
    if not ref:
        raise RuntimeError("customer ref (email or GID) is required")

    if ref.startswith("gid://shopify/Customer/"):
        data = _post(shop, _CUSTOMER_BY_GID_QUERY, {"id": ref})
        node = (data.get("data") or {}).get("customer")
        return _customer_node(node) if node else None

    # Email lookup via query= filter
    data = _post(shop, _CUSTOMER_BY_EMAIL_QUERY, {"query": f"email:{ref}"})
    edges = (data.get("data") or {}).get("customers", {}).get("edges") or []
    if not edges:
        return None
    return _customer_node(edges[0]["node"])


# ---------------------------------------------------------------------------
# Product lookup
# ---------------------------------------------------------------------------

_PRODUCT_QUERY = """
query ProductById($id: ID!) {
  product(id: $id) {
    id
    title
    handle
    status
    tags
    createdAt
    updatedAt
    variants(first: 10) {
      edges {
        node {
          id
          title
          price
          sku
          inventoryQuantity
          availableForSale
        }
      }
    }
  }
}
"""


def _product_node(node: dict) -> dict:
    variants = []
    try:
        for e in node.get("variants", {}).get("edges", []):
            v = e.get("node", {})
            variants.append({
                "id": v.get("id"),
                "title": v.get("title"),
                "price": v.get("price"),
                "sku": v.get("sku"),
                "inventory": v.get("inventoryQuantity"),
                "available": v.get("availableForSale"),
            })
    except (KeyError, TypeError):
        pass
    return {
        "id": node.get("id"),
        "title": node.get("title"),
        "handle": node.get("handle"),
        "status": node.get("status"),
        "tags": node.get("tags") or [],
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "variants": variants,
    }


def fetch_product(shop: str, product_id: str) -> dict | None:
    """Fetch a product by GID. Returns None if not found."""
    if not product_id:
        raise RuntimeError("product GID is required")
    data = _post(shop, _PRODUCT_QUERY, {"id": product_id})
    node = (data.get("data") or {}).get("product")
    return _product_node(node) if node else None


# ---------------------------------------------------------------------------
# Markdown renderers
# ---------------------------------------------------------------------------


def orders_to_markdown(orders: list[dict], shop: str) -> str:
    if not orders:
        return f"# Shopify orders: {shop}\nNo orders found."
    lines = [f"# Shopify orders: {shop} ({len(orders)} shown)"]
    for o in orders:
        status = f"{o.get('financial_status') or '?'} / {o.get('fulfillment_status') or '?'}"
        lines.append(f"\n## {o.get('name') or o.get('id')}")
        lines.append(f"- Status: {status}")
        lines.append(f"- Total: {o.get('total') or '—'}")
        lines.append(f"- Customer: {o.get('customer_email') or '—'} {o.get('customer_name') or ''}".rstrip())
        lines.append(f"- Created: {o.get('created_at') or '—'}")
        if o.get("cancelled_at"):
            lines.append(f"- **Cancelled**: {o['cancel_reason'] or ''} at {o['cancelled_at']}")
        items = o.get("line_items") or []
        if items:
            item_str = ", ".join(f"{it['title']} ×{it['quantity']}" for it in items)
            lines.append(f"- Items: {item_str}")
        if o.get("tags"):
            lines.append(f"- Tags: {', '.join(o['tags'])}")
    return "\n".join(lines)


def customer_to_markdown(customer: dict | None, ref: str, shop: str) -> str:
    if not customer:
        return f"# Shopify customer not found\nNo customer matched `{ref}` on shop `{shop}`."
    lines = [f"# Shopify customer: {customer.get('email') or customer.get('id')}"]
    lines.append(f"- Name: {customer.get('name') or '—'}")
    if customer.get("phone"):
        lines.append(f"- Phone: {customer['phone']}")
    lines.append(f"- Orders: {customer.get('orders_count') or 0}, Total spent: {customer.get('total_spent') or '—'}")
    lines.append(f"- State: {customer.get('state') or '—'}")
    lines.append(f"- Customer since: {customer.get('created_at') or '—'}")
    if customer.get("tags"):
        lines.append(f"- Tags: {', '.join(customer['tags'])}")
    return "\n".join(lines)


def product_to_markdown(product: dict | None, product_id: str, shop: str) -> str:
    if not product:
        return f"# Shopify product not found\nNo product matched `{product_id}` on shop `{shop}`."
    lines = [f"# Shopify product: {product.get('title') or product.get('id')}"]
    lines.append(f"- Handle: {product.get('handle') or '—'}")
    lines.append(f"- Status: {product.get('status') or '—'}")
    lines.append(f"- Created: {product.get('created_at') or '—'}")
    variants = product.get("variants") or []
    if variants:
        lines.append(f"- Variants ({len(variants)}):")
        for v in variants:
            inv = v.get("inventory")
            avail = "in stock" if v.get("available") else "out of stock"
            sku = f" SKU:{v['sku']}" if v.get("sku") else ""
            lines.append(f"  - {v.get('title') or '?'}: {v.get('price') or '?'}{sku}, {inv if inv is not None else '?'} units ({avail})")
    if product.get("tags"):
        lines.append(f"- Tags: {', '.join(product['tags'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.shopify",
        description="Shopify Admin GraphQL connector — read-only grounding for support runs.",
    )
    parser.add_argument("--shop", required=True, help="Shopify subdomain slug (e.g. mystore)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ord_p = sub.add_parser("orders", help="list recent orders")
    ord_p.add_argument("--limit", type=int, default=50, help="max orders to fetch (default 50)")
    ord_p.add_argument("--query", default="", dest="query_filter", help="Shopify order query filter (e.g. 'financial_status:unpaid')")

    cust_p = sub.add_parser("customer", help="look up a customer by email or GID")
    cust_p.add_argument("--ref", required=True, help="customer email or GID (gid://shopify/Customer/…)")

    prod_p = sub.add_parser("product", help="fetch a product by GID")
    prod_p.add_argument("--id", dest="product_id", required=True, help="product GID (gid://shopify/Product/…)")

    args = parser.parse_args(argv)

    if args.cmd == "orders":
        orders = fetch_orders(args.shop, limit=args.limit, query_filter=args.query_filter)
        print(orders_to_markdown(orders, args.shop))
        return 0
    if args.cmd == "customer":
        customer = fetch_customer(args.shop, args.ref)
        print(customer_to_markdown(customer, args.ref, args.shop))
        return 0
    if args.cmd == "product":
        product = fetch_product(args.shop, args.product_id)
        print(product_to_markdown(product, args.product_id, args.shop))
        return 0

    parser.error("unknown command")
    return 2
