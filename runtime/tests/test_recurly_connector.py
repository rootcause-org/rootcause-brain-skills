"""Fixture test for the Recurly support connector.

Script connector — force-code triggers (a) field pre-selection and (d) non-standard pagination:
Recurly v3 returns ``has_more`` + ``next`` (a relative path like ``/accounts?cursor=abc&limit=200``)
inside the JSON response body, not in an HTTP Link header and not as an opaque cursor token.

No live creds, no network: HTTP is mocked with ``responses``. Bodies mirror Recurly's documented
v2021-02-25 API example payloads, trimmed to support-relevant fields.

Tests cover:
- YAML loads via lib.api's loader and maps every runtime field.
- basic auth credential rides every request (initial + pagination follow).
- Two-page account list exercises the has_more + next body pagination.
- support_summary joins account → subscriptions → invoice → transactions.
- summary_to_markdown renders expected markdown sections.
- api.pick works on pre-selected dicts.
- CLI main() drives account subcommand and returns 0.
- Token-prefix hygiene guard (scoped to the recurly connector dir only).

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_recurly_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import recurly as rc  # noqa: E402

BASE = "https://v3.recurly.com"

# Documented example payloads trimmed to support-relevant fields.
# Shapes mirror Recurly v2021-02-25 API response objects.

_ACCOUNT = {
    "object": "account",
    "id": "abcd1234-0000-0000-0000-000000000001",
    "code": "usr_42",
    "email": "alice@example.com",
    "company": "Acme Corp",
    "state": "active",
    "balance": {"amount": 0.0, "currency": "USD"},
    "created_at": "2024-01-15T10:00:00Z",
}

_SUBSCRIPTION_1 = {
    "object": "subscription",
    "id": "sub_aaaabbbb-0001",
    "state": "active",
    "plan": {"code": "pro_monthly", "name": "Pro Monthly"},
    "quantity": 1,
    "unit_amount": 29.99,
    "currency": "USD",
    "current_period_started_at": "2026-06-01T00:00:00Z",
    "current_period_ends_at": "2026-07-01T00:00:00Z",
    "trial_ends_at": None,
    "cancel_at_period_end": False,
    "canceled_at": None,
    "expires_at": None,
}

_INVOICE_1 = {
    "object": "invoice",
    "id": "inv-0001",
    "number": "1234",
    "state": "paid",
    "type": "charge",
    "subtotal": 29.99,
    "tax": 0.0,
    "total": 29.99,
    "currency": "USD",
    "net_terms": 0,
    "due_at": "2026-06-01T00:00:00Z",
    "closed_at": "2026-06-01T12:00:00Z",
    "created_at": "2026-06-01T00:00:00Z",
}

_TRANSACTION_1 = {
    "object": "transaction",
    "id": "txn-0001",
    "type": "purchase",
    "status": "success",
    "amount": 29.99,
    "currency": "USD",
    "success": True,
    "payment_method": {
        "object": "billing_info",
        "card_type": "Visa",
        "last_four": "4242",
    },
    "gateway_message": None,
    "status_code": None,
    "created_at": "2026-06-01T12:00:00Z",
}

_TRANSACTION_2 = {
    "object": "transaction",
    "id": "txn-0002",
    "type": "purchase",
    "status": "declined",
    "amount": 29.99,
    "currency": "USD",
    "success": False,
    "payment_method": {"object": "billing_info", "card_type": "Visa", "last_four": "4242"},
    "gateway_message": "Insufficient funds",
    "status_code": "4000",
    "created_at": "2026-05-01T12:00:00Z",
}

# Two-page account list fixtures for pagination test.
_ACCOUNTS_PAGE_1 = {
    "object": "list",
    "has_more": True,
    "next": "/accounts?cursor=abc123&limit=200",
    "data": [_ACCOUNT],
}
_ACCOUNTS_PAGE_2 = {
    "object": "list",
    "has_more": False,
    "next": None,
    "data": [
        {
            "object": "account",
            "id": "abcd1234-0000-0000-0000-000000000002",
            "code": "usr_99",
            "email": "bob@example.com",
            "company": None,
            "state": "active",
            "balance": {"amount": 10.0, "currency": "USD"},
            "created_at": "2024-02-01T10:00:00Z",
        }
    ],
}

_SUBS_RESPONSE = {"object": "list", "has_more": False, "next": None, "data": [_SUBSCRIPTION_1]}
_INVOICES_RESPONSE = {"object": "list", "has_more": False, "next": None, "data": [_INVOICE_1]}
_TRANSACTIONS_RESPONSE = {
    "object": "list",
    "has_more": False,
    "next": None,
    "data": [_TRANSACTION_1, _TRANSACTION_2],
}
_SUBS_EMPTY = {"object": "list", "has_more": False, "next": None, "data": []}


class RecurlyManifestLoad(unittest.TestCase):
    """YAML manifest loads correctly via lib.api's loader."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("recurly", m)
        r = m["recurly"]
        self.assertEqual(r.base_url, "https://v3.recurly.com")
        self.assertEqual(r.auth.strategy, "basic")
        self.assertEqual(r.pagination.style, "none")
        self.assertEqual(r.pagination.items_field, "data")
        self.assertEqual(r.rate_limit_remaining_header, "X-RateLimit-Remaining")
        self.assertIn("Accept", r.default_headers)
        self.assertIn("recurly", r.default_headers["Accept"])


class RecurlyPaginationAndAuth(unittest.TestCase):
    """Pagination follows body-embedded next paths; basic auth rides every request."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_RECURLY")
        # Use a format that encodes cleanly as Basic: "apikey:"
        os.environ["RC_CONN_RECURLY"] = "test_api_key_dummy"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_RECURLY", None)
        else:
            os.environ["RC_CONN_RECURLY"] = self._saved

    @responses_lib.activate
    def test_two_page_list_stitches_items(self):
        """_paginate follows has_more + next relative path across two pages."""
        responses_lib.add(
            responses_lib.GET,
            f"{BASE}/accounts",
            json=_ACCOUNTS_PAGE_1,
            status=200,
            headers={"X-RateLimit-Remaining": "1999"},
        )
        # Page 2: the `next` path from page 1 is `/accounts?cursor=abc123&limit=200`.
        # lib.api joins it to base_url, resulting in BASE + /accounts?cursor=abc123&limit=200.
        responses_lib.add(
            responses_lib.GET,
            f"{BASE}/accounts",
            json=_ACCOUNTS_PAGE_2,
            status=200,
            headers={"X-RateLimit-Remaining": "1998"},
        )

        api.load_manifests()
        items = rc._paginate("/accounts", max_items=400)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["code"], "usr_42")
        self.assertEqual(items[1]["code"], "usr_99")

    @responses_lib.activate
    def test_basic_auth_on_every_request(self):
        """Basic auth Authorization header is present on every request including pagination."""
        import base64

        expected_auth = "Basic " + base64.b64encode(b"test_api_key_dummy:").decode()

        responses_lib.add(responses_lib.GET, f"{BASE}/accounts", json=_ACCOUNTS_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, f"{BASE}/accounts", json=_ACCOUNTS_PAGE_2, status=200)

        api.load_manifests()
        rc._paginate("/accounts", max_items=400)

        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers.get("Authorization"), expected_auth)

    @responses_lib.activate
    def test_accept_header_on_every_request(self):
        """Recurly-version Accept header is present on every request."""
        responses_lib.add(responses_lib.GET, f"{BASE}/accounts", json=_ACCOUNTS_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, f"{BASE}/accounts", json=_ACCOUNTS_PAGE_2, status=200)

        api.load_manifests()
        rc._paginate("/accounts", max_items=400)

        for call in responses_lib.calls:
            accept = call.request.headers.get("Accept", "")
            self.assertIn("recurly", accept.lower())


class RecurlySupportSummary(unittest.TestCase):
    """support_summary joins account → subscriptions → invoice → transactions."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_RECURLY")
        os.environ["RC_CONN_RECURLY"] = "test_api_key_dummy"
        api.load_manifests()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_RECURLY", None)
        else:
            os.environ["RC_CONN_RECURLY"] = self._saved

    @responses_lib.activate
    def test_not_found_returns_found_false(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/accounts/ghost_99", json={}, status=404)
        s = rc.support_summary("ghost_99")
        self.assertFalse(s["found"])
        self.assertEqual(s["ref"], "ghost_99")

    @responses_lib.activate
    def test_full_join_fields(self):
        """All four calls are made; result contains account, subs, invoice, transactions."""
        responses_lib.add(responses_lib.GET, f"{BASE}/accounts/usr_42", json=_ACCOUNT, status=200)
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts/usr_42/subscriptions",
            json=_SUBS_RESPONSE, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts/usr_42/invoices",
            json=_INVOICES_RESPONSE, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts/usr_42/transactions",
            json=_TRANSACTIONS_RESPONSE, status=200,
        )

        s = rc.support_summary("usr_42")

        self.assertTrue(s["found"])
        self.assertEqual(s["account"]["email"], "alice@example.com")
        self.assertEqual(s["account"]["state"], "active")
        self.assertEqual(len(s["subscriptions"]), 1)
        self.assertEqual(s["subscriptions"][0]["plan_code"], "pro_monthly")
        self.assertEqual(s["subscriptions"][0]["state"], "active")
        self.assertIsNotNone(s["latest_invoice"])
        self.assertEqual(s["latest_invoice"]["number"], "1234")
        self.assertEqual(s["latest_invoice"]["state"], "paid")
        self.assertEqual(len(s["recent_transactions"]), 2)
        self.assertEqual(s["recent_transactions"][0]["status"], "success")
        self.assertTrue(s["recent_transactions"][0]["success"])
        self.assertEqual(s["recent_transactions"][1]["gateway_message"], "Insufficient funds")

    @responses_lib.activate
    def test_fallback_to_all_states_when_no_active_subs(self):
        """Falls back to all-states subscription list when active state returns empty."""
        responses_lib.add(responses_lib.GET, f"{BASE}/accounts/usr_42", json=_ACCOUNT, status=200)
        # First call (state=active) returns empty; second (no filter) returns one sub.
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts/usr_42/subscriptions",
            json=_SUBS_EMPTY, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts/usr_42/subscriptions",
            json=_SUBS_RESPONSE, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts/usr_42/invoices",
            json=_INVOICES_RESPONSE, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts/usr_42/transactions",
            json=_TRANSACTIONS_RESPONSE, status=200,
        )

        s = rc.support_summary("usr_42")
        self.assertTrue(s["found"])
        self.assertEqual(len(s["subscriptions"]), 1)
        self.assertEqual(s["subscriptions"][0]["plan_code"], "pro_monthly")


class RecurlyMarkdown(unittest.TestCase):
    """summary_to_markdown renders expected sections."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_RECURLY")
        os.environ["RC_CONN_RECURLY"] = "test_api_key_dummy"
        api.load_manifests()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_RECURLY", None)
        else:
            os.environ["RC_CONN_RECURLY"] = self._saved

    @responses_lib.activate
    def test_markdown_has_expected_sections(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/accounts/usr_42", json=_ACCOUNT, status=200)
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts/usr_42/subscriptions",
            json=_SUBS_RESPONSE, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts/usr_42/invoices",
            json=_INVOICES_RESPONSE, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts/usr_42/transactions",
            json=_TRANSACTIONS_RESPONSE, status=200,
        )

        md = rc.summary_to_markdown(rc.support_summary("usr_42"))

        self.assertIn("# Recurly: alice@example.com", md)
        self.assertIn("usr_42", md)
        self.assertIn("## Subscriptions", md)
        self.assertIn("Pro Monthly", md)
        self.assertIn("29.99", md)
        self.assertIn("## Latest invoice", md)
        self.assertIn("#1234", md)
        self.assertIn("paid", md)
        self.assertIn("## Recent transactions", md)
        self.assertIn("Visa", md)
        self.assertIn("Insufficient funds", md)

    def test_not_found_markdown(self):
        md = rc.summary_to_markdown({"found": False, "ref": "ghost_99"})
        self.assertIn("not found", md.lower())
        self.assertIn("ghost_99", md)

    def test_money_helper(self):
        self.assertEqual(rc._money(29.99, "USD"), "29.99 USD")
        self.assertEqual(rc._money(0, "USD"), "0.00 USD")
        self.assertEqual(rc._money(None, "USD"), "— USD")
        self.assertEqual(rc._money(100.5, ""), "100.50")


class RecurlyPickFields(unittest.TestCase):
    """api.pick works on pre-selected subscription/invoice/transaction dicts."""

    def test_pick_subscription_fields(self):
        picked = rc._pick_subscription(_SUBSCRIPTION_1)
        self.assertEqual(picked["state"], "active")
        self.assertEqual(picked["plan_code"], "pro_monthly")
        self.assertEqual(picked["unit_amount"], 29.99)
        self.assertNotIn("plan", picked)  # pre-selection: plan is flattened

    def test_pick_invoice_fields(self):
        picked = rc._pick_invoice(_INVOICE_1)
        self.assertEqual(picked["number"], "1234")
        self.assertEqual(picked["state"], "paid")
        self.assertEqual(picked["total"], 29.99)

    def test_pick_transaction_fields(self):
        picked = rc._pick_transaction(_TRANSACTION_1)
        self.assertEqual(picked["status"], "success")
        self.assertEqual(picked["card_type"], "Visa")
        self.assertEqual(picked["last_four"], "4242")
        self.assertNotIn("payment_method", picked)  # flattened


class RecurlyCLI(unittest.TestCase):
    """CLI main() drives the account subcommand end-to-end."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_RECURLY")
        os.environ["RC_CONN_RECURLY"] = "test_api_key_dummy"
        api.load_manifests()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_RECURLY", None)
        else:
            os.environ["RC_CONN_RECURLY"] = self._saved

    @responses_lib.activate
    def test_cli_account_subcommand_returns_zero(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/accounts/usr_42", json=_ACCOUNT, status=200)
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts/usr_42/subscriptions",
            json=_SUBS_RESPONSE, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts/usr_42/invoices",
            json=_INVOICES_RESPONSE, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts/usr_42/transactions",
            json=_TRANSACTIONS_RESPONSE, status=200,
        )

        rc_code = rc.main(["account", "usr_42"])
        self.assertEqual(rc_code, 0)


class RecurlyTokenHygiene(unittest.TestCase):
    """CI guard: no real Recurly API key prefix may land in the connector dir files.

    Scoped to the connector dir (manifest + source) only, NOT this test file — this file
    legitimately names the prefixes it hunts for so scanning itself would be a false positive.
    """

    # Recurly private API key prefix. Split to avoid triggering the guard on this source file.
    _TOKEN_PREFIXES = ("recurly-private" "-", "rc_" "priv_")

    def test_no_token_prefixes_in_recurly_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "recurly"
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
