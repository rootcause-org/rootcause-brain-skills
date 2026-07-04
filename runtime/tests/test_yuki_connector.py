"""Tests for the Yuki connector (manifest + SOAP script).

No live credentials, no network. The XML fixtures mirror Yuki SOAP envelopes from the public WSDL:
Authenticate -> session ID, then read methods on Accounting/Archive services.

Run:
    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_yuki_connector.py -q
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import api  # noqa: E402
import lib.connectors.yuki as yuki  # noqa: E402

BASE = "https://api.yukiworks.be/ws"
ADMIN_ID = "c2f87b75-f6cf-4791-b0f5-4eaa4be6a7f2"
DOMAIN_ID = "59034794-9338-48d8-ad5c-0252161735cf"


def _soap(result_name: str, inner: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <{result_name} xmlns="http://www.theyukicompany.com/">
      {inner}
    </{result_name}>
  </soap:Body>
</soap:Envelope>"""


AUTH_BODY = _soap("AuthenticateResponse", "<AuthenticateResult>SESSION-123</AuthenticateResult>")

DOMAINS_BODY = _soap(
    "DomainsResponse",
    f"""
    <DomainsResult>
      <Domain>
        <ID>{DOMAIN_ID}</ID>
        <Name>KampAdmin</Name>
      </Domain>
    </DomainsResult>
    """,
)

ADMINISTRATIONS_BODY = _soap(
    "AdministrationsResponse",
    f"""
    <AdministrationsResult>
      <Administration>
        <ID>{ADMIN_ID}</ID>
        <Name>KampAdmin BV</Name>
      </Administration>
    </AdministrationsResult>
    """,
)

OUTSTANDING_BODY = _soap(
    "OutstandingCreditorItemsResponse",
    """
    <OutstandingCreditorItemsResult>
      <item>
        <date>2026-04-14</date>
        <contact>Acme BV</contact>
        <openAmount>121.00</openAmount>
        <originalAmount>121.00</originalAmount>
        <type>Aankoopfactuur</type>
        <description>Factuur van Acme BV | Klantreferentie: INV-2026-0042</description>
        <VATNumber>BE0123456789</VATNumber>
        <CoCNumber>0123.456.789</CoCNumber>
      </item>
      <item>
        <date>2026-04-15</date>
        <contact>Other Supplier</contact>
        <openAmount>42.00</openAmount>
        <originalAmount>42.00</originalAmount>
        <type>Banktransactie</type>
        <description>Card payment</description>
      </item>
    </OutstandingCreditorItemsResult>
    """,
)

SEARCH_BODY = _soap(
    "SearchDocumentsResponse",
    """
    <SearchDocumentsResult>
      <Document>
        <ID>doc-1</ID>
        <Subject>INV-2026-0042 Acme BV</Subject>
        <Contact>Acme BV</Contact>
      </Document>
    </SearchDocumentsResult>
    """,
)


class _EnvPatch:
    def __init__(self, key: str, value: str):
        self.key = key
        self.value = value
        self.old = None

    def __enter__(self):
        self.old = os.environ.get(self.key)
        os.environ[self.key] = self.value

    def __exit__(self, *_):
        if self.old is None:
            os.environ.pop(self.key, None)
        else:
            os.environ[self.key] = self.old


def _mock_env_token():
    return _EnvPatch("RC_CONN_YUKI", "yuki_test_access_key_mock")


def _add_auth():
    responses_lib.add(responses_lib.POST, f"{BASE}/AccountingInfo.asmx", body=AUTH_BODY, status=200)


class TestYukiManifest(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loads_via_yaml_loader(self):
        manifests = api.load_manifests()
        self.assertIn("yuki", manifests)
        m = manifests["yuki"]
        self.assertEqual(m.key, "yuki")
        self.assertEqual(m.base_url, BASE)
        self.assertEqual(m.auth.strategy, "none")

    def test_connector_registration_matches_manifest(self):
        self.assertEqual(yuki.MANIFEST.key, "yuki")
        self.assertEqual(yuki.MANIFEST.base_url, BASE)


class TestYukiSoapClient(unittest.TestCase):
    def setUp(self):
        self.env = _mock_env_token()
        self.env.__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    @responses_lib.activate
    def test_domains_authenticates_and_parses_records(self):
        _add_auth()
        responses_lib.add(responses_lib.POST, f"{BASE}/AccountingInfo.asmx", body=DOMAINS_BODY, status=200)

        client = yuki.Client()
        domains = client.domains()

        self.assertEqual(client.session_id, "SESSION-123")
        self.assertEqual(domains[0]["id"], DOMAIN_ID)
        self.assertEqual(domains[0]["name"], "KampAdmin")
        auth_request = responses_lib.calls[0].request.body.decode()
        self.assertIn("<accessKey>yuki_test_access_key_mock</accessKey>", auth_request)
        self.assertIn("SOAPAction", responses_lib.calls[0].request.headers)

    @responses_lib.activate
    def test_administrations_sets_domain_first(self):
        _add_auth()
        responses_lib.add(responses_lib.POST, f"{BASE}/AccountingInfo.asmx", body=_soap("SetCurrentDomainResponse", ""), status=200)
        responses_lib.add(responses_lib.POST, f"{BASE}/AccountingInfo.asmx", body=ADMINISTRATIONS_BODY, status=200)

        client = yuki.Client().connect(domain_id=DOMAIN_ID)
        admins = client.administrations()

        self.assertEqual(admins[0]["id"], ADMIN_ID)
        set_domain_body = responses_lib.calls[1].request.body.decode()
        self.assertIn(f"<domainID>{DOMAIN_ID}</domainID>", set_domain_body)

    @responses_lib.activate
    def test_outstanding_invoice_status_matches_supplier_reference_amount(self):
        _add_auth()
        responses_lib.add(responses_lib.POST, f"{BASE}/Accounting.asmx", body=OUTSTANDING_BODY, status=200)

        status = yuki.Client().supplier_invoice_status(
            ADMIN_ID,
            supplier="Acme",
            reference="INV-2026-0042",
            amount=121,
        )

        self.assertEqual(status["status"], "open_or_unpaid")
        self.assertEqual(status["evidence"][0]["contact"], "Acme BV")
        self.assertEqual(status["evidence"][0]["open_amount"], 121.0)
        self.assertEqual(status["evidence"][0]["vat_number"], "BE0123456789")

    @responses_lib.activate
    def test_invoice_status_absent_is_not_currently_outstanding(self):
        _add_auth()
        responses_lib.add(responses_lib.POST, f"{BASE}/Accounting.asmx", body=OUTSTANDING_BODY, status=200)

        status = yuki.Client().supplier_invoice_status(ADMIN_ID, supplier="Missing BV", reference="NOPE")

        self.assertEqual(status["status"], "not_currently_outstanding")
        self.assertEqual(status["evidence"], [])

    @responses_lib.activate
    def test_search_documents_parses_archive_results(self):
        _add_auth()
        responses_lib.add(responses_lib.POST, f"{BASE}/Archive.asmx", body=SEARCH_BODY, status=200)

        docs = yuki.Client().search_documents(text="INV-2026-0042")

        self.assertEqual(docs, [{"id": "doc-1", "subject": "INV-2026-0042 Acme BV", "contact": "Acme BV"}])
        search_body = responses_lib.calls[1].request.body.decode()
        self.assertIn("<folderID>-1</folderID>", search_body)
        self.assertIn("<tabID>-1</tabID>", search_body)

    @responses_lib.activate
    def test_archive_search_can_set_domain_before_read(self):
        _add_auth()
        responses_lib.add(responses_lib.POST, f"{BASE}/AccountingInfo.asmx", body=_soap("SetCurrentDomainResponse", ""), status=200)
        responses_lib.add(responses_lib.POST, f"{BASE}/Archive.asmx", body=SEARCH_BODY, status=200)

        client = yuki.Client().connect(domain_id=DOMAIN_ID)
        client.search_documents(text="INV-2026-0042")

        set_domain_body = responses_lib.calls[1].request.body.decode()
        self.assertIn(f"<domainID>{DOMAIN_ID}</domainID>", set_domain_body)

    def test_private_soap_call_refuses_upload_operations(self):
        client = yuki.Client(api_key="test")

        with self.assertRaisesRegex(yuki.YukiError, "not available in read-only grounding"):
            client._call("archive", "UploadDocument", {"fileName": "x.pdf"}, result_name="UploadDocumentResult")

    @responses_lib.activate
    def test_cli_invoice_status_prints_json(self):
        _add_auth()
        responses_lib.add(responses_lib.POST, f"{BASE}/Accounting.asmx", body=OUTSTANDING_BODY, status=200)
        out = io.StringIO()

        with patch("sys.stdout", out):
            rc = yuki.main([
                "invoice-status",
                "--administration-id",
                ADMIN_ID,
                "--supplier",
                "Acme",
                "--reference",
                "INV-2026-0042",
            ])

        self.assertEqual(rc, 0)
        body = json.loads(out.getvalue())
        self.assertEqual(body["status"], "open_or_unpaid")

    @responses_lib.activate
    def test_soap_fault_raises_loudly(self):
        _add_auth()
        fault = """<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body><soap:Fault><faultstring>bad admin</faultstring></soap:Fault></soap:Body>
</soap:Envelope>"""
        responses_lib.add(responses_lib.POST, f"{BASE}/Accounting.asmx", body=fault, status=200)

        with self.assertRaisesRegex(yuki.YukiError, "bad admin"):
            yuki.Client().outstanding_creditor_items(ADMIN_ID)


class TestYukiUploadPayload(unittest.TestCase):
    def test_upload_payload_is_action_ready_and_not_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            invoice = Path(tmp) / "invoice.pdf"
            invoice.write_bytes(b"%PDF fake invoice")

            payload = yuki.prepare_upload_document(invoice, administration_id=ADMIN_ID, folder=42)

        self.assertEqual(payload.service, "archive")
        self.assertEqual(payload.operation, "UploadDocument")
        self.assertEqual(payload.filename, "invoice.pdf")
        self.assertEqual(payload.message["administrationID"], ADMIN_ID)
        self.assertEqual(payload.message["folder"], 42)
        self.assertEqual(payload.message["data"], "JVBERiBmYWtlIGludm9pY2U=")

    def test_upload_with_data_adds_accounting_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            invoice = Path(tmp) / "invoice.pdf"
            invoice.write_bytes(b"invoice")

            payload = yuki.prepare_upload_document_with_data(
                invoice,
                administration_id=ADMIN_ID,
                folder=42,
                currency="EUR",
                amount="121.00",
                payment_method=7,
                cost_category="Software",
                remarks="prechecked duplicate search",
            )

        self.assertEqual(payload.operation, "UploadDocumentWithData")
        self.assertEqual(payload.message["amount"], "121.00")
        self.assertEqual(payload.message["paymentMethod"], 7)
        self.assertEqual(payload.message["costCategory"], "Software")


if __name__ == "__main__":
    unittest.main()
