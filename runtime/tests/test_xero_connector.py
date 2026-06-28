"""Tests for the Xero connector (manifest + script).

Fixture test: HTTP is mocked with ``responses`` (no live creds, no network). Payloads mirror Xero's
documented example objects, trimmed to support-relevant fields.

Covers:
- YAML manifest loads cleanly via lib.api's loader and maps every field.
- 1-based page-number pagination stitches ≥2 pages via ``_xero_pages``.
- ``Xero-tenant-id`` header is present on EVERY request (incl. subsequent pages).
- Bearer credential rides every request from the env-var injection.
- ``api.pick`` selects support fields from a Xero invoice body.
- CLI drives: ``tenants``, ``invoice``, ``contact``, ``invoices``.
- Token-prefix hygiene: no real Xero oauth bearer prefix leaks into connector files.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_xero_connector.py -q
"""

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
import lib.connectors.xero as xero  # noqa: E402

TENANT_ID = "a8f9a6b2-0000-0000-0000-000000000001"
BASE = "https://api.xero.com/api.xro/2.0"
CONNECTIONS_URL = "https://api.xero.com/connections"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# Shapes mirror https://developer.xero.com/documentation/api/accounting/invoices
# ---------------------------------------------------------------------------

_INVOICE_1 = {
    "InvoiceID": "f0d71f82-0000-0000-0000-000000000001",
    "InvoiceNumber": "INV-0001",
    "Type": "ACCREC",
    "Status": "OUTSTANDING",
    "Contact": {"ContactID": "c1-0000", "Name": "Acme Corp", "EmailAddress": "billing@acme.example"},
    "Total": 1500.00,
    "AmountDue": 1500.00,
    "AmountPaid": 0.0,
    "CurrencyCode": "USD",
    "DueDate": "/Date(1735689600000+0000)/",
    "UpdatedDateUTC": "/Date(1735000000000+0000)/",
    "LineItems": [
        {"Description": "Widget A", "Quantity": 10.0, "UnitAmount": 150.0}
    ],
}

_INVOICE_2 = {
    "InvoiceID": "f0d71f82-0000-0000-0000-000000000002",
    "InvoiceNumber": "INV-0002",
    "Type": "ACCREC",
    "Status": "OUTSTANDING",
    "Contact": {"ContactID": "c1-0000", "Name": "Acme Corp", "EmailAddress": "billing@acme.example"},
    "Total": 800.00,
    "AmountDue": 800.00,
    "AmountPaid": 0.0,
    "CurrencyCode": "USD",
    "DueDate": "/Date(1736899200000+0000)/",
    "UpdatedDateUTC": "/Date(1735100000000+0000)/",
    "LineItems": [],
}

_CONTACT_1 = {
    "ContactID": "c1-0000",
    "Name": "Acme Corp",
    "EmailAddress": "billing@acme.example",
    "IsCustomer": True,
    "IsSupplier": False,
}

# Xero wraps list responses in a keyed envelope
_INVOICES_PAGE_1 = {"Invoices": [_INVOICE_1] * 100}   # full page → triggers page 2
_INVOICES_PAGE_2 = {"Invoices": [_INVOICE_2] * 3}      # partial page → stop
_INVOICES_SINGLE = {"Invoices": [_INVOICE_1]}
_CONTACTS_BODY = {"Contacts": [_CONTACT_1]}
_INVOICE_BODY = {"Invoices": [_INVOICE_1]}

_TENANTS_BODY = [
    {
        "tenantId": TENANT_ID,
        "tenantName": "Acme Corp Accounting",
        "tenantType": "ORGANISATION",
    }
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_env_token(token: str = "xero_test_bearer_token_mock"):
    """Set RC_CONN_XERO in the environment for the duration of a test."""
    return _EnvPatch("RC_CONN_XERO", token)


class _EnvPatch:
    def __init__(self, key: str, value: str):
        self._key = key
        self._value = value
        self._old = None

    def __enter__(self):
        self._old = os.environ.get(self._key)
        os.environ[self._key] = self._value
        return self

    def __exit__(self, *_):
        if self._old is None:
            os.environ.pop(self._key, None)
        else:
            os.environ[self._key] = self._old


# ---------------------------------------------------------------------------
# Test: manifest loads correctly
# ---------------------------------------------------------------------------


class TestXeroManifestLoad(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loads_via_yaml_loader(self):
        manifests = api.load_manifests()
        self.assertIn("xero", manifests)

    def test_manifest_fields(self):
        api.load_manifests()
        m = api.MANIFESTS["xero"]
        self.assertEqual(m.key, "xero")
        self.assertEqual(m.base_url, "https://api.xero.com/api.xro/2.0")
        self.assertEqual(m.auth.strategy, "bearer")
        # 1-based page-number pagination via the generic `page` style.
        self.assertEqual(m.pagination.style, "page")
        self.assertEqual(m.pagination.page_param, "page")
        self.assertEqual(m.pagination.page_start, 1)
        self.assertEqual(m.pagination.page_size, 100)
        self.assertEqual(m.rate_limit_remaining_header, "X-MinLimit-Remaining")

    def test_manifest_matches_connector_registration(self):
        """The connector's explicit register() should win over YAML loader."""
        # The connector module was imported at the top of this file, so MANIFEST is registered.
        # Re-loading should not overwrite it.
        m_before = api.MANIFESTS.get("xero")
        api.load_manifests()
        m_after = api.MANIFESTS.get("xero")
        # Both should point to the same key and base_url.
        self.assertIsNotNone(m_after)
        self.assertEqual(m_after.key, "xero")
        if m_before is not None:
            self.assertEqual(m_before.base_url, m_after.base_url)


# ---------------------------------------------------------------------------
# Test: pagination stitches ≥2 pages; Xero-tenant-id header rides every request
# ---------------------------------------------------------------------------


class TestXeroPagination(unittest.TestCase):
    def setUp(self):
        self._env = _mock_env_token()
        self._env.__enter__()

    def tearDown(self):
        self._env.__exit__(None, None, None)

    @responses_lib.activate
    def test_1based_page_pagination_stitches_two_pages(self):
        # Page 1: 100 items → triggers page 2. Page 2: 3 items → stop.
        responses_lib.add(
            responses_lib.GET,
            f"{BASE}/Invoices",
            json=_INVOICES_PAGE_1,
            status=200,
            headers={"X-MinLimit-Remaining": "59"},
        )
        responses_lib.add(
            responses_lib.GET,
            f"{BASE}/Invoices",
            json=_INVOICES_PAGE_2,
            status=200,
            headers={"X-MinLimit-Remaining": "58"},
        )

        items = xero._xero_pages("Invoices", tenant_id=TENANT_ID, items_key="Invoices")
        self.assertEqual(len(items), 103)  # 100 + 3

    @responses_lib.activate
    def test_tenant_id_header_on_every_page(self):
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Invoices",
            json=_INVOICES_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Invoices",
            json=_INVOICES_PAGE_2, status=200,
        )

        xero._xero_pages("Invoices", tenant_id=TENANT_ID, items_key="Invoices")

        for call in responses_lib.calls:
            self.assertEqual(
                call.request.headers.get("Xero-tenant-id"),
                TENANT_ID,
                f"Xero-tenant-id missing on {call.request.url}",
            )

    @responses_lib.activate
    def test_bearer_token_on_every_page(self):
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Invoices",
            json=_INVOICES_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Invoices",
            json=_INVOICES_PAGE_2, status=200,
        )

        xero._xero_pages("Invoices", tenant_id=TENANT_ID, items_key="Invoices")

        for call in responses_lib.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(
                auth.startswith("Bearer "),
                f"Bearer missing on {call.request.url}: got {auth!r}",
            )

    @responses_lib.activate
    def test_single_page_stops_when_below_100(self):
        # Only 3 items on the first page → no second request made.
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Invoices",
            json={"Invoices": [_INVOICE_1] * 3}, status=200,
        )
        items = xero._xero_pages("Invoices", tenant_id=TENANT_ID, items_key="Invoices")
        self.assertEqual(len(items), 3)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_page_number_query_params_are_correct(self):
        """Verify page=1, page=2 are sent (1-based), not page=0 / offset style."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Invoices",
            json=_INVOICES_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Invoices",
            json=_INVOICES_PAGE_2, status=200,
        )

        xero._xero_pages("Invoices", tenant_id=TENANT_ID, items_key="Invoices")

        from urllib.parse import parse_qs, urlparse
        page_nums = []
        for call in responses_lib.calls:
            qs = parse_qs(urlparse(call.request.url).query)
            page_nums.append(int(qs["page"][0]))
        self.assertEqual(page_nums, [1, 2])


# ---------------------------------------------------------------------------
# Test: api.pick selects support fields
# ---------------------------------------------------------------------------


class TestXeroPick(unittest.TestCase):
    def test_pick_invoice_fields(self):
        result = api.pick(
            _INVOICE_1,
            "InvoiceNumber,Status,Total,AmountDue,CurrencyCode,Contact.Name,Contact.EmailAddress",
        )
        self.assertEqual(result["InvoiceNumber"], "INV-0001")
        self.assertEqual(result["Status"], "OUTSTANDING")
        self.assertEqual(result["Total"], 1500.00)
        self.assertEqual(result["Contact.Name"], "Acme Corp")
        self.assertEqual(result["Contact.EmailAddress"], "billing@acme.example")

    def test_pick_missing_path_absent(self):
        result = api.pick(_INVOICE_1, "InvoiceNumber,DoesNotExist.Nope")
        self.assertIn("InvoiceNumber", result)
        self.assertNotIn("DoesNotExist.Nope", result)


# ---------------------------------------------------------------------------
# Test: get_invoice, find_contact, contact_summary
# ---------------------------------------------------------------------------


class TestXeroHighLevel(unittest.TestCase):
    def setUp(self):
        self._env = _mock_env_token()
        self._env.__enter__()

    def tearDown(self):
        self._env.__exit__(None, None, None)

    @responses_lib.activate
    def test_get_invoice_by_number(self):
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Invoices/INV-0001",
            json=_INVOICE_BODY, status=200,
        )
        inv = xero.get_invoice(TENANT_ID, "INV-0001")
        self.assertIsNotNone(inv)
        self.assertEqual(inv["InvoiceNumber"], "INV-0001")
        # Xero-tenant-id must be on the request.
        self.assertEqual(responses_lib.calls[0].request.headers.get("Xero-tenant-id"), TENANT_ID)

    @responses_lib.activate
    def test_get_invoice_not_found_returns_none(self):
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Invoices/INV-9999",
            json={"Invoices": []}, status=404,
        )
        inv = xero.get_invoice(TENANT_ID, "INV-9999")
        self.assertIsNone(inv)

    @responses_lib.activate
    def test_find_contact_by_name(self):
        # First attempt (direct lookup) returns 400 (name is not a UUID); fallback to search.
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Contacts/Acme Corp",
            json={"ErrorNumber": 400, "Type": "ValidationException"}, status=400,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Contacts",
            json=_CONTACTS_BODY, status=200,
        )
        c = xero.find_contact(TENANT_ID, "Acme Corp")
        self.assertIsNotNone(c)
        self.assertEqual(c["Name"], "Acme Corp")

    @responses_lib.activate
    def test_contact_summary_found(self):
        # Direct UUID lookup for contact
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Contacts/c1-0000",
            json=_CONTACTS_BODY, status=200,
        )
        # Outstanding invoices page (single partial page)
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Invoices",
            json=_INVOICES_SINGLE, status=200,
        )
        s = xero.contact_summary(TENANT_ID, "c1-0000")
        self.assertTrue(s["found"])
        self.assertEqual(s["contact"]["Name"], "Acme Corp")
        self.assertEqual(len(s["outstanding_invoices"]), 1)

    @responses_lib.activate
    def test_contact_not_found_by_uuid(self):
        # 404 on a direct UUID lookup → definitively absent, no search fallback.
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Contacts/c0-0000-0000-0000-notfound",
            json={"Type": "ValidationException", "Detail": "ContactID: not found"}, status=404,
        )
        s = xero.contact_summary(TENANT_ID, "c0-0000-0000-0000-notfound")
        self.assertFalse(s["found"])
        self.assertEqual(s["ref"], "c0-0000-0000-0000-notfound")
        # Only one HTTP call: no search fallback on 404.
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_contact_not_found_by_name_search(self):
        # 400 on a name-as-id lookup → falls back to searchTerm, returns empty → None.
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Contacts/nobody",
            json={"Type": "ValidationException", "Detail": "ContactID invalid"}, status=400,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Contacts",
            json={"Contacts": []}, status=200,
        )
        s = xero.contact_summary(TENANT_ID, "nobody")
        self.assertFalse(s["found"])
        self.assertEqual(s["ref"], "nobody")
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_list_tenants(self):
        responses_lib.add(
            responses_lib.GET, CONNECTIONS_URL,
            json=_TENANTS_BODY, status=200,
        )
        tenants = xero.list_tenants()
        self.assertEqual(len(tenants), 1)
        self.assertEqual(tenants[0]["tenantId"], TENANT_ID)
        # Bearer token on the connections request
        auth = responses_lib.calls[0].request.headers.get("Authorization", "")
        self.assertTrue(auth.startswith("Bearer "))


# ---------------------------------------------------------------------------
# Test: CLI drives end-to-end
# ---------------------------------------------------------------------------


class TestXeroCLI(unittest.TestCase):
    def setUp(self):
        self._env = _mock_env_token()
        self._env.__enter__()

    def tearDown(self):
        self._env.__exit__(None, None, None)

    @responses_lib.activate
    def test_cli_tenants(self):
        responses_lib.add(
            responses_lib.GET, CONNECTIONS_URL,
            json=_TENANTS_BODY, status=200,
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = xero.main(["tenants"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Acme Corp Accounting", out)
        self.assertIn(TENANT_ID, out)

    @responses_lib.activate
    def test_cli_invoice(self):
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Invoices/INV-0001",
            json=_INVOICE_BODY, status=200,
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = xero.main(["invoice", "--tenant-id", TENANT_ID, "INV-0001"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["InvoiceNumber"], "INV-0001")

    @responses_lib.activate
    def test_cli_invoices(self):
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Invoices",
            json=_INVOICES_SINGLE, status=200,
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = xero.main(["invoices", "--tenant-id", TENANT_ID, "--status", "OUTSTANDING"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertIsInstance(out, list)
        self.assertEqual(out[0]["InvoiceNumber"], "INV-0001")

    @responses_lib.activate
    def test_cli_contact_not_found(self):
        # 400 on direct lookup → search fallback → empty → not found rendered in markdown.
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Contacts/nobody",
            json={"Type": "ValidationException", "Detail": "ContactID invalid"}, status=400,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Contacts",
            json={"Contacts": []}, status=200,
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = xero.main(["contact", "--tenant-id", TENANT_ID, "nobody"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("not found", out)


# ---------------------------------------------------------------------------
# Test: token-prefix hygiene (no real Xero OAuth bearer prefix in committed files)
# ---------------------------------------------------------------------------


class TestXeroTokenHygiene(unittest.TestCase):
    """CI guard: no real Xero OAuth2 bearer token prefix may land in the connector dir.

    Xero access tokens issued by identity.xero.com begin with "eyJ" (JWT base64url).
    We split the prefix across string literals here so this guard doesn't flag itself.
    """

    # Xero OAuth access tokens are JWTs starting with base64url-encoded {"alg":"..."}
    # header: always "eyJ" + "0" or similar. We check for the JWT prefix.
    _TOKEN_PREFIXES = ("eyJ" "0",)

    def test_no_token_prefixes_in_xero_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "xero"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
