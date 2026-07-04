"""Fixture tests for the Mollie integration.

Mollie is a script connector because list responses put items under a variable HAL envelope
(``_embedded.payments``, ``_embedded.refunds``, ...). Tests use mocked HTTP only.
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import api  # noqa: E402
from lib.action import mollie as action_mollie  # noqa: E402
from lib.connectors import mollie  # noqa: E402

API_BASE = "https://api.mollie.com/v2"

_PAYMENT_1 = {
    "resource": "payment",
    "id": "tr_7UhSN1zuXS",
    "mode": "test",
    "createdAt": "2026-07-01T10:00:00Z",
    "paidAt": "2026-07-01T10:01:00Z",
    "status": "paid",
    "isCancelable": False,
    "amount": {"value": "75.00", "currency": "EUR"},
    "amountRefunded": {"value": "10.00", "currency": "EUR"},
    "amountRemaining": {"value": "65.00", "currency": "EUR"},
    "description": "Order #123",
    "method": "ideal",
    "profileId": "pfl_QkEhN94Ba",
}

_PAYMENT_2 = {
    "resource": "payment",
    "id": "tr_WDqYK6vllg",
    "mode": "test",
    "createdAt": "2026-07-02T10:00:00Z",
    "status": "paid",
    "amount": {"value": "24.00", "currency": "EUR"},
    "description": "Order #124",
}

_REFUND_1 = {
    "resource": "refund",
    "id": "re_4qqhO89gsT",
    "paymentId": "tr_7UhSN1zuXS",
    "mode": "test",
    "createdAt": "2026-07-03T10:00:00Z",
    "status": "refunded",
    "amount": {"value": "10.00", "currency": "EUR"},
    "description": "Partial refund",
}


def _page(resource: str, items: list, next_url: str | None = None) -> dict:
    return {
        "count": len(items),
        "_embedded": {resource: items},
        "_links": {
            "self": {"href": f"{API_BASE}/{resource}", "type": "application/hal+json"},
            "previous": None,
            "next": {"href": next_url, "type": "application/hal+json"} if next_url else None,
        },
    }


class _MollieBase(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "RC_CONN_MOLLIE": os.environ.get("RC_CONN_MOLLIE"),
            "RC_ACTION_MOLLIE": os.environ.get("RC_ACTION_MOLLIE"),
            "RC_API_BROKERED_KEYS": os.environ.get("RC_API_BROKERED_KEYS"),
        }
        os.environ["RC_CONN_MOLLIE"] = "test_" + "fake_mollie_key"
        os.environ["RC_ACTION_MOLLIE"] = "access_" + "fake_action_token"
        os.environ.pop("RC_API_BROKERED_KEYS", None)
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        action_mollie._client.cache_clear()
        api.register(mollie.MANIFEST)

    def tearDown(self):
        action_mollie._client.cache_clear()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestMollieManifest(_MollieBase):
    def test_yaml_loads_and_maps_fields(self):
        self.assertIn("mollie", api.MANIFESTS)
        m = api.MANIFESTS["mollie"]
        self.assertEqual(m.base_url, API_BASE)
        self.assertEqual(m.auth.strategy, "bearer")
        self.assertEqual(m.pagination.style, "body_url")
        self.assertEqual(m.pagination.next_url_field, "_links.next.href")
        self.assertEqual(m.pagination.page_size, 250)


class TestMollieReadConnector(_MollieBase):
    @responses_lib.activate
    def test_list_payments_follows_hal_next_url_and_picks_fields(self):
        page2_url = f"{API_BASE}/payments?from=tr_WDqYK6vllg&limit=50"
        responses_lib.add(responses_lib.GET, f"{API_BASE}/payments", json=_page("payments", [_PAYMENT_1], page2_url), status=200)
        responses_lib.add(responses_lib.GET, page2_url, json=_page("payments", [_PAYMENT_2]), status=200)

        result = mollie.list_resource("payments", query={"limit": "50"}, max_pages=5)
        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual([p["id"] for p in result["items"]], ["tr_7UhSN1zuXS", "tr_WDqYK6vllg"])
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers.get("Authorization"), "Bearer test_fake_mollie_key")

        picked = api.pick(result["items"][0], mollie._PICK_FIELDS["payments"])
        self.assertEqual(picked["amount.value"], "75.00")
        self.assertEqual(picked["amountRemaining.value"], "65.00")

    @responses_lib.activate
    def test_list_payments_rejects_cross_origin_next_url(self):
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/payments",
            json=_page("payments", [_PAYMENT_1], "https://evil.example/payments?from=tr_7UhSN1zuXS"),
            status=200,
        )

        result = mollie.list_resource("payments", query={"limit": "50"}, max_pages=5)

        self.assertTrue(result["incomplete"])
        self.assertIn("escaped Mollie API origin", result["reason"])
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_payment_refund_plan_reads_payment_and_existing_refunds(self):
        responses_lib.add(responses_lib.GET, f"{API_BASE}/payments/tr_7UhSN1zuXS", json=_PAYMENT_1, status=200)
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/payments/tr_7UhSN1zuXS/refunds",
            json=_page("refunds", [_REFUND_1]),
            status=200,
        )

        plan = mollie.refund_plan("tr_7UhSN1zuXS", amount="12.34", currency="EUR", description="Goodwill refund")

        self.assertEqual(plan["payment"]["id"], "tr_7UhSN1zuXS")
        self.assertEqual(plan["available_amount"], {"value": "65.00", "currency": "EUR"})
        self.assertEqual(plan["existing_refunds"][0]["id"], "re_4qqhO89gsT")
        self.assertEqual(plan["action_capability"], "mollie.write")
        checks = {check["name"]: check["ok"] for check in plan["checks"]}
        self.assertTrue(checks["payment_status"])
        self.assertTrue(checks["refund_history_complete"])
        self.assertTrue(checks["currency_matches"])
        self.assertTrue(checks["amount_within_remaining"])

    @responses_lib.activate
    def test_refund_plan_marks_incomplete_refund_history_unsafe(self):
        responses_lib.add(responses_lib.GET, f"{API_BASE}/payments/tr_7UhSN1zuXS", json=_PAYMENT_1, status=200)
        responses_lib.add(responses_lib.GET, f"{API_BASE}/payments/tr_7UhSN1zuXS/refunds", body="nope", status=500)

        plan = mollie.refund_plan("tr_7UhSN1zuXS", amount="12.34", currency="EUR")

        checks = {check["name"]: check for check in plan["checks"]}
        self.assertFalse(checks["refund_history_complete"]["ok"])
        self.assertIn("HTTP 500", checks["refund_history_complete"]["observed"])


class TestMollieActionHelper(_MollieBase):
    @responses_lib.activate
    def test_create_payment_refund_uses_action_credential_and_idempotency_key(self):
        responses_lib.add(
            responses_lib.POST,
            f"{API_BASE}/payments/tr_7UhSN1zuXS/refunds",
            json={
                "resource": "refund",
                "id": "re_4qqhO89gsT",
                "status": "pending",
                "amount": {"value": "12.34", "currency": "EUR"},
            },
            status=201,
        )

        refund = action_mollie.create_payment_refund(
            payment_id="tr_7UhSN1zuXS",
            amount_value="12.34",
            currency="EUR",
            description="Goodwill refund",
            idempotency_key="refund-tr_7UhSN1zuXS-1234",
        )

        self.assertEqual(refund.id, "re_4qqhO89gsT")
        self.assertEqual(refund.status, "pending")
        req = responses_lib.calls[0].request
        self.assertEqual(req.headers.get("Authorization"), "Bearer access_fake_action_token")
        self.assertEqual(req.headers.get("Idempotency-Key"), "refund-tr_7UhSN1zuXS-1234")
        self.assertIn(b'"description": "Goodwill refund"', req.body)


if __name__ == "__main__":
    unittest.main()
