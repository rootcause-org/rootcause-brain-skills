"""Fixture coverage for the scripted Belgian Exact Online connector."""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import api  # noqa: E402
from lib.connectors import exactonline  # noqa: E402

API_BASE = "https://start.exactonline.be/api/v1"
DIVISION = 123456
ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"

_ACCOUNT_1 = {
    "ID": ACCOUNT_ID,
    "Code": "              1001",
    "Name": "O'Reilly Belgium",
    "Email": "billing@oreilly.example",
    "VATNumber": "BE0123456789",
    "Status": "C",
    "IsSales": True,
    "IsSupplier": False,
    "Blocked": False,
}
_ACCOUNT_2 = {
    "ID": "22222222-2222-2222-2222-222222222222",
    "Code": "              1002",
    "Name": "Example Services",
    "Email": "finance@example.test",
    "VATNumber": "BE0987654321",
    "Status": "C",
    "IsSales": True,
    "IsSupplier": False,
    "Blocked": False,
}


def _page(items: list[dict], next_url: str | None = None) -> dict:
    body: dict = {"results": items}
    if next_url:
        body["__next"] = next_url
    return {"d": body}


def _query(call_index: int) -> dict[str, list[str]]:
    return parse_qs(urlsplit(responses_lib.calls[call_index].request.url).query)


class _ExactOnlineBase(unittest.TestCase):
    def setUp(self):
        self._saved_token = os.environ.get("RC_CONN_EXACTONLINE")
        os.environ["RC_CONN_EXACTONLINE"] = "exactonline_fixture_bearer"
        self._saved_brokered = os.environ.pop("RC_API_BROKERED_KEYS", None)
        api.register(exactonline.MANIFEST)

    def tearDown(self):
        if self._saved_token is None:
            os.environ.pop("RC_CONN_EXACTONLINE", None)
        else:
            os.environ["RC_CONN_EXACTONLINE"] = self._saved_token
        if self._saved_brokered is not None:
            os.environ["RC_API_BROKERED_KEYS"] = self._saved_brokered


class TestExactOnlineManifest(_ExactOnlineBase):
    def test_yaml_maps_oauth_and_odata_contract(self):
        manifest = exactonline.MANIFEST
        self.assertEqual(manifest.base_url, API_BASE)
        self.assertEqual(manifest.auth.strategy, "bearer")
        self.assertEqual(manifest.pagination.style, "body_url")
        self.assertEqual(manifest.pagination.next_url_field, "d.__next")
        self.assertEqual(manifest.pagination.items_field, "d.results")
        self.assertEqual(manifest.pagination.page_size, 60)
        self.assertEqual(manifest.default_headers["Accept"], "application/json")


class TestExactOnlineReads(_ExactOnlineBase):
    @responses_lib.activate
    def test_current_identity_and_default_division(self):
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/current/Me",
            json=_page([{"CurrentDivision": DIVISION, "FullName": "Ada Example", "UserID": "user-1"}]),
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/{DIVISION}/system/Divisions",
            json=_page([{"Code": DIVISION, "Description": "Example Belgium", "Current": True}]),
            status=200,
        )

        result = exactonline.divisions()

        self.assertEqual(result["items"][0]["Code"], DIVISION)
        self.assertEqual(_query(0)["$select"], ["CurrentDivision,FullName,UserID"])
        self.assertEqual(_query(1)["$select"], ["Code,Description,Current"])
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], "Bearer exactonline_fixture_bearer")
            self.assertEqual(call.request.headers["Accept"], "application/json")

    @responses_lib.activate
    def test_account_search_escapes_odata_literal_and_selects_supported_fields(self):
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/{DIVISION}/crm/Accounts",
            json=_page([_ACCOUNT_1]),
            status=200,
        )

        result = exactonline.accounts("O'Reilly", field="name", division=DIVISION)

        self.assertEqual(result["items"][0]["Name"], "O'Reilly Belgium")
        query = _query(0)
        self.assertEqual(query["$filter"], ["substringof('O''Reilly',Name)"])
        self.assertEqual(
            query["$select"],
            ["ID,Code,Name,Email,Phone,VATNumber,Status,IsSales,IsSupplier,Blocked"],
        )
        self.assertNotIn("IsCustomer", query["$select"][0])

    @responses_lib.activate
    def test_numeric_account_code_is_left_padded_to_exact_fixed_width(self):
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/{DIVISION}/crm/Accounts",
            json=_page([_ACCOUNT_1]),
            status=200,
        )

        exactonline.accounts("1001", field="code", division=DIVISION)

        self.assertEqual(_query(0)["$filter"], ["Code eq '              1001'"])

    @responses_lib.activate
    def test_default_account_search_also_pads_numeric_code_candidate(self):
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/{DIVISION}/crm/Accounts",
            json=_page([_ACCOUNT_1]),
            status=200,
        )

        exactonline.accounts("1001", division=DIVISION)

        self.assertIn("Code eq '              1001'", _query(0)["$filter"][0])

    @responses_lib.activate
    def test_two_page_odata_next_is_followed_by_lib_api(self):
        first_url = f"{API_BASE}/{DIVISION}/crm/Accounts"
        next_url = (
            f"{first_url}?%24select=ID%2CName%2CEmail&%24skiptoken="
            "guid%2722222222-2222-2222-2222-222222222222%27"
        )
        responses_lib.add(responses_lib.GET, first_url, json=_page([_ACCOUNT_1], next_url), status=200)
        responses_lib.add(responses_lib.GET, next_url, json=_page([_ACCOUNT_2]), status=200)

        result = exactonline.accounts("customer", division=DIVISION)

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual([item["ID"] for item in result["items"]], [ACCOUNT_ID, _ACCOUNT_2["ID"]])
        self.assertEqual(len(responses_lib.calls), 2)
        self.assertIn("$skiptoken", _query(1))

    @responses_lib.activate
    def test_sales_invoice_filter_uses_validated_guid_and_fixed_select(self):
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/{DIVISION}/salesinvoice/SalesInvoices",
            json=_page([]),
            status=200,
        )

        exactonline.sales_invoices(division=DIVISION, account_id=ACCOUNT_ID, invoice_number="20260042")

        query = _query(0)
        self.assertEqual(
            query["$filter"],
            [f"InvoiceTo eq guid'{ACCOUNT_ID}' and InvoiceNumber eq 20260042"],
        )
        self.assertIn("InvoiceID,InvoiceNumber,InvoiceDate,DueDate", query["$select"][0])

    @responses_lib.activate
    def test_account_summary_joins_account_invoices_and_open_receivables(self):
        invoice = {
            "InvoiceID": "33333333-3333-3333-3333-333333333333",
            "InvoiceNumber": 20260042,
            "InvoiceDate": "/Date(1780185600000)/",
            "DueDate": "/Date(1782864000000)/",
            "Status": 50,
            "StatusDescription": "Processed",
            "InvoiceTo": ACCOUNT_ID,
            "InvoiceToName": _ACCOUNT_1["Name"],
            "AmountFC": 1210.0,
            "Currency": "EUR",
        }
        receivable = {
            "ID": "44444444-4444-4444-4444-444444444444",
            "Account": ACCOUNT_ID,
            "AccountName": _ACCOUNT_1["Name"],
            "InvoiceNumber": invoice["InvoiceNumber"],
            "DueDate": invoice["DueDate"],
            "AmountFC": 1210.0,
            "Currency": "EUR",
            "Status": 20,
            "IsFullyPaid": False,
        }
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/{DIVISION}/crm/Accounts",
            json=_page([_ACCOUNT_1]),
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/{DIVISION}/salesinvoice/SalesInvoices",
            json=_page([invoice]),
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/{DIVISION}/cashflow/Receivables",
            json=_page([receivable]),
            status=200,
        )

        result = exactonline.account_summary("billing@oreilly.example", division=DIVISION)

        self.assertTrue(result["found"])
        self.assertEqual(result["account"]["ID"], ACCOUNT_ID)
        self.assertEqual(result["sales_invoices"][0]["InvoiceNumber"], 20260042)
        self.assertEqual(result["open_receivables"][0]["Status"], 20)
        self.assertFalse(result["incomplete"])
        self.assertEqual(_query(0)["$filter"], ["Email eq 'billing@oreilly.example'"])
        self.assertEqual(_query(1)["$filter"], [f"InvoiceTo eq guid'{ACCOUNT_ID}'"])
        self.assertEqual(_query(2)["$filter"], [f"Account eq guid'{ACCOUNT_ID}' and Status eq 20"])

    @responses_lib.activate
    def test_account_summary_preserves_partial_lookup_reason(self):
        matches = [
            dict(
                _ACCOUNT_1,
                ID=f"{index:08x}-0000-0000-0000-000000000000",
                Name=f"Customer {index}",
            )
            for index in range(10)
        ]
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/{DIVISION}/crm/Accounts",
            json=_page(matches),
            status=200,
        )

        result = exactonline.account_summary("customer", division=DIVISION)

        self.assertFalse(result["found"])
        self.assertTrue(result["ambiguous"])
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["reason"], "reached max_items=10")

    @responses_lib.activate
    def test_connector_cli_emits_compact_json(self):
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/current/Me",
            json=_page([{"CurrentDivision": DIVISION, "FullName": "Ada Example", "UserID": "user-1"}]),
            status=200,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            rc = exactonline.main(["me"])

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue())["CurrentDivision"], DIVISION)

    def test_invalid_guid_is_rejected_before_http(self):
        with self.assertRaisesRegex(RuntimeError, "invalid Exact Online UUID"):
            exactonline.sales_invoices(division=DIVISION, account_id="not-a-guid")


if __name__ == "__main__":
    unittest.main()
