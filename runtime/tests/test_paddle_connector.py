"""Fixture tests for the Paddle connector (script connector — force-code triggers b + d).

Tests cover:
  - YAML loads via lib.api's manifest loader and maps every field correctly.
  - Paddle-specific pagination (_paddle_pages) stitches ≥2 pages correctly by following
    ``meta.pagination.next`` (a body-embedded full URL), gated by ``has_more``.
  - The bearer credential rides EVERY request including continuation pages.
  - api.pick selects the support-relevant fields.
  - resolve_customer works by ID and by email search.
  - support_summary does the multi-call join (customer + subscriptions + transactions).
  - summary_to_markdown renders correctly.
  - The connector CLI (main()) drives the customer command.
  - Token-prefix hygiene: no real Paddle API key prefix lands in the connector files.

No live creds, no network. HTTP is mocked with ``responses``.
Bodies mirror Paddle's documented example payloads, trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_paddle_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

# Import the connector AFTER lib (it registers the manifest).
import lib.connectors.paddle as paddle_connector  # noqa: E402

API_BASE = "https://api.paddle.com"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# ---------------------------------------------------------------------------

_CUSTOMER_1 = {
    "id": "ctm_01h8441jn5pcwrfhwh78jqt8hk",
    "email": "alice@example.com",
    "name": "Alice Example",
    "status": "active",
    "created_at": "2024-01-15T10:00:00Z",
    "locale": "en",
    "marketing_consent": False,
}

_SUB_1 = {
    "id": "sub_01h9jj8h8bnmf9c58pjiyf9jd",
    "status": "active",
    "billing_cycle": {"frequency": 1, "interval": "month"},
    "next_billed_at": "2026-07-15T10:00:00Z",
    "paused_at": None,
    "canceled_at": None,
    "items": [
        {
            "price": {
                "description": "Pro Monthly",
                "unit_price": {"amount": "4900", "currency_code": "USD"},
            }
        }
    ],
    "management_urls": {
        "cancel": "https://customer-portal.paddle.com/subscriptions/sub_01h9/cancel",
        "update_payment_method": "https://customer-portal.paddle.com/subscriptions/sub_01h9/update-payment-method",
    },
}

_TXN_1 = {
    "id": "txn_01h9jj8h8bnmf9c58pjiyf9jd",
    "status": "completed",
    "billed_at": "2026-06-15T10:00:00Z",
    "created_at": "2026-06-15T09:58:00Z",
    "details": {
        "totals": {
            "total": "4900",
            "currency_code": "USD",
            "tax": "0",
        }
    },
    "billing_period": {"starts_at": "2026-06-15T00:00:00Z", "ends_at": "2026-07-15T00:00:00Z"},
}

# Page-2 customer (for pagination test)
_CUSTOMER_2 = {
    "id": "ctm_02aaaa",
    "email": "bob@example.com",
    "name": "Bob Example",
    "status": "active",
    "created_at": "2024-02-01T10:00:00Z",
    "locale": "en",
    "marketing_consent": False,
}


def _page(items: list, has_more: bool, next_url: str | None = None) -> dict:
    """Build a Paddle-shaped response envelope."""
    return {
        "data": items,
        "meta": {
            "request_id": "req_test_01",
            "pagination": {
                "per_page": 50,
                "next": next_url,
                "has_more": has_more,
                "estimated_total": len(items),
            },
        },
    }


# ---------------------------------------------------------------------------
# Helper: restore manifests around each test
# ---------------------------------------------------------------------------

class _PaddleBase(unittest.TestCase):
    def setUp(self):
        # Preserve manifest registry state; paddle connector registers on import.
        self._saved_env = os.environ.get("RC_CONN_PADDLE")
        # Use a fake token with a split prefix so the hygiene guard can't flag this file itself.
        os.environ["RC_CONN_PADDLE"] = "pdl_sdbx_" + "apikey_testfakekey0000000000_FakeSecret22_abc"

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("RC_CONN_PADDLE", None)
        else:
            os.environ["RC_CONN_PADDLE"] = self._saved_env


# ---------------------------------------------------------------------------
# 1. Manifest loading
# ---------------------------------------------------------------------------

class TestPaddleManifest(_PaddleBase):
    def test_yaml_loads_and_maps_every_field(self):
        """YAML manifest loads via lib.api loader and maps every field."""
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        m = api.load_manifests()
        self.assertIn("paddle", m)
        p = m["paddle"]
        self.assertEqual(p.key, "paddle")
        self.assertEqual(p.base_url, "https://api.paddle.com")
        self.assertEqual(p.auth.strategy, "bearer")
        self.assertEqual(p.pagination.style, "cursor")
        self.assertEqual(p.pagination.cursor_param, "after")
        self.assertEqual(p.pagination.has_more_field, "meta.pagination.has_more")
        self.assertEqual(p.pagination.items_field, "data")
        self.assertEqual(p.pagination.page_size, 50)
        self.assertEqual(p.rate_limit_remaining_header, "")  # no remaining-count header

    def test_connector_registers_manifest(self):
        """Connector's register() call makes the manifest drivable via lib.api."""
        self.assertIn("paddle", api.MANIFESTS)
        m = api.MANIFESTS["paddle"]
        self.assertEqual(m.base_url, "https://api.paddle.com")


# ---------------------------------------------------------------------------
# 2. Paddle pagination: body-embedded next URL (trigger d)
# ---------------------------------------------------------------------------

class TestPaddlePagination(_PaddleBase):
    @responses_lib.activate
    def test_pagination_stitches_two_pages_via_body_next_url(self):
        """_paddle_pages follows meta.pagination.next as an absolute URL across 2 pages."""
        page2_url = f"{API_BASE}/customers?per_page=50&after=ctm_02aaaa"
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/customers",
            json=_page([_CUSTOMER_1], has_more=True, next_url=page2_url),
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            page2_url,
            json=_page([_CUSTOMER_2], has_more=False, next_url=None),
            status=200,
        )

        all_items = []
        for batch in paddle_connector._paddle_pages("/customers"):
            all_items.extend(batch)

        self.assertEqual(len(all_items), 2)
        self.assertEqual(all_items[0]["id"], "ctm_01h8441jn5pcwrfhwh78jqt8hk")
        self.assertEqual(all_items[1]["id"], "ctm_02aaaa")
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_bearer_credential_on_all_pages_including_continuation(self):
        """Bearer token rides every request, including the continuation (page 2)."""
        page2_url = f"{API_BASE}/customers?per_page=50&after=ctm_02aaaa"
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/customers",
            json=_page([_CUSTOMER_1], has_more=True, next_url=page2_url),
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            page2_url,
            json=_page([_CUSTOMER_2], has_more=False),
            status=200,
        )

        list(paddle_connector._paddle_pages("/customers"))

        for call in responses_lib.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(auth.startswith("Bearer "), f"Missing Bearer on {call.request.url}")
            self.assertIn("testfakekey", auth)

    @responses_lib.activate
    def test_single_page_no_continuation(self):
        """has_more=False on first page stops pagination immediately."""
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/customers",
            json=_page([_CUSTOMER_1], has_more=False),
            status=200,
        )

        all_items = []
        for batch in paddle_connector._paddle_pages("/customers"):
            all_items.extend(batch)

        self.assertEqual(len(all_items), 1)
        self.assertEqual(len(responses_lib.calls), 1)


# ---------------------------------------------------------------------------
# 3. api.pick on Paddle fields
# ---------------------------------------------------------------------------

class TestPaddlePick(_PaddleBase):
    def test_pick_selects_support_fields_from_customer(self):
        picked = api.pick(_CUSTOMER_1, "id,email,name,status")
        self.assertEqual(picked["id"], "ctm_01h8441jn5pcwrfhwh78jqt8hk")
        self.assertEqual(picked["email"], "alice@example.com")
        self.assertEqual(picked["status"], "active")

    def test_pick_nested_subscription_fields(self):
        picked = api.pick(_SUB_1, "id,status,next_billed_at,items.*.price.unit_price.amount")
        self.assertEqual(picked["id"], "sub_01h9jj8h8bnmf9c58pjiyf9jd")
        self.assertEqual(picked["status"], "active")
        self.assertEqual(picked["items.*.price.unit_price.amount"], ["4900"])

    def test_pick_transaction_totals(self):
        picked = api.pick(_TXN_1, "id,status,billed_at,details.totals.total,details.totals.currency_code")
        self.assertEqual(picked["id"], "txn_01h9jj8h8bnmf9c58pjiyf9jd")
        self.assertEqual(picked["details.totals.total"], "4900")
        self.assertEqual(picked["details.totals.currency_code"], "USD")


# ---------------------------------------------------------------------------
# 4. resolve_customer
# ---------------------------------------------------------------------------

class TestResolveCustomer(_PaddleBase):
    @responses_lib.activate
    def test_resolve_by_id(self):
        """Resolves customer directly by ctm_ ID via GET /customers/{id}."""
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/customers/ctm_01h8441jn5pcwrfhwh78jqt8hk",
            json={"data": _CUSTOMER_1, "meta": {"request_id": "req_01"}},
            status=200,
        )
        result = paddle_connector.resolve_customer("ctm_01h8441jn5pcwrfhwh78jqt8hk")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "ctm_01h8441jn5pcwrfhwh78jqt8hk")

    @responses_lib.activate
    def test_resolve_by_email_exact_match(self):
        """Resolves customer by email via /customers?search=, exact match preferred."""
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/customers",
            json=_page([_CUSTOMER_1], has_more=False),
            status=200,
        )
        result = paddle_connector.resolve_customer("alice@example.com")
        self.assertIsNotNone(result)
        self.assertEqual(result["email"], "alice@example.com")
        # Confirm search param was sent
        req_url = responses_lib.calls[0].request.url
        self.assertIn("search=", req_url)

    @responses_lib.activate
    def test_resolve_not_found_returns_none(self):
        """Returns None when no customer matches."""
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/customers",
            json=_page([], has_more=False),
            status=200,
        )
        result = paddle_connector.resolve_customer("nobody@example.com")
        self.assertIsNone(result)

    def test_resolve_empty_ref_raises(self):
        with self.assertRaises(RuntimeError):
            paddle_connector.resolve_customer("")


# ---------------------------------------------------------------------------
# 5. support_summary multi-call join (trigger b)
# ---------------------------------------------------------------------------

class TestSupportSummary(_PaddleBase):
    @responses_lib.activate
    def test_full_join_customer_subscriptions_transactions(self):
        """support_summary does the multi-call join and pre-selects fields."""
        cid = _CUSTOMER_1["id"]
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/customers/{cid}",
            json={"data": _CUSTOMER_1, "meta": {"request_id": "r1"}},
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/subscriptions",
            json=_page([_SUB_1], has_more=False),
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/transactions",
            json=_page([_TXN_1], has_more=False),
            status=200,
        )

        s = paddle_connector.support_summary(cid)
        self.assertTrue(s["found"])
        self.assertEqual(s["customer"]["id"], cid)
        self.assertEqual(s["customer"]["email"], "alice@example.com")
        self.assertEqual(len(s["subscriptions"]), 1)
        self.assertEqual(s["subscriptions"][0]["id"], "sub_01h9jj8h8bnmf9c58pjiyf9jd")
        self.assertEqual(s["subscriptions"][0]["status"], "active")
        self.assertEqual(len(s["recent_transactions"]), 1)
        self.assertEqual(s["recent_transactions"][0]["status"], "completed")
        # Three distinct endpoints were called.
        urls = [c.request.url for c in responses_lib.calls]
        self.assertTrue(any("/customers/" in u for u in urls))
        self.assertTrue(any("/subscriptions" in u for u in urls))
        self.assertTrue(any("/transactions" in u for u in urls))

    @responses_lib.activate
    def test_not_found_customer(self):
        """Returns found=False dict when customer not found."""
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/customers",
            json=_page([], has_more=False),
            status=200,
        )
        s = paddle_connector.support_summary("nobody@example.com")
        self.assertFalse(s["found"])
        self.assertEqual(s["ref"], "nobody@example.com")


# ---------------------------------------------------------------------------
# 6. summary_to_markdown rendering
# ---------------------------------------------------------------------------

class TestSummaryToMarkdown(_PaddleBase):
    def _make_summary(self):
        return {
            "found": True,
            "customer": api.pick(_CUSTOMER_1, "id,email,name,status,created_at,locale,marketing_consent"),
            "subscriptions": [api.pick(_SUB_1, "id,status,next_billed_at,paused_at,canceled_at")],
            "recent_transactions": [api.pick(_TXN_1, "id,status,billed_at,details.totals.total,details.totals.currency_code")],
        }

    def test_markdown_contains_customer_header(self):
        md = paddle_connector.summary_to_markdown(self._make_summary())
        self.assertIn("# Paddle:", md)
        self.assertIn("alice@example.com", md)

    def test_markdown_contains_subscription_status(self):
        md = paddle_connector.summary_to_markdown(self._make_summary())
        self.assertIn("active", md)
        self.assertIn("sub_01h9", md)

    def test_markdown_contains_transaction(self):
        md = paddle_connector.summary_to_markdown(self._make_summary())
        self.assertIn("txn_01h9", md)
        self.assertIn("completed", md)

    def test_not_found_markdown(self):
        md = paddle_connector.summary_to_markdown({"found": False, "ref": "nobody@example.com"})
        self.assertIn("not found", md.lower())
        self.assertIn("nobody@example.com", md)


# ---------------------------------------------------------------------------
# 7. CLI drive (connector main)
# ---------------------------------------------------------------------------

class TestPaddleCLI(_PaddleBase):
    @responses_lib.activate
    def test_cli_customer_command(self):
        """CLI 'customer' command calls support_summary and prints markdown."""
        cid = "ctm_01h8441jn5pcwrfhwh78jqt8hk"
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/customers/{cid}",
            json={"data": _CUSTOMER_1, "meta": {"request_id": "r1"}},
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/subscriptions",
            json=_page([_SUB_1], has_more=False),
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/transactions",
            json=_page([_TXN_1], has_more=False),
            status=200,
        )
        rc = paddle_connector.main(["customer", cid])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_lib_api_cli_drives_manifest(self):
        """python -m lib.api get paddle /customers works for manifest-only direct calls."""
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/customers",
            json=_page([_CUSTOMER_1], has_more=False),
            status=200,
        )
        rc = api._main(["get", "paddle", "/customers", "--pick", "id,email,status"])
        self.assertEqual(rc, 0)
        # Confirm the bearer credential rode the request.
        auth = responses_lib.calls[0].request.headers.get("Authorization", "")
        self.assertTrue(auth.startswith("Bearer "))


# ---------------------------------------------------------------------------
# 8. Token-prefix hygiene
# ---------------------------------------------------------------------------

class TestPaddleHygiene(unittest.TestCase):
    """CI guard: no real Paddle API key prefix may land in the connector files.

    Scoped to the connector dir (manifest + __init__ + __main__), NOT this test file — the test
    legitimately names the prefixes it hunts for, so scanning itself would be a false positive.
    """

    # Split the prefix literal so the guard can't flag this file itself.
    _TOKEN_PREFIXES = ("pdl_live_" "apikey",)  # concatenated string literal

    def test_no_token_prefixes_in_paddle_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "paddle"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix in (".pyc",):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref}")
        self.assertEqual(offenders, [], f"real token prefix found in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
