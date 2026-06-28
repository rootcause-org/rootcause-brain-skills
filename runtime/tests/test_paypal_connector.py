"""Fixture tests for the PayPal support connector.

``responses``-mocked, NO live creds, NO network. Bodies mirror PayPal's documented example
payloads trimmed to support-relevant fields.

Auth: ``oauth2_client_credentials`` — the host mints a bearer; the workspace injects it as
``RC_CONN_PAYPAL``. lib.api presents it as ``Authorization: Bearer …`` (same as ``bearer``
strategy). All assertions verify the credential rides every request.

Pagination: PayPal uses page-number pagination (page=1,2,3). The connector manages paging
manually via ``_paginate``; we exercise ≥2 pages in the dispute list test.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_paypal_connector.py -q
"""

import io
import os
import sys
import unittest
from pathlib import Path

import responses as rsps

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import paypal as pp  # noqa: E402

BASE = "https://api-m.paypal.com"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# ---------------------------------------------------------------------------

_ORDER_BODY = {
    "id": "5O190127TN364715T",
    "status": "COMPLETED",
    "intent": "CAPTURE",
    "create_time": "2018-04-01T21:18:49Z",
    "update_time": "2018-04-01T21:20:49Z",
    "payer": {
        "email_address": "buyer@example.com",
        "payer_id": "QYR5Z8XDVJNXQ",
        "name": {"given_name": "John", "surname": "Doe"},
    },
    "purchase_units": [
        {
            "reference_id": "d9f80740-38f0-11e8-b467-0ed5f89f718b",
            "amount": {"currency_code": "USD", "value": "100.00"},
        }
    ],
    "links": [{"href": f"{BASE}/v2/checkout/orders/5O190127TN364715T", "rel": "self", "method": "GET"}],
}

_DISPUTE_BODY = {
    "dispute_id": "PP-D-27803",
    "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
    "status": "OPEN",
    "dispute_state": "REQUIRED_ACTION",
    "dispute_life_cycle_stage": "INQUIRY",
    "create_time": "2019-04-11T04:31:59.000Z",
    "update_time": "2019-04-11T04:31:59.000Z",
    "dispute_amount": {"currency_code": "USD", "value": "95.00"},
    "dispute_outcome": {
        "outcome_code": "RESOLVED_BUYER_FAVOUR",
        "amount_refunded": {"currency_code": "USD", "value": "95.00"},
    },
    "disputed_transactions": [
        {
            "buyer_transaction_id": "3B3867503U7064535",
            "seller_transaction_id": "7S666543B0754812V",
        }
    ],
    "links": [
        {"href": f"{BASE}/v1/customer/disputes/PP-D-27803", "rel": "self", "method": "GET"}
    ],
}

# Two pages of dispute summaries (list endpoint).
_DISPUTES_PAGE_1 = {
    "items": [
        {
            "dispute_id": "PP-D-27803",
            "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
            "status": "OPEN",
            "dispute_amount": {"currency_code": "USD", "value": "95.00"},
            "create_time": "2019-04-11T04:31:59.000Z",
            "update_time": "2019-04-11T04:31:59.000Z",
            "links": [],
        }
    ],
    "total_items": 2,
    "total_pages": 2,
    "links": [
        {
            "href": f"{BASE}/v1/customer/disputes?page=2&page_size=20",
            "rel": "next",
            "method": "GET",
        }
    ],
}

_DISPUTES_PAGE_2 = {
    "items": [
        {
            "dispute_id": "PP-D-99999",
            "reason": "UNAUTHORISED",
            "status": "RESOLVED",
            "dispute_amount": {"currency_code": "USD", "value": "50.00"},
            "create_time": "2019-05-01T10:00:00.000Z",
            "update_time": "2019-05-02T10:00:00.000Z",
            "links": [],
        }
    ],
    "total_items": 2,
    "total_pages": 2,
    "links": [],
}

_SUBSCRIPTION_BODY = {
    "id": "I-BW452GLLEP1G",
    "plan_id": "P-5ML4271244454362WXNWU5NQ",
    "status": "ACTIVE",
    "quantity": "1",
    "start_time": "2019-08-01T00:00:00Z",
    "create_time": "2019-08-01T00:00:00Z",
    "update_time": "2019-08-02T00:00:00Z",
    "subscriber": {
        "email_address": "subscriber@example.com",
        "payer_id": "2J6QB8YJQSJRJ",
        "name": {"given_name": "Jane", "surname": "Smith"},
        "shipping_address": {},
    },
    "billing_info": {
        "outstanding_balance": {"currency_code": "USD", "value": "0.00"},
        "next_billing_time": "2020-09-01T00:00:00Z",
        "failed_payments_count": 0,
        "last_payment": {
            "amount": {"currency_code": "USD", "value": "15.00"},
            "time": "2019-08-01T00:00:00Z",
        },
    },
    "links": [
        {"href": f"{BASE}/v1/billing/subscriptions/I-BW452GLLEP1G", "rel": "self", "method": "GET"}
    ],
}


class PayPalManifest(unittest.TestCase):
    """Manifest loads via the YAML loader and maps every field correctly."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_PAYPAL")
        os.environ["RC_CONN_PAYPAL"] = "paypal_test_bearer_tok"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_PAYPAL", None)
        else:
            os.environ["RC_CONN_PAYPAL"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        manifests = api.load_manifests()
        self.assertIn("paypal", manifests)
        m = manifests["paypal"]
        self.assertEqual(m.key, "paypal")
        self.assertEqual(m.base_url, "https://api-m.paypal.com")
        self.assertEqual(m.auth.strategy, "oauth2_client_credentials")
        self.assertEqual(m.pagination.style, "none")
        # No remaining-count header — PayPal uses 429 + Retry-After only.
        self.assertEqual(m.rate_limit_remaining_header, "")

    def test_manifest_connector_module_declared(self):
        """Catalog field connector_module is present in raw YAML (lib.api ignores catalog-only keys
        but the host migration seed reads it; we verify it's non-empty in the raw file)."""
        import yaml

        manifest_path = (
            Path(__file__).resolve().parents[1] / "lib" / "connectors" / "paypal" / "manifest.yaml"
        )
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(raw.get("connector_module"), "lib.connectors.paypal")
        self.assertIn("oauth", raw)
        self.assertEqual(raw["oauth"]["token_url"], "https://api-m.paypal.com/v1/oauth2/token")
        self.assertIn("token", raw.get("kinds", []))
        self.assertIn("oauth", raw.get("kinds", []))


class PayPalOrderSummary(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_PAYPAL")
        os.environ["RC_CONN_PAYPAL"] = "paypal_test_bearer_tok"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_PAYPAL", None)
        else:
            os.environ["RC_CONN_PAYPAL"] = self._saved

    @rsps.activate
    def test_order_summary_fields_and_bearer(self):
        rsps.add(
            rsps.GET,
            f"{BASE}/v2/checkout/orders/5O190127TN364715T",
            json=_ORDER_BODY,
            status=200,
        )
        s = pp.order_summary("5O190127TN364715T")
        self.assertTrue(s["found"])
        self.assertEqual(s["order"]["id"], "5O190127TN364715T")
        self.assertEqual(s["order"]["status"], "COMPLETED")
        self.assertEqual(s["payer"]["email"], "buyer@example.com")
        self.assertEqual(s["payer"]["name"], "John Doe")
        self.assertEqual(s["amount"]["value"], "100.00")
        self.assertEqual(s["amount"]["currency_code"], "USD")
        # oauth2_client_credentials injects a bearer — verify credential on the wire.
        self.assertEqual(
            rsps.calls[0].request.headers["Authorization"],
            "Bearer paypal_test_bearer_tok",
        )

    @rsps.activate
    def test_order_not_found_returns_found_false(self):
        rsps.add(
            rsps.GET,
            f"{BASE}/v2/checkout/orders/MISSING",
            json={"name": "RESOURCE_NOT_FOUND", "message": "order not found"},
            status=404,
        )
        s = pp.order_summary("MISSING")
        self.assertFalse(s["found"])
        self.assertEqual(s["order_id"], "MISSING")

    @rsps.activate
    def test_order_to_markdown(self):
        rsps.add(
            rsps.GET,
            f"{BASE}/v2/checkout/orders/5O190127TN364715T",
            json=_ORDER_BODY,
            status=200,
        )
        md = pp.order_to_markdown(pp.order_summary("5O190127TN364715T"))
        self.assertIn("# PayPal Order: 5O190127TN364715T", md)
        self.assertIn("Status: **COMPLETED**", md)
        self.assertIn("100.00 USD", md)
        self.assertIn("buyer@example.com", md)


class PayPalDisputeSummary(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_PAYPAL")
        os.environ["RC_CONN_PAYPAL"] = "paypal_test_bearer_tok"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_PAYPAL", None)
        else:
            os.environ["RC_CONN_PAYPAL"] = self._saved

    @rsps.activate
    def test_dispute_summary_fields(self):
        rsps.add(
            rsps.GET,
            f"{BASE}/v1/customer/disputes/PP-D-27803",
            json=_DISPUTE_BODY,
            status=200,
        )
        s = pp.dispute_summary("PP-D-27803")
        self.assertTrue(s["found"])
        self.assertEqual(s["dispute"]["dispute_id"], "PP-D-27803")
        self.assertEqual(s["dispute"]["reason"], "MERCHANDISE_OR_SERVICE_NOT_RECEIVED")
        self.assertEqual(s["dispute"]["status"], "OPEN")
        self.assertEqual(s["amount"]["value"], "95.00")
        self.assertEqual(s["outcome"]["code"], "RESOLVED_BUYER_FAVOUR")
        self.assertEqual(s["transaction_id"], "3B3867503U7064535")
        # bearer on every request
        self.assertEqual(
            rsps.calls[0].request.headers["Authorization"],
            "Bearer paypal_test_bearer_tok",
        )

    @rsps.activate
    def test_list_disputes_stitches_two_pages(self):
        """_paginate advances page=1 then page=2 (page-number, not item-count offset)."""
        rsps.add(
            rsps.GET,
            f"{BASE}/v1/customer/disputes",
            json=_DISPUTES_PAGE_1,
            status=200,
        )
        rsps.add(
            rsps.GET,
            f"{BASE}/v1/customer/disputes",
            json=_DISPUTES_PAGE_2,
            status=200,
        )
        disputes = pp.list_disputes()
        self.assertEqual(len(disputes), 2)
        self.assertEqual(disputes[0]["dispute_id"], "PP-D-27803")
        self.assertEqual(disputes[1]["dispute_id"], "PP-D-99999")
        # Verify page numbers were sent (not item-count offsets).
        call1_params = rsps.calls[0].request.url
        call2_params = rsps.calls[1].request.url
        self.assertIn("page=1", call1_params)
        self.assertIn("page=2", call2_params)
        # Bearer on both pages.
        self.assertEqual(
            rsps.calls[0].request.headers["Authorization"],
            "Bearer paypal_test_bearer_tok",
        )
        self.assertEqual(
            rsps.calls[1].request.headers["Authorization"],
            "Bearer paypal_test_bearer_tok",
        )

    @rsps.activate
    def test_dispute_to_markdown(self):
        rsps.add(
            rsps.GET,
            f"{BASE}/v1/customer/disputes/PP-D-27803",
            json=_DISPUTE_BODY,
            status=200,
        )
        md = pp.dispute_to_markdown(pp.dispute_summary("PP-D-27803"))
        self.assertIn("# PayPal Dispute: PP-D-27803", md)
        self.assertIn("Reason: **MERCHANDISE_OR_SERVICE_NOT_RECEIVED**", md)
        self.assertIn("Status: **OPEN**", md)
        self.assertIn("95.00 USD", md)
        self.assertIn("RESOLVED_BUYER_FAVOUR", md)


class PayPalSubscriptionSummary(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_PAYPAL")
        os.environ["RC_CONN_PAYPAL"] = "paypal_test_bearer_tok"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_PAYPAL", None)
        else:
            os.environ["RC_CONN_PAYPAL"] = self._saved

    @rsps.activate
    def test_subscription_summary_fields(self):
        rsps.add(
            rsps.GET,
            f"{BASE}/v1/billing/subscriptions/I-BW452GLLEP1G",
            json=_SUBSCRIPTION_BODY,
            status=200,
        )
        s = pp.subscription_summary("I-BW452GLLEP1G")
        self.assertTrue(s["found"])
        self.assertEqual(s["subscription"]["id"], "I-BW452GLLEP1G")
        self.assertEqual(s["subscription"]["status"], "ACTIVE")
        self.assertEqual(s["subscription"]["plan_id"], "P-5ML4271244454362WXNWU5NQ")
        self.assertEqual(s["subscriber"]["email"], "subscriber@example.com")
        self.assertEqual(s["subscriber"]["name"], "Jane Smith")
        self.assertEqual(s["billing"]["next_billing_time"], "2020-09-01T00:00:00Z")
        self.assertEqual(s["billing"]["last_payment_amount"]["value"], "15.00")
        self.assertEqual(s["billing"]["failed_payments_count"], 0)
        # Bearer on every request.
        self.assertEqual(
            rsps.calls[0].request.headers["Authorization"],
            "Bearer paypal_test_bearer_tok",
        )

    @rsps.activate
    def test_subscription_to_markdown(self):
        rsps.add(
            rsps.GET,
            f"{BASE}/v1/billing/subscriptions/I-BW452GLLEP1G",
            json=_SUBSCRIPTION_BODY,
            status=200,
        )
        md = pp.subscription_to_markdown(pp.subscription_summary("I-BW452GLLEP1G"))
        self.assertIn("# PayPal Subscription: I-BW452GLLEP1G", md)
        self.assertIn("Status: **ACTIVE**", md)
        self.assertIn("subscriber@example.com", md)
        self.assertIn("Next billing: 2020-09-01T00:00:00Z", md)
        self.assertIn("15.00 USD", md)


class PayPalPickIntegration(unittest.TestCase):
    """api.pick selects support-relevant fields from a raw PayPal object."""

    def test_pick_order_fields(self):
        picked = api.pick(_ORDER_BODY, "id,status,payer.email_address,purchase_units.*.amount.value")
        self.assertEqual(picked["id"], "5O190127TN364715T")
        self.assertEqual(picked["status"], "COMPLETED")
        self.assertEqual(picked["payer.email_address"], "buyer@example.com")
        self.assertEqual(picked["purchase_units.*.amount.value"], ["100.00"])

    def test_pick_dispute_fields(self):
        picked = api.pick(_DISPUTE_BODY, "dispute_id,reason,status,dispute_amount.value")
        self.assertEqual(picked["dispute_id"], "PP-D-27803")
        self.assertEqual(picked["reason"], "MERCHANDISE_OR_SERVICE_NOT_RECEIVED")
        self.assertEqual(picked["dispute_amount.value"], "95.00")


class PayPalCLI(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_PAYPAL")
        os.environ["RC_CONN_PAYPAL"] = "paypal_test_bearer_tok"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_PAYPAL", None)
        else:
            os.environ["RC_CONN_PAYPAL"] = self._saved

    @rsps.activate
    def test_cli_order(self):
        rsps.add(
            rsps.GET,
            f"{BASE}/v2/checkout/orders/5O190127TN364715T",
            json=_ORDER_BODY,
            status=200,
        )
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pp.main(["order", "5O190127TN364715T"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("5O190127TN364715T", out)
        self.assertIn("COMPLETED", out)

    @rsps.activate
    def test_cli_dispute(self):
        rsps.add(
            rsps.GET,
            f"{BASE}/v1/customer/disputes/PP-D-27803",
            json=_DISPUTE_BODY,
            status=200,
        )
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pp.main(["dispute", "PP-D-27803"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("PP-D-27803", out)

    @rsps.activate
    def test_cli_subscription(self):
        rsps.add(
            rsps.GET,
            f"{BASE}/v1/billing/subscriptions/I-BW452GLLEP1G",
            json=_SUBSCRIPTION_BODY,
            status=200,
        )
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pp.main(["subscription", "I-BW452GLLEP1G"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("I-BW452GLLEP1G", out)
        self.assertIn("ACTIVE", out)

    @rsps.activate
    def test_lib_api_cli_drives_paypal_single_get(self):
        """The generic `python -m lib.api get paypal <path>` works for ad-hoc single-object reads."""
        rsps.add(
            rsps.GET,
            f"{BASE}/v2/checkout/orders/5O190127TN364715T",
            json=_ORDER_BODY,
            status=200,
        )
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        rc = api._main(
            ["get", "paypal", "v2/checkout/orders/5O190127TN364715T", "--pick", "id,status"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            rsps.calls[0].request.headers["Authorization"],
            "Bearer paypal_test_bearer_tok",
        )


class PayPalTokenHygiene(unittest.TestCase):
    """CI guard: no real PayPal credential prefix may land in the connector directory."""

    # PayPal access tokens start with a long alphanumeric string; restrict to client_id prefixes
    # and common test token patterns. Split literals so this guard doesn't flag itself.
    _TOKEN_PREFIXES = (
        "A21AA" + "FkpX",  # PayPal sandbox client id prefix pattern
        "Bearer " + "A21AA",  # real bearer prefix pattern
    )

    def test_no_token_prefixes_in_paypal_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "paypal"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
