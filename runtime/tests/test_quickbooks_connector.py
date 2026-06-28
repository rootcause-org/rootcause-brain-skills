"""Fixture test for the QuickBooks Online connector (lib.connectors.quickbooks).

Force-code triggers that required a script:
(a) field pre-selection, (d) pagination embedded in QB SQL, (e) QB SQL query DSL.

No live creds, no network. HTTP is mocked with ``responses``. The fixture bodies mirror
Intuit's documented QB Online API response shapes, trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_quickbooks_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
import lib.connectors.quickbooks as qb  # noqa: E402

REALM_ID = "123456789"
BASE = f"https://quickbooks.api.intuit.com/v3/company/{REALM_ID}"
QUERY_URL = f"{BASE}/query"

# ---------------------------------------------------------------------------
# Documented example payloads (from Intuit QB Online API docs)
# ---------------------------------------------------------------------------

# Two-page Customer query response. QB returns items under QueryResponse.<Entity>.
# Page 1: 2 customers (matching MAXRESULTS 2 to force a page 2 fetch).
_CUSTOMER_PAGE_1 = {
    "QueryResponse": {
        "Customer": [
            {
                "Id": "1",
                "DisplayName": "Amy Lauterbach",
                "PrimaryEmailAddr": {"Address": "Amy@Lauterbach.com"},
                "PrimaryPhone": {"FreeFormNumber": "555-1234"},
                "CompanyName": "Lauterbach Inc",
                "Balance": 239.00,
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-10T12:00:00"},
            },
            {
                "Id": "2",
                "DisplayName": "Bill Lucchini",
                "PrimaryEmailAddr": {"Address": "Bill@Lucchini.com"},
                "Balance": 0.00,
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-09T10:00:00"},
            },
        ],
        "startPosition": 1,
        "maxResults": 2,
    },
    "time": "2024-01-10T15:00:00.000-08:00",
}

# Page 2: 1 customer (fewer than MAXRESULTS → exhausted).
_CUSTOMER_PAGE_2 = {
    "QueryResponse": {
        "Customer": [
            {
                "Id": "3",
                "DisplayName": "Cool Cars",
                "PrimaryEmailAddr": {"Address": "cool@cars.com"},
                "Balance": 1500.00,
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-08T09:00:00"},
            },
        ],
        "startPosition": 3,
        "maxResults": 1,
    },
    "time": "2024-01-10T15:00:00.000-08:00",
}

# Single customer direct-read response (GET /customer/{id}).
_CUSTOMER_SINGLE = {
    "Customer": {
        "Id": "42",
        "DisplayName": "Geeta Kalapatapu",
        "PrimaryEmailAddr": {"Address": "Geeta@Kalapatapu.com"},
        "Balance": 629.10,
        "Active": True,
        "CompanyName": "Kalapatapu Exports",
    },
    "time": "2024-01-10T15:00:00.000-08:00",
}

# Invoice list response (one invoice).
_INVOICE_LIST = {
    "QueryResponse": {
        "Invoice": [
            {
                "Id": "1001",
                "DocNumber": "1037",
                "TxnDate": "2024-01-05",
                "DueDate": "2024-02-04",
                "TotalAmt": 582.50,
                "Balance": 582.50,
                "CustomerRef": {"value": "42", "name": "Geeta Kalapatapu"},
                "EmailStatus": "NotSet",
                "PrintStatus": "NotSet",
                "MetaData": {"LastUpdatedTime": "2024-01-05T08:00:00"},
            },
        ],
        "startPosition": 1,
        "maxResults": 1,
    },
    "time": "2024-01-10T15:00:00.000-08:00",
}

# Single invoice direct-read (GET /invoice/{id}).
_INVOICE_SINGLE = {
    "Invoice": {
        "Id": "1001",
        "DocNumber": "1037",
        "TxnDate": "2024-01-05",
        "DueDate": "2024-02-04",
        "TotalAmt": 582.50,
        "Balance": 582.50,
        "CustomerRef": {"value": "42", "name": "Geeta Kalapatapu"},
        "EmailStatus": "NotSet",
    },
    "time": "2024-01-10T15:00:00.000-08:00",
}

# Company info.
_COMPANY_INFO = {
    "CompanyInfo": {
        "CompanyName": "Sandbox Company_US_1",
        "LegalName": "Sandbox Company_US_1",
        "CompanyAddr": {
            "Id": "1",
            "Line1": "2500 Garcia Ave",
            "City": "Mountain View",
            "CountrySubDivisionCode": "CA",
            "PostalCode": "94043",
            "Country": "US",
        },
        "FiscalYearStartMonth": "January",
        "Country": "US",
        "Email": {"Address": "donotreply@intuit.com"},
        "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00"},
    },
    "time": "2024-01-10T15:00:00.000-08:00",
}


def _env(monkeypatch_dict: dict[str, str], env: dict[str, str]) -> None:
    """Temporarily update os.environ within a test — restored in tearDown."""
    monkeypatch_dict.update(env)


class _Base(unittest.TestCase):
    """Common setUp: inject a fake token + realmId, clear manifest registry."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._orig_env: dict[str, str | None] = {}
        for key in ("RC_CONN_QUICKBOOKS", "RC_CONN_QUICKBOOKS_REALM_ID"):
            self._orig_env[key] = os.environ.get(key)
        os.environ["RC_CONN_QUICKBOOKS"] = "Bearer_test_qb_tok"
        os.environ["RC_CONN_QUICKBOOKS_REALM_ID"] = REALM_ID

    def tearDown(self):
        for key, val in self._orig_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


class QuickbooksManifest(_Base):
    """The YAML manifest loads correctly and maps every lib.api field."""

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("quickbooks", m)
        qbm = m["quickbooks"]
        # base_url from the YAML manifest
        self.assertIn("quickbooks.api.intuit.com", qbm.base_url)
        self.assertEqual(qbm.auth.strategy, "bearer")
        self.assertEqual(qbm.pagination.style, "none")
        self.assertEqual(qbm.rate_limit_remaining_header, "")
        self.assertEqual(qbm.default_headers.get("Accept"), "application/json")

    def test_connector_register_wins_over_yaml(self):
        """The connector's api.register() call must take precedence over YAML loading."""
        import lib.connectors.quickbooks  # noqa: F401 — side-effect: registers manifest
        api.load_manifests()  # should not overwrite the registered manifest
        # The registered manifest's base_url is the host-only form (connector sets path per-call).
        self.assertEqual(api.MANIFESTS["quickbooks"].key, "quickbooks")
        self.assertEqual(api.MANIFESTS["quickbooks"].auth.strategy, "bearer")


class QuickbooksCustomerQuery(_Base):
    """Customer lookup — two-page SQL pagination stitched by the connector."""

    @responses_lib.activate
    def test_customer_query_stitches_two_pages(self):
        # The connector calls STARTPOSITION 1 MAXRESULTS 100 then STARTPOSITION 101... etc.
        # We mock QUERY_URL twice: page 1 has 2 items (simulate page_size=2), page 2 has 1 item.
        # Override page_size to 2 so we can trigger a second page.
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_CUSTOMER_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_CUSTOMER_PAGE_2, status=200)

        # Call with a tiny page_size to force two pages.
        results = qb._qb_query(
            "SELECT Id, DisplayName FROM Customer WHERE DisplayName LIKE '%a%'",
            "Customer",
            realm=REALM_ID,
            page_size=2,
            max_records=50,
        )

        self.assertEqual(len(results), 3)  # 2 from page 1 + 1 from page 2
        self.assertEqual(results[0]["Id"], "1")
        self.assertEqual(results[2]["Id"], "3")

        # Bearer credential must ride on BOTH requests.
        for call in responses_lib.calls:
            self.assertIn("Bearer", call.request.headers.get("Authorization", ""))
            self.assertIn("Bearer_test_qb_tok", call.request.headers.get("Authorization", ""))

        # Accept: application/json must be present (QB serves XML by default).
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers.get("Accept"), "application/json")

    @responses_lib.activate
    def test_customer_query_stops_when_fewer_than_page_size(self):
        """Connector stops paging when the returned batch is smaller than page_size."""
        # Page 1 returns 1 item with page_size=2 → exhausted, no page 2.
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_CUSTOMER_PAGE_2, status=200)

        results = qb._qb_query(
            "SELECT Id FROM Customer",
            "Customer",
            realm=REALM_ID,
            page_size=2,
            max_records=50,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(len(responses_lib.calls), 1)  # only one HTTP call

    @responses_lib.activate
    def test_customer_query_by_display_name(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_CUSTOMER_PAGE_1, status=200)
        results = qb.query_customer("Lauterbach", realm=REALM_ID)
        self.assertGreater(len(results), 0)
        # SQL must embed the LIKE clause (checked via the query param sent to the mock).
        req_url = responses_lib.calls[0].request.url
        self.assertIn("LIKE", req_url)
        self.assertIn("Lauterbach", req_url)

    @responses_lib.activate
    def test_customer_direct_read_by_id(self):
        """customer_id triggers the direct GET /customer/{id} endpoint (no SQL)."""
        responses_lib.add(
            responses_lib.GET,
            f"{BASE}/customer/42",
            json=_CUSTOMER_SINGLE,
            status=200,
        )
        results = qb.query_customer(customer_id="42", realm=REALM_ID)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["Id"], "42")
        # Must NOT have called the query endpoint.
        for call in responses_lib.calls:
            self.assertNotIn("/query", call.request.url)


class QuickbooksInvoiceQuery(_Base):
    """Invoice lookup — direct read and filtered query."""

    @responses_lib.activate
    def test_invoice_direct_read(self):
        responses_lib.add(
            responses_lib.GET,
            f"{BASE}/invoice/1001",
            json=_INVOICE_SINGLE,
            status=200,
        )
        invs = qb.query_invoices(invoice_id="1001", realm=REALM_ID)
        self.assertEqual(len(invs), 1)
        self.assertEqual(invs[0]["Id"], "1001")
        # Only one call, no pagination.
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_invoice_list_by_customer(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_INVOICE_LIST, status=200)
        invs = qb.query_invoices(customer_id="42", realm=REALM_ID)
        self.assertEqual(len(invs), 1)
        self.assertEqual(invs[0]["DocNumber"], "1037")
        # SQL must filter by CustomerRef.
        req_url = responses_lib.calls[0].request.url
        self.assertIn("CustomerRef", req_url)


class QuickbooksCompanyInfo(_Base):
    """Company info read."""

    @responses_lib.activate
    def test_company_info(self):
        responses_lib.add(
            responses_lib.GET,
            f"{BASE}/companyinfo/{REALM_ID}",
            json=_COMPANY_INFO,
            status=200,
        )
        info = qb.query_company_info(realm=REALM_ID)
        self.assertIsNotNone(info)
        self.assertEqual(info["CompanyName"], "Sandbox Company_US_1")
        # Bearer credential present.
        self.assertIn("Bearer_test_qb_tok", responses_lib.calls[0].request.headers.get("Authorization", ""))


class QuickbooksMarkdownRendering(_Base):
    """Markdown rendering helpers produce correct output."""

    def test_customers_to_markdown(self):
        custs = _CUSTOMER_PAGE_1["QueryResponse"]["Customer"]
        md = qb.customers_to_markdown(custs)
        self.assertIn("Amy Lauterbach", md)
        self.assertIn("Amy@Lauterbach.com", md)
        self.assertIn("239.00", md)

    def test_empty_customers(self):
        md = qb.customers_to_markdown([])
        self.assertIn("No customers found", md)

    def test_invoices_to_markdown(self):
        invs = _INVOICE_LIST["QueryResponse"]["Invoice"]
        md = qb.invoices_to_markdown(invs)
        self.assertIn("1037", md)
        self.assertIn("582.50", md)
        self.assertIn("Geeta Kalapatapu", md)

    def test_company_to_markdown(self):
        info = _COMPANY_INFO["CompanyInfo"]
        md = qb.company_to_markdown(info)
        self.assertIn("Sandbox Company_US_1", md)
        self.assertIn("Mountain View", md)

    def test_pick_selects_fields(self):
        """api.pick works on QB response dicts for field pre-selection."""
        cust = _CUSTOMER_SINGLE["Customer"]
        picked = api.pick(cust, "Id,DisplayName,Balance")
        self.assertEqual(picked["Id"], "42")
        self.assertEqual(picked["Balance"], 629.10)


class QuickbooksCLI(_Base):
    """CLI entry point drives the connector for each subcommand."""

    @responses_lib.activate
    def test_cli_customer_by_name(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_CUSTOMER_PAGE_1, status=200)
        rc = qb.main(["customer", "Lauterbach", "--realm", REALM_ID])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_cli_invoices_for_customer(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_INVOICE_LIST, status=200)
        rc = qb.main(["invoices", "--customer-id", "42", "--realm", REALM_ID])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_invoice_single(self):
        responses_lib.add(
            responses_lib.GET,
            f"{BASE}/invoice/1001",
            json=_INVOICE_SINGLE,
            status=200,
        )
        rc = qb.main(["invoice", "1001", "--realm", REALM_ID])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_company(self):
        responses_lib.add(
            responses_lib.GET,
            f"{BASE}/companyinfo/{REALM_ID}",
            json=_COMPANY_INFO,
            status=200,
        )
        rc = qb.main(["company", "--realm", REALM_ID])
        self.assertEqual(rc, 0)


class QuickbooksTokenHygiene(unittest.TestCase):
    """CI guard: no QB OAuth token material in the connector directory.

    Splits the prefix literals with string concatenation so this guard
    doesn't flag itself.
    """

    # QB OAuth access token prefixes (Intuit tokens start with "Bearer " in headers
    # but the raw token is a long base64 JWT; check for common Intuit SDK placeholder patterns).
    # We guard against accidentally committing a real token by checking for the Intuit
    # prod token placeholder prefixes used in their own docs/SDKs.
    _TOKEN_PREFIXES = (
        "eyJlb" + "mMiOiJBMTI4",          # Intuit prod JWT token prefix (base64 encoded "{"enc":"A128")
        "intuit_" + "token",               # generic placeholder used in Intuit docs
    )

    def test_no_token_material_in_quickbooks_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "quickbooks"
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
