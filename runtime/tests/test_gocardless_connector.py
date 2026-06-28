"""Fixture test for the GoCardless script connector.

Force-code trigger (d): GoCardless list responses embed items under a resource-type key that
varies per endpoint ("payments", "mandates", "customers", etc.) while the pagination cursor lives
at ``meta.cursors.after``. The connector handles this variable envelope dynamically.

No live creds, no network: HTTP is mocked with ``responses``. Bodies mirror GoCardless's documented
example payloads (trimmed to support-relevant fields). Two pages of payments exercise the real
cursor pagination loop end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_gocardless_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
import lib.connectors.gocardless as gc  # noqa: E402 — registers the manifest

BASE = "https://api.gocardless.com"
PAYMENTS_URL = f"{BASE}/payments"
MANDATES_URL = f"{BASE}/mandates"
CUSTOMERS_URL = f"{BASE}/customers"
PAYMENT_SINGLE_URL = f"{BASE}/payments/PM123"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# ---------------------------------------------------------------------------

_PAYMENT_1 = {
    "id": "PM123",
    "status": "paid_out",
    "amount": 2000,
    "currency": "GBP",
    "description": "Monthly invoice",
    "created_at": "2024-01-15T10:00:00.000Z",
    "charge_date": "2024-01-17",
    "failure_reason": None,
    "failure_reason_description": None,
    "can_retry": False,
    "links": {"mandate": "MD456", "subscription": "SB789", "customer": "CU001"},
}
_PAYMENT_2 = {
    "id": "PM124",
    "status": "failed",
    "amount": 1500,
    "currency": "GBP",
    "description": "Annual plan",
    "created_at": "2024-01-20T11:00:00.000Z",
    "charge_date": "2024-01-22",
    "failure_reason": "refer_to_payer",
    "failure_reason_description": "Bank account closed or transferred",
    "can_retry": True,
    "links": {"mandate": "MD456", "subscription": None, "customer": "CU001"},
}

# Page 1: one payment + cursor pointing to page 2.
_PAGE_1_BODY = {
    "payments": [_PAYMENT_1],
    "meta": {
        "cursors": {
            "before": None,
            "after": "cursor_opaque_abc",
        }
    },
}
# Page 2: one payment + null cursor → last page.
_PAGE_2_BODY = {
    "payments": [_PAYMENT_2],
    "meta": {
        "cursors": {
            "before": "cursor_opaque_abc",
            "after": None,
        }
    },
}

_MANDATE_BODY = {
    "mandates": [
        {
            "id": "MD456",
            "status": "active",
            "scheme": "bacs",
            "created_at": "2023-12-01T09:00:00.000Z",
            "next_possible_charge_date": "2024-02-01",
            "reference": "REF001",
            "payments_require_approval": False,
            "links": {"customer": "CU001", "customer_bank_account": "BA111"},
        }
    ],
    "meta": {"cursors": {"before": None, "after": None}},
}

_CUSTOMER_BODY = {
    "customers": [
        {
            "id": "CU001",
            "email": "alice@example.com",
            "given_name": "Alice",
            "family_name": "Smith",
            "company_name": None,
            "phone_number": "+44 7700 900077",
            "created_at": "2023-11-15T08:00:00.000Z",
            "language": "en-GB",
            "metadata": {"internal_id": "USR999"},
        }
    ],
    "meta": {"cursors": {"before": None, "after": None}},
}

_SINGLE_PAYMENT_BODY = {
    "payments": {
        "id": "PM123",
        "status": "paid_out",
        "amount": 2000,
        "currency": "GBP",
        "description": "Monthly invoice",
        "created_at": "2024-01-15T10:00:00.000Z",
        "charge_date": "2024-01-17",
        "failure_reason": None,
        "failure_reason_description": None,
        "can_retry": False,
        "links": {"mandate": "MD456", "subscription": "SB789", "customer": "CU001"},
    }
}


class GoCardlessManifest(unittest.TestCase):
    """Verify the YAML manifest loads correctly and maps all fields via lib.api's loader."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GOCARDLESS")
        os.environ["RC_CONN_GOCARDLESS"] = "tok_gc_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GOCARDLESS", None)
        else:
            os.environ["RC_CONN_GOCARDLESS"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        manifests = api.load_manifests()
        self.assertIn("gocardless", manifests)
        m = manifests["gocardless"]
        self.assertEqual(m.key, "gocardless")
        self.assertEqual(m.base_url, "https://api.gocardless.com")
        self.assertEqual(m.auth.strategy, "bearer")
        self.assertEqual(m.pagination.style, "cursor")
        self.assertEqual(m.pagination.cursor_field, "meta.cursors.after")
        self.assertEqual(m.pagination.cursor_param, "after")
        self.assertEqual(m.pagination.has_more_field, "")
        self.assertEqual(m.pagination.items_field, "")
        self.assertEqual(m.pagination.page_size, 200)
        self.assertEqual(m.rate_limit_remaining_header, "ratelimit-remaining")
        self.assertEqual(m.default_headers["GoCardless-Version"], "2015-07-06")

    def test_connector_registers_manifest(self):
        """Importing the connector module registers the manifest under its key."""
        # gc is already imported at module level; verify the registration stuck.
        api.load_manifests()
        self.assertIn("gocardless", api.MANIFESTS)
        self.assertEqual(api.MANIFESTS["gocardless"].base_url, "https://api.gocardless.com")


class GoCardlessPagination(unittest.TestCase):
    """Cursor pagination stitches ≥2 pages and credential rides every request."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GOCARDLESS")
        os.environ["RC_CONN_GOCARDLESS"] = "tok_gc_test"
        # Re-register so the connector manifest is available after MANIFESTS.clear().
        api.register(gc.MANIFEST)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GOCARDLESS", None)
        else:
            os.environ["RC_CONN_GOCARDLESS"] = self._saved

    @responses_lib.activate
    def test_cursor_pagination_stitches_two_pages(self):
        # Page 1: has meta.cursors.after → page 2 follows.
        responses_lib.add(responses_lib.GET, PAYMENTS_URL, json=_PAGE_1_BODY, status=200,
                          headers={"ratelimit-remaining": "999"})
        # Page 2: meta.cursors.after is null → loop stops.
        responses_lib.add(responses_lib.GET, PAYMENTS_URL, json=_PAGE_2_BODY, status=200,
                          headers={"ratelimit-remaining": "998"})

        result = gc.list_resource("/payments", query={"customer_id": "CU001"})

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["id"], "PM123")
        self.assertEqual(result["items"][1]["id"], "PM124")

    @responses_lib.activate
    def test_bearer_token_on_every_request(self):
        """Bearer credential must ride both page-1 and page-2 requests."""
        responses_lib.add(responses_lib.GET, PAYMENTS_URL, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, PAYMENTS_URL, json=_PAGE_2_BODY, status=200)

        gc.list_resource("/payments")

        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], "Bearer tok_gc_test")

    @responses_lib.activate
    def test_gocardless_version_header_on_every_request(self):
        """GoCardless-Version default header must be present on all requests."""
        responses_lib.add(responses_lib.GET, PAYMENTS_URL, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, PAYMENTS_URL, json=_PAGE_2_BODY, status=200)

        gc.list_resource("/payments")

        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["GoCardless-Version"], "2015-07-06")

    @responses_lib.activate
    def test_cursor_param_sent_on_second_page(self):
        """The ``after`` query param carries the cursor value from page 1 on the page 2 request."""
        responses_lib.add(responses_lib.GET, PAYMENTS_URL, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, PAYMENTS_URL, json=_PAGE_2_BODY, status=200)

        gc.list_resource("/payments")

        # Page 1: no 'after' param.
        self.assertNotIn("after", responses_lib.calls[0].request.url)
        # Page 2: 'after=cursor_opaque_abc' from page 1's meta.cursors.after.
        self.assertIn("after=cursor_opaque_abc", responses_lib.calls[1].request.url)

    @responses_lib.activate
    def test_single_page_stops_when_cursor_null(self):
        """If meta.cursors.after is null on the first page, only one request is made."""
        single_page = {
            "payments": [_PAYMENT_1],
            "meta": {"cursors": {"before": None, "after": None}},
        }
        responses_lib.add(responses_lib.GET, PAYMENTS_URL, json=single_page, status=200)

        result = gc.list_resource("/payments")

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_variable_envelope_extraction_mandates(self):
        """Items are correctly extracted from the 'mandates' envelope key."""
        responses_lib.add(responses_lib.GET, MANDATES_URL, json=_MANDATE_BODY, status=200)

        result = gc.list_resource("/mandates")

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], "MD456")

    @responses_lib.activate
    def test_variable_envelope_extraction_customers(self):
        """Items are correctly extracted from the 'customers' envelope key."""
        responses_lib.add(responses_lib.GET, CUSTOMERS_URL, json=_CUSTOMER_BODY, status=200)

        result = gc.list_resource("/customers")

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["email"], "alice@example.com")


class GoCardlessPickFields(unittest.TestCase):
    """api.pick selects support-relevant fields correctly."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_GOCARDLESS")
        os.environ["RC_CONN_GOCARDLESS"] = "tok_gc_test"
        api.register(gc.MANIFEST)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GOCARDLESS", None)
        else:
            os.environ["RC_CONN_GOCARDLESS"] = self._saved

    def test_pick_payment_fields(self):
        picked = api.pick(_PAYMENT_2, gc._PICK_FIELDS["payments"])
        self.assertEqual(picked["id"], "PM124")
        self.assertEqual(picked["status"], "failed")
        self.assertEqual(picked["failure_reason"], "refer_to_payer")
        self.assertEqual(picked["can_retry"], True)
        self.assertIn("links.customer", picked)

    def test_pick_mandate_fields(self):
        mandate = _MANDATE_BODY["mandates"][0]
        picked = api.pick(mandate, gc._PICK_FIELDS["mandates"])
        self.assertEqual(picked["id"], "MD456")
        self.assertEqual(picked["status"], "active")
        self.assertEqual(picked["scheme"], "bacs")

    def test_pick_customer_fields(self):
        customer = _CUSTOMER_BODY["customers"][0]
        picked = api.pick(customer, gc._PICK_FIELDS["customers"])
        self.assertEqual(picked["email"], "alice@example.com")
        self.assertEqual(picked["given_name"], "Alice")


class GoCardlessCLI(unittest.TestCase):
    """CLI (main()) drives the connector end-to-end."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GOCARDLESS")
        os.environ["RC_CONN_GOCARDLESS"] = "tok_gc_test"
        api.register(gc.MANIFEST)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GOCARDLESS", None)
        else:
            os.environ["RC_CONN_GOCARDLESS"] = self._saved

    @responses_lib.activate
    def test_cli_list_payments_paginates_and_picks(self, capsys=None):
        responses_lib.add(responses_lib.GET, PAYMENTS_URL, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, PAYMENTS_URL, json=_PAGE_2_BODY, status=200)

        rc = gc.main(["list", "payments", "--query", "customer_id=CU001"])
        self.assertEqual(rc, 0)
        # Both pages fetched.
        self.assertEqual(len(responses_lib.calls), 2)
        # Both requests carry the bearer token.
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], "Bearer tok_gc_test")

    @responses_lib.activate
    def test_cli_get_payment_single_resource(self):
        responses_lib.add(responses_lib.GET, PAYMENT_SINGLE_URL, json=_SINGLE_PAYMENT_BODY,
                          status=200)

        rc = gc.main(["get", "payment", "PM123"])
        self.assertEqual(rc, 0)
        # Exactly one request to the single-resource URL.
        self.assertEqual(len(responses_lib.calls), 1)
        self.assertIn("/payments/PM123", responses_lib.calls[0].request.url)

    @responses_lib.activate
    def test_cli_list_no_pick_returns_full_objects(self):
        responses_lib.add(responses_lib.GET, PAYMENTS_URL, json=_PAGE_2_BODY, status=200)

        # --no-pick should not raise and should still return 0.
        rc = gc.main(["list", "payments", "--no-pick"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_lib_api_cli_drives_gocardless_single_page(self):
        """Generic ``python -m lib.api get gocardless`` works for single-page GETs."""
        responses_lib.add(responses_lib.GET, PAYMENTS_URL, json=_PAGE_2_BODY, status=200)

        api.load_manifests()
        rc = api._main(["get", "gocardless", "/payments",
                        "--query", "customer_id=CU001"])
        self.assertEqual(rc, 0)
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"],
                         "Bearer tok_gc_test")


class GoCardlessCassetteHygiene(unittest.TestCase):
    """CI guard: no real GoCardless access-token prefix may land in the committed connector files.

    Scopes to the connector dir (manifest + __init__.py + __main__.py), NOT this test file — the
    test legitimately names the prefixes it hunts for, so scanning itself would be a false positive.
    """

    # GoCardless live/sandbox access token prefixes: "live_" + "sandbox_" (concatenated to avoid
    # triggering the guard on this very literal while still catching real leaks).
    _TOKEN_PREFIXES = ("live" "_", "sandbox" "_")

    def test_no_token_prefixes_in_gocardless_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "gocardless"
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
