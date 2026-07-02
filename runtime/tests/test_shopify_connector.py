"""Fixture tests for the Shopify Admin GraphQL connector.

Shopify uses GraphQL (POST), not REST. The connector drives POSTs through lib.api so env-token and
brokered modes share method policy/retry behavior. The fixture bodies mirror Shopify's documented
example payloads, trimmed to support-relevant fields.

Exercises:
- Manifest YAML loads via lib.api's YAML loader and maps every field correctly.
- `fetch_orders` auto-pages across two GraphQL cursor pages (pageInfo.hasNextPage / endCursor).
- `fetch_customer` looks up by email and by GID.
- `fetch_product` fetches a product by GID.
- X-Shopify-Access-Token credential rides EVERY request (not just page 1).
- Markdown renderers produce the expected headings / key lines.
- CLI drives orders/customer/product sub-commands end-to-end.
- Token-prefix hygiene guard: no real Shopify token prefix in the connector directory.

No live creds, no network. All HTTP is mocked.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_shopify_connector.py -q
"""

import json
import os
import sys
import unittest
from pathlib import Path
import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import shopify as shopify_conn  # noqa: E402

SHOP = "teststore"
GRAPHQL_URL = f"https://{SHOP}.myshopify.com/admin/api/2025-01/graphql.json"

# ---------------------------------------------------------------------------
# Fixture payloads (Shopify documented example shapes, trimmed to support fields)
# ---------------------------------------------------------------------------

_ORDER_1 = {
    "id": "gid://shopify/Order/1",
    "name": "#1001",
    "createdAt": "2025-01-10T10:00:00Z",
    "updatedAt": "2025-01-10T11:00:00Z",
    "displayFinancialStatus": "PAID",
    "displayFulfillmentStatus": "FULFILLED",
    "totalPriceSet": {"shopMoney": {"amount": "99.00", "currencyCode": "USD"}},
    "customer": {"email": "alice@example.com", "displayName": "Alice Smith"},
    "shippingAddress": {"city": "New York", "countryCode": "US"},
    "lineItems": {"edges": [{"node": {"title": "Widget Pro", "quantity": 2}}]},
    "tags": ["vip"],
    "cancelledAt": None,
    "cancelReason": None,
}

_ORDER_2 = {
    "id": "gid://shopify/Order/2",
    "name": "#1002",
    "createdAt": "2025-01-09T08:00:00Z",
    "updatedAt": "2025-01-09T09:00:00Z",
    "displayFinancialStatus": "UNPAID",
    "displayFulfillmentStatus": "UNFULFILLED",
    "totalPriceSet": {"shopMoney": {"amount": "24.50", "currencyCode": "USD"}},
    "customer": {"email": "bob@example.com", "displayName": "Bob Jones"},
    "shippingAddress": {"city": "Austin", "countryCode": "US"},
    "lineItems": {"edges": [{"node": {"title": "Gadget", "quantity": 1}}]},
    "tags": [],
    "cancelledAt": None,
    "cancelReason": None,
}

_PAGE_1_BODY = {
    "data": {
        "orders": {
            "edges": [{"cursor": "cursor1", "node": _ORDER_1}],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor1"},
        }
    }
}

_PAGE_2_BODY = {
    "data": {
        "orders": {
            "edges": [{"cursor": "cursor2", "node": _ORDER_2}],
            "pageInfo": {"hasNextPage": False, "endCursor": "cursor2"},
        }
    }
}

_CUSTOMER_EMAIL_BODY = {
    "data": {
        "customers": {
            "edges": [
                {
                    "node": {
                        "id": "gid://shopify/Customer/42",
                        "displayName": "Alice Smith",
                        "email": "alice@example.com",
                        "phone": "+15555551234",
                        "numberOfOrders": 3,
                        "totalSpentV2": {"amount": "245.00", "currencyCode": "USD"},
                        "tags": ["loyal"],
                        "createdAt": "2024-06-01T00:00:00Z",
                        "state": "ENABLED",
                    }
                }
            ]
        }
    }
}

_CUSTOMER_GID_BODY = {
    "data": {
        "customer": {
            "id": "gid://shopify/Customer/42",
            "displayName": "Alice Smith",
            "email": "alice@example.com",
            "phone": "+15555551234",
            "numberOfOrders": 3,
            "totalSpentV2": {"amount": "245.00", "currencyCode": "USD"},
            "tags": ["loyal"],
            "createdAt": "2024-06-01T00:00:00Z",
            "state": "ENABLED",
        }
    }
}

_PRODUCT_BODY = {
    "data": {
        "product": {
            "id": "gid://shopify/Product/7",
            "title": "Widget Pro",
            "handle": "widget-pro",
            "status": "ACTIVE",
            "tags": ["bestseller"],
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2025-01-01T00:00:00Z",
            "variants": {
                "edges": [
                    {
                        "node": {
                            "id": "gid://shopify/ProductVariant/71",
                            "title": "Default Title",
                            "price": "49.99",
                            "sku": "WP-001",
                            "inventoryQuantity": 120,
                            "availableForSale": True,
                        }
                    }
                ]
            },
        }
    }
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_post(body: dict, status: int = 200):
    """Register a `responses` mock for a POST to the Shopify GraphQL endpoint."""
    responses_lib.add(
        responses_lib.POST,
        GRAPHQL_URL,
        json=body,
        status=status,
    )


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------


class ShopifyManifestLoad(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("shopify", m)
        s = m["shopify"]
        self.assertEqual(s.key, "shopify")
        self.assertIn("myshopify.com", s.base_url)
        self.assertEqual(s.auth.strategy, "api_key_header")
        self.assertEqual(s.auth.name, "X-Shopify-Access-Token")
        # Pagination is `none` — GraphQL cursor is query-embedded, not HTTP-header driven.
        self.assertEqual(s.pagination.style, "none")
        # No remaining-count header (Shopify uses query-cost throttle in response body).
        self.assertEqual(s.rate_limit_remaining_header, "")

    def test_manifest_page_size_field(self):
        m = api.load_manifests()
        self.assertEqual(m["shopify"].pagination.page_size, 50)


# ---------------------------------------------------------------------------
# Orders tests
# ---------------------------------------------------------------------------


class ShopifyOrders(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_SHOPIFY")
        os.environ["RC_CONN_SHOPIFY"] = "shpat_test_dummy"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_SHOPIFY", None)
        else:
            os.environ["RC_CONN_SHOPIFY"] = self._saved

    @responses_lib.activate
    def test_orders_auto_pages_two_pages(self):
        _mock_post(_PAGE_1_BODY)
        _mock_post(_PAGE_2_BODY)

        orders = shopify_conn.fetch_orders(SHOP, limit=10)

        self.assertEqual(len(orders), 2)
        # Order from page 1
        self.assertEqual(orders[0]["name"], "#1001")
        self.assertEqual(orders[0]["financial_status"], "PAID")
        self.assertEqual(orders[0]["total"], "99.00 USD")
        self.assertEqual(orders[0]["customer_email"], "alice@example.com")
        self.assertEqual(orders[0]["line_items"][0]["title"], "Widget Pro")
        self.assertEqual(orders[0]["tags"], ["vip"])
        # Order from page 2
        self.assertEqual(orders[1]["name"], "#1002")
        self.assertEqual(orders[1]["financial_status"], "UNPAID")
        # Two POST requests were made
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_credential_rides_every_request(self):
        _mock_post(_PAGE_1_BODY)
        _mock_post(_PAGE_2_BODY)

        shopify_conn.fetch_orders(SHOP, limit=10)

        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["X-Shopify-Access-Token"], "shpat_test_dummy")

    @responses_lib.activate
    def test_brokered_mode_posts_without_client_side_token(self):
        os.environ.pop("RC_CONN_SHOPIFY", None)
        old_brokered = os.environ.get("RC_API_BROKERED_KEYS")
        os.environ["RC_API_BROKERED_KEYS"] = "shopify"
        try:
            responses_lib.add(
                responses_lib.POST,
                "http://rc-broker.internal/shopify/__url/"
                "https%3A%2F%2Fteststore.myshopify.com%2Fadmin%2Fapi%2F2025-01%2Fgraphql.json",
                json=_PAGE_2_BODY,
                status=200,
            )
            orders = shopify_conn.fetch_orders(SHOP, limit=1)
            self.assertEqual(len(orders), 1)
            self.assertNotIn("X-Shopify-Access-Token", responses_lib.calls[0].request.headers)
            self.assertNotIn("Authorization", responses_lib.calls[0].request.headers)
        finally:
            if old_brokered is None:
                os.environ.pop("RC_API_BROKERED_KEYS", None)
            else:
                os.environ["RC_API_BROKERED_KEYS"] = old_brokered

    @responses_lib.activate
    def test_second_page_uses_after_cursor(self):
        _mock_post(_PAGE_1_BODY)
        _mock_post(_PAGE_2_BODY)

        shopify_conn.fetch_orders(SHOP, limit=10)

        # The second request should include the cursor from page 1.
        body2 = json.loads(responses_lib.calls[1].request.body)
        self.assertEqual(body2["variables"]["after"], "cursor1")

    @responses_lib.activate
    def test_limit_respected(self):
        _mock_post(_PAGE_1_BODY)
        # Only 1 requested — should stop after page 1 even though hasNextPage=True.
        orders = shopify_conn.fetch_orders(SHOP, limit=1)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["name"], "#1001")

    @responses_lib.activate
    def test_orders_to_markdown(self):
        _mock_post(_PAGE_1_BODY)
        _mock_post(_PAGE_2_BODY)

        orders = shopify_conn.fetch_orders(SHOP, limit=10)
        md = shopify_conn.orders_to_markdown(orders, SHOP)

        self.assertIn("# Shopify orders:", md)
        self.assertIn("#1001", md)
        self.assertIn("PAID", md)
        self.assertIn("99.00 USD", md)
        self.assertIn("alice@example.com", md)
        self.assertIn("#1002", md)
        self.assertIn("UNPAID", md)

    def test_orders_to_markdown_empty(self):
        md = shopify_conn.orders_to_markdown([], SHOP)
        self.assertIn("No orders found", md)


# ---------------------------------------------------------------------------
# Customer tests
# ---------------------------------------------------------------------------


class ShopifyCustomer(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_SHOPIFY")
        os.environ["RC_CONN_SHOPIFY"] = "shpat_test_dummy"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_SHOPIFY", None)
        else:
            os.environ["RC_CONN_SHOPIFY"] = self._saved

    @responses_lib.activate
    def test_fetch_customer_by_email(self):
        _mock_post(_CUSTOMER_EMAIL_BODY)

        c = shopify_conn.fetch_customer(SHOP, "alice@example.com")

        self.assertIsNotNone(c)
        self.assertEqual(c["email"], "alice@example.com")
        self.assertEqual(c["name"], "Alice Smith")
        self.assertEqual(c["orders_count"], 3)
        self.assertEqual(c["total_spent"], "245.00 USD")
        self.assertEqual(c["state"], "ENABLED")
        self.assertIn("loyal", c["tags"])
        # Credential was in the request
        self.assertEqual(
            responses_lib.calls[0].request.headers["X-Shopify-Access-Token"],
            "shpat_test_dummy",
        )

    @responses_lib.activate
    def test_fetch_customer_by_gid(self):
        _mock_post(_CUSTOMER_GID_BODY)

        c = shopify_conn.fetch_customer(SHOP, "gid://shopify/Customer/42")

        self.assertIsNotNone(c)
        self.assertEqual(c["id"], "gid://shopify/Customer/42")
        self.assertEqual(c["email"], "alice@example.com")

    @responses_lib.activate
    def test_fetch_customer_not_found(self):
        _mock_post({"data": {"customers": {"edges": []}}})

        c = shopify_conn.fetch_customer(SHOP, "nobody@example.com")
        self.assertIsNone(c)

    @responses_lib.activate
    def test_customer_to_markdown(self):
        _mock_post(_CUSTOMER_EMAIL_BODY)
        c = shopify_conn.fetch_customer(SHOP, "alice@example.com")
        md = shopify_conn.customer_to_markdown(c, "alice@example.com", SHOP)

        self.assertIn("# Shopify customer: alice@example.com", md)
        self.assertIn("Alice Smith", md)
        self.assertIn("245.00 USD", md)
        self.assertIn("ENABLED", md)

    def test_customer_to_markdown_not_found(self):
        md = shopify_conn.customer_to_markdown(None, "ghost@example.com", SHOP)
        self.assertIn("not found", md.lower())
        self.assertIn("ghost@example.com", md)


# ---------------------------------------------------------------------------
# Product tests
# ---------------------------------------------------------------------------


class ShopifyProduct(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_SHOPIFY")
        os.environ["RC_CONN_SHOPIFY"] = "shpat_test_dummy"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_SHOPIFY", None)
        else:
            os.environ["RC_CONN_SHOPIFY"] = self._saved

    @responses_lib.activate
    def test_fetch_product_by_gid(self):
        _mock_post(_PRODUCT_BODY)

        p = shopify_conn.fetch_product(SHOP, "gid://shopify/Product/7")

        self.assertIsNotNone(p)
        self.assertEqual(p["title"], "Widget Pro")
        self.assertEqual(p["handle"], "widget-pro")
        self.assertEqual(p["status"], "ACTIVE")
        self.assertEqual(len(p["variants"]), 1)
        v = p["variants"][0]
        self.assertEqual(v["price"], "49.99")
        self.assertEqual(v["sku"], "WP-001")
        self.assertEqual(v["inventory"], 120)
        self.assertTrue(v["available"])

    @responses_lib.activate
    def test_fetch_product_not_found(self):
        _mock_post({"data": {"product": None}})
        p = shopify_conn.fetch_product(SHOP, "gid://shopify/Product/999")
        self.assertIsNone(p)

    @responses_lib.activate
    def test_product_to_markdown(self):
        _mock_post(_PRODUCT_BODY)
        p = shopify_conn.fetch_product(SHOP, "gid://shopify/Product/7")
        md = shopify_conn.product_to_markdown(p, "gid://shopify/Product/7", SHOP)

        self.assertIn("# Shopify product: Widget Pro", md)
        self.assertIn("widget-pro", md)
        self.assertIn("ACTIVE", md)
        self.assertIn("49.99", md)
        self.assertIn("WP-001", md)
        self.assertIn("120", md)
        self.assertIn("in stock", md)

    def test_product_to_markdown_not_found(self):
        md = shopify_conn.product_to_markdown(None, "gid://shopify/Product/999", SHOP)
        self.assertIn("not found", md.lower())


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class ShopifyCLI(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_SHOPIFY")
        os.environ["RC_CONN_SHOPIFY"] = "shpat_test_dummy"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_SHOPIFY", None)
        else:
            os.environ["RC_CONN_SHOPIFY"] = self._saved

    @responses_lib.activate
    def test_cli_orders(self):
        _mock_post(_PAGE_1_BODY)
        _mock_post(_PAGE_2_BODY)
        rc = shopify_conn.main(["--shop", SHOP, "orders", "--limit", "10"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_cli_customer(self):
        _mock_post(_CUSTOMER_EMAIL_BODY)
        rc = shopify_conn.main(["--shop", SHOP, "customer", "--ref", "alice@example.com"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_product(self):
        _mock_post(_PRODUCT_BODY)
        rc = shopify_conn.main(["--shop", SHOP, "product", "--id", "gid://shopify/Product/7"])
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Token-prefix hygiene guard
# ---------------------------------------------------------------------------


class ShopifyTokenHygiene(unittest.TestCase):
    """CI guard: no real Shopify token prefix may land in the connector directory files.

    Scoped to the connector dir only (NOT this test file, which legitimately names the prefixes).
    Splits prefix literals with concatenation so this guard doesn't flag itself.
    """

    # Shopify offline/online token prefix, private-app token prefix.
    _TOKEN_PREFIXES = (
        "shpat" "_",    # offline/online access token (Admin API)
        "shpss" "_",    # session token
        "shpca" "_",    # custom app token
        "shppa" "_",    # private app password
    )

    def test_no_token_prefixes_in_shopify_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "shopify"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
