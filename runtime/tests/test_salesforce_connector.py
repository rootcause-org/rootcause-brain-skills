"""Tests for the Salesforce support connector (lib.connectors.salesforce).

No live creds, no network: HTTP is mocked with `responses`. Fixture bodies mirror Salesforce's
documented REST API example payloads (trimmed to support-relevant fields).

Tests cover:
- manifest.yaml loads via lib.api YAML loader and maps every field correctly
- SOQL pagination via nextRecordsUrl stitches ≥2 pages
- bearer credential rides EVERY request including the nextRecordsUrl follow
- lib.api.pick selects support fields from Case records
- CLI (main([...])) drives cases + contact subcommands
- Token-prefix hygiene guard: no Salesforce token literal in connector files

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \
        pytest tests/test_salesforce_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import salesforce as sf  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures: Salesforce documented example payloads (trimmed to support fields)
# ---------------------------------------------------------------------------

_INSTANCE = "https://myorg.my.salesforce.com"
_QUERY_URL = f"{_INSTANCE}/services/data/v59.0/query"
_NEXT_URL_PATH = "/services/data/v59.0/query/01gAB0000001234ABC"
_NEXT_URL_FULL = f"{_INSTANCE}{_NEXT_URL_PATH}"

# Page 1: done=False, nextRecordsUrl set → connector should follow.
_CASE_PAGE_1 = {
    "totalSize": 2,
    "done": False,
    "nextRecordsUrl": _NEXT_URL_PATH,
    "records": [
        {
            "attributes": {"type": "Case", "url": "/services/data/v59.0/sobjects/Case/5000P000007DaQUQA0"},
            "Id": "5000P000007DaQUQA0",
            "CaseNumber": "00001001",
            "Subject": "Login timeout after upgrade",
            "Description": "Users see 'Session expired' after the 3.4 release.",
            "Status": "Open",
            "Priority": "High",
            "Origin": "Email",
            "CreatedDate": "2024-03-01T09:15:00.000+0000",
            "LastModifiedDate": "2024-03-02T14:22:00.000+0000",
            "Contact": {"attributes": {"type": "Contact"}, "Name": "Alice Smith", "Email": "alice@example.com"},
            "Account": {"attributes": {"type": "Account"}, "Name": "Example Corp"},
            "OwnerId": "0053000000AbcXYZ",
        }
    ],
}

# Page 2: done=True, no nextRecordsUrl → pagination stops.
_CASE_PAGE_2 = {
    "totalSize": 2,
    "done": True,
    "records": [
        {
            "attributes": {"type": "Case", "url": "/services/data/v59.0/sobjects/Case/5000P000007DbQUQA0"},
            "Id": "5000P000007DbQUQA0",
            "CaseNumber": "00001002",
            "Subject": "Export fails silently",
            "Description": "CSV export returns 200 but file is empty.",
            "Status": "In Progress",
            "Priority": "Medium",
            "Origin": "Phone",
            "CreatedDate": "2024-02-20T11:00:00.000+0000",
            "LastModifiedDate": "2024-02-21T09:00:00.000+0000",
            "Contact": {"attributes": {"type": "Contact"}, "Name": "Bob Jones", "Email": "bob@example.com"},
            "Account": {"attributes": {"type": "Account"}, "Name": "Example Corp"},
            "OwnerId": "0053000000AbcXYZ",
        }
    ],
}

_CONTACT_RESPONSE = {
    "totalSize": 1,
    "done": True,
    "records": [
        {
            "attributes": {"type": "Contact"},
            "Id": "0033000000AbcABCDE",
            "Name": "Alice Smith",
            "Email": "alice@example.com",
            "Phone": "+1-415-555-0100",
            "Title": "CTO",
            "Department": "Engineering",
            "Account": {"attributes": {"type": "Account"}, "Name": "Example Corp", "Id": "0013000000AbcXYZ"},
            "CreatedDate": "2023-01-10T08:00:00.000+0000",
            "LastModifiedDate": "2024-01-01T00:00:00.000+0000",
        }
    ],
}

_EMPTY_RESPONSE = {"totalSize": 0, "done": True, "records": []}


class SalesforceManifestLoad(unittest.TestCase):
    """manifest.yaml loads via lib.api YAML loader and maps all fields."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_SALESFORCE")
        os.environ["RC_CONN_SALESFORCE"] = "00D!_test_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_SALESFORCE", None)
        else:
            os.environ["RC_CONN_SALESFORCE"] = self._saved
        # Reset the register() call that __init__.py makes so the YAML loader test is clean.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loads_and_maps_fields(self):
        """YAML loader picks up the salesforce manifest and maps every key field."""
        m = api.load_manifests()
        self.assertIn("salesforce", m)
        sf_m = m["salesforce"]
        self.assertEqual(sf_m.key, "salesforce")
        # base_url is "" in the manifest (instance is per-org); loader accepts it.
        self.assertIsInstance(sf_m.base_url, str)
        self.assertEqual(sf_m.auth.strategy, "bearer")
        self.assertEqual(sf_m.pagination.style, "none")
        self.assertEqual(sf_m.pagination.items_field, "records")
        self.assertEqual(sf_m.rate_limit_remaining_header, "")

    def test_connector_register_takes_precedence(self):
        """Importing the connector re-registers the manifest; load_manifests sees it."""
        # The import-time register() ran when the module was first imported. setUp cleared MANIFESTS,
        # so we call load_manifests() here and verify the salesforce entry is present and correct.
        m = api.load_manifests()
        self.assertIn("salesforce", m)
        self.assertEqual(m["salesforce"].auth.strategy, "bearer")


class SalesforceQueryPagination(unittest.TestCase):
    """nextRecordsUrl pagination stitches ≥2 pages; bearer rides every request."""

    def setUp(self):
        # Reset instance URL cache so tests are independent.
        sf._instance_url._cached = ""
        self._saved = os.environ.get("RC_CONN_SALESFORCE")
        os.environ["RC_CONN_SALESFORCE"] = "00D!_bearer_test"
        self._saved_inst = os.environ.get("RC_CONN_SALESFORCE_INSTANCE")
        os.environ["RC_CONN_SALESFORCE_INSTANCE"] = _INSTANCE

    def tearDown(self):
        sf._instance_url._cached = ""
        if self._saved is None:
            os.environ.pop("RC_CONN_SALESFORCE", None)
        else:
            os.environ["RC_CONN_SALESFORCE"] = self._saved
        if self._saved_inst is None:
            os.environ.pop("RC_CONN_SALESFORCE_INSTANCE", None)
        else:
            os.environ["RC_CONN_SALESFORCE_INSTANCE"] = self._saved_inst

    @responses_lib.activate
    def test_pagination_stitches_two_pages(self):
        """_soql_query follows nextRecordsUrl and returns records from both pages."""
        responses_lib.add(responses_lib.GET, _QUERY_URL, json=_CASE_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, _NEXT_URL_FULL, json=_CASE_PAGE_2, status=200)

        records = sf._soql_query(
            "SELECT Id,CaseNumber,Subject FROM Case WHERE Account.Name = 'Example Corp' LIMIT 10",
            instance=_INSTANCE,
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["CaseNumber"], "00001001")
        self.assertEqual(records[1]["CaseNumber"], "00001002")
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_bearer_on_both_page_requests(self):
        """The bearer token must appear on the initial query AND the nextRecordsUrl follow."""
        responses_lib.add(responses_lib.GET, _QUERY_URL, json=_CASE_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, _NEXT_URL_FULL, json=_CASE_PAGE_2, status=200)

        sf._soql_query("SELECT Id FROM Case LIMIT 10", instance=_INSTANCE)

        for call in responses_lib.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(
                auth.startswith("Bearer "),
                f"Expected Bearer auth on {call.request.url!r}, got {auth!r}",
            )

    @responses_lib.activate
    def test_single_page_no_next(self):
        """When done=True on first page, only one request is made."""
        responses_lib.add(responses_lib.GET, _QUERY_URL, json=_EMPTY_RESPONSE, status=200)
        records = sf._soql_query("SELECT Id FROM Case WHERE Status = 'Closed' LIMIT 5", instance=_INSTANCE)
        self.assertEqual(records, [])
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_max_records_cap_stops_pagination(self):
        """max_records caps the collected records; no extra page is fetched if limit hit."""
        # Page 1 returns done=False but we cap at 1; connector should stop after page 1.
        responses_lib.add(responses_lib.GET, _QUERY_URL, json=_CASE_PAGE_1, status=200)
        records = sf._soql_query("SELECT Id FROM Case LIMIT 10", instance=_INSTANCE, max_records=1)
        self.assertEqual(len(records), 1)
        # Page 2 should NOT have been fetched (max_records=1 hit after page 1).
        self.assertEqual(len(responses_lib.calls), 1)


class SalesforceQueryHelpers(unittest.TestCase):
    """query_cases and query_contact build correct SOQL and return structured results."""

    def setUp(self):
        sf._instance_url._cached = ""
        self._saved = os.environ.get("RC_CONN_SALESFORCE")
        os.environ["RC_CONN_SALESFORCE"] = "00D!_bearer_test"
        self._saved_inst = os.environ.get("RC_CONN_SALESFORCE_INSTANCE")
        os.environ["RC_CONN_SALESFORCE_INSTANCE"] = _INSTANCE

    def tearDown(self):
        sf._instance_url._cached = ""
        if self._saved is None:
            os.environ.pop("RC_CONN_SALESFORCE", None)
        else:
            os.environ["RC_CONN_SALESFORCE"] = self._saved
        if self._saved_inst is None:
            os.environ.pop("RC_CONN_SALESFORCE_INSTANCE", None)
        else:
            os.environ["RC_CONN_SALESFORCE_INSTANCE"] = self._saved_inst

    @responses_lib.activate
    def test_query_cases_by_email(self):
        """query_cases with email filter returns cases and SOQL contains the email."""
        # Return single-page response (done=True).
        single_page = {**_CASE_PAGE_1, "done": True, "nextRecordsUrl": None}
        del single_page["nextRecordsUrl"]
        responses_lib.add(responses_lib.GET, _QUERY_URL, json=single_page, status=200)

        cases = sf.query_cases(email="alice@example.com", instance=_INSTANCE)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["CaseNumber"], "00001001")
        # SOQL in the request should reference the email.
        q_param = responses_lib.calls[0].request.params.get("q", "")
        self.assertIn("alice@example.com", q_param)
        self.assertIn("Contact.Email", q_param)

    @responses_lib.activate
    def test_query_cases_by_account_name(self):
        """query_cases with account name builds WHERE Account.Name = ... SOQL."""
        single_page = {**_CASE_PAGE_1, "done": True}
        del single_page["nextRecordsUrl"]
        responses_lib.add(responses_lib.GET, _QUERY_URL, json=single_page, status=200)

        sf.query_cases(account="Example Corp", instance=_INSTANCE)
        q = responses_lib.calls[0].request.params.get("q", "")
        self.assertIn("Account.Name", q)
        self.assertIn("Example Corp", q)

    @responses_lib.activate
    def test_query_cases_by_account_id(self):
        """query_cases with a Salesforce Account ID (starts with 001) uses AccountId = ..."""
        single_page = {**_CASE_PAGE_1, "done": True}
        del single_page["nextRecordsUrl"]
        responses_lib.add(responses_lib.GET, _QUERY_URL, json=single_page, status=200)

        sf.query_cases(account="0013000000ABCDEFGH", instance=_INSTANCE)
        q = responses_lib.calls[0].request.params.get("q", "")
        # WHERE clause should use AccountId = '...' (direct ID match), not Account.Name = '...'
        # Note: Account.Name still appears in the SELECT field list; check the WHERE specifically.
        self.assertIn("AccountId = '0013000000ABCDEFGH'", q)
        self.assertNotIn("Account.Name = ", q)

    def test_query_cases_requires_filter(self):
        """query_cases with neither email nor account raises RuntimeError."""
        with self.assertRaises(RuntimeError):
            sf.query_cases(instance=_INSTANCE)

    @responses_lib.activate
    def test_query_contact_returns_contact(self):
        """query_contact fetches the contact and returns the first record."""
        responses_lib.add(responses_lib.GET, _QUERY_URL, json=_CONTACT_RESPONSE, status=200)
        contact = sf.query_contact("alice@example.com", instance=_INSTANCE)
        self.assertIsNotNone(contact)
        self.assertEqual(contact["Name"], "Alice Smith")
        self.assertEqual(contact["Email"], "alice@example.com")

    @responses_lib.activate
    def test_query_contact_not_found(self):
        """query_contact returns None when no record matches."""
        responses_lib.add(responses_lib.GET, _QUERY_URL, json=_EMPTY_RESPONSE, status=200)
        contact = sf.query_contact("nobody@example.com", instance=_INSTANCE)
        self.assertIsNone(contact)


class SalesforcePickAndRender(unittest.TestCase):
    """api.pick selects support fields; markdown renderers produce expected output."""

    def test_pick_case_fields(self):
        """api.pick extracts the key Case fields from a raw record."""
        case = _CASE_PAGE_1["records"][0]
        picked = api.pick(case, "CaseNumber,Subject,Status,Priority,Contact.Name,Account.Name")
        self.assertEqual(picked["CaseNumber"], "00001001")
        self.assertEqual(picked["Subject"], "Login timeout after upgrade")
        self.assertEqual(picked["Status"], "Open")
        self.assertEqual(picked["Contact.Name"], "Alice Smith")
        self.assertEqual(picked["Account.Name"], "Example Corp")

    def test_cases_to_markdown(self):
        """cases_to_markdown renders case list with subject, status, contact."""
        md = sf.cases_to_markdown(_CASE_PAGE_1["records"] + _CASE_PAGE_2["records"])
        self.assertIn("00001001", md)
        self.assertIn("Login timeout after upgrade", md)
        self.assertIn("**Open**", md)
        self.assertIn("Alice Smith", md)
        self.assertIn("00001002", md)
        self.assertIn("Export fails silently", md)

    def test_cases_to_markdown_empty(self):
        """cases_to_markdown with empty list produces a 'No cases found' message."""
        md = sf.cases_to_markdown([])
        self.assertIn("No cases found", md)

    def test_contact_to_markdown(self):
        """contact_to_markdown renders name, email, account."""
        contact = _CONTACT_RESPONSE["records"][0]
        md = sf.contact_to_markdown(contact)
        self.assertIn("Alice Smith", md)
        self.assertIn("alice@example.com", md)
        self.assertIn("Example Corp", md)
        self.assertIn("CTO", md)

    def test_contact_to_markdown_not_found(self):
        """contact_to_markdown with None renders a 'no contact found' message."""
        md = sf.contact_to_markdown(None, email="ghost@example.com")
        self.assertIn("ghost@example.com", md)
        self.assertIn("no contact found", md.lower())


class SalesforceCLI(unittest.TestCase):
    """CLI main([...]) drives cases and contact subcommands."""

    def setUp(self):
        sf._instance_url._cached = ""
        self._saved = os.environ.get("RC_CONN_SALESFORCE")
        os.environ["RC_CONN_SALESFORCE"] = "00D!_bearer_test"
        self._saved_inst = os.environ.get("RC_CONN_SALESFORCE_INSTANCE")
        os.environ["RC_CONN_SALESFORCE_INSTANCE"] = _INSTANCE

    def tearDown(self):
        sf._instance_url._cached = ""
        if self._saved is None:
            os.environ.pop("RC_CONN_SALESFORCE", None)
        else:
            os.environ["RC_CONN_SALESFORCE"] = self._saved
        if self._saved_inst is None:
            os.environ.pop("RC_CONN_SALESFORCE_INSTANCE", None)
        else:
            os.environ["RC_CONN_SALESFORCE_INSTANCE"] = self._saved_inst

    @responses_lib.activate
    def test_cli_cases_by_email(self):
        """CLI cases --email returns exit 0 and fetches SOQL with bearer."""
        single = {**_CASE_PAGE_1, "done": True}
        del single["nextRecordsUrl"]
        responses_lib.add(responses_lib.GET, _QUERY_URL, json=single, status=200)

        rc = sf.main(["cases", "--email", "alice@example.com", "--instance", _INSTANCE])
        self.assertEqual(rc, 0)
        auth = responses_lib.calls[0].request.headers.get("Authorization", "")
        self.assertTrue(auth.startswith("Bearer "), f"Got: {auth!r}")

    @responses_lib.activate
    def test_cli_contact(self):
        """CLI contact <email> returns exit 0 and renders the contact."""
        responses_lib.add(responses_lib.GET, _QUERY_URL, json=_CONTACT_RESPONSE, status=200)
        rc = sf.main(["contact", "alice@example.com", "--instance", _INSTANCE])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_cases_no_filter_exits_nonzero(self):
        """CLI cases with neither --email nor --account should error before any HTTP call."""
        with self.assertRaises(SystemExit) as ctx:
            sf.main(["cases", "--instance", _INSTANCE])
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertEqual(len(responses_lib.calls), 0)


class SalesforceTokenHygiene(unittest.TestCase):
    """CI guard: no Salesforce token literal in the connector directory.

    Salesforce access tokens look like '00D...' (org ID prefix) or 'Bearer 00D...'. This test
    ensures no real-looking token prefix escapes into committed files.

    The prefixes are split with string concatenation so this guard file doesn't flag itself.
    """

    # Salesforce org ID prefix (access tokens embed the org ID): "00D" + the rest.
    # We check for the concatenated form that would appear in a real token.
    _TOKEN_PREFIXES = (
        "00D" "!",          # connected-app access tokens: 00D!<orgId>.<token>
        "Bearer 00D",       # token in an Authorization header value
        "access_token=00D", # token in a URL query string (should never appear)
    )

    def test_no_token_prefixes_in_salesforce_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "salesforce"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
