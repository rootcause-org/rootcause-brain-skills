"""Fixture tests for the Billit OAuth/API-key connectors.

No live creds, no network: HTTP is mocked with responses. Payloads mirror Billit's documented account,
orders, files, and Peppol inbox shapes, trimmed to support-relevant fields.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import api  # noqa: E402
from lib.connectors import billit  # noqa: E402

BASE = "https://api.billit.be"


class BillitBase(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        api.load_manifests()
        self.saved_oauth = os.environ.get("RC_CONN_BILLIT")
        self.saved_key = os.environ.get("RC_CONN_BILLIT_APIKEY")
        os.environ["RC_CONN_BILLIT"] = "oauth_access_fixture"
        os.environ["RC_CONN_BILLIT_APIKEY"] = "api_key_fixture"

    def tearDown(self):
        if self.saved_oauth is None:
            os.environ.pop("RC_CONN_BILLIT", None)
        else:
            os.environ["RC_CONN_BILLIT"] = self.saved_oauth
        if self.saved_key is None:
            os.environ.pop("RC_CONN_BILLIT_APIKEY", None)
        else:
            os.environ["RC_CONN_BILLIT_APIKEY"] = self.saved_key


class BillitManifestTest(BillitBase):
    def test_manifests_load_oauth_and_api_key_fallback(self):
        oauth = api.MANIFESTS["billit"]
        self.assertEqual(oauth.base_url, BASE)
        self.assertEqual(oauth.auth.strategy, "bearer")
        self.assertEqual(oauth.default_headers["Accept"], "application/json")

        api_key = api.MANIFESTS["billit_apikey"]
        self.assertEqual(api_key.auth.strategy, "api_key_header")
        self.assertEqual(api_key.auth.name, "ApiKey")


class BillitReadTest(BillitBase):
    @responses.activate
    def test_orders_uses_oauth_bearer_and_party_header(self):
        responses.add(
            responses.GET,
            f"{BASE}/v1/orders",
            json={
                "Items": [
                    {
                        "OrderID": 1077603,
                        "CompanyID": 574991,
                        "OrderNumber": "QS-Contact2",
                        "OrderDirection": "Cost",
                        "OrderType": "Invoice",
                        "OrderStatus": "ToPay",
                        "TotalIncl": 121.0,
                    }
                ]
            },
            status=200,
        )

        body = billit.orders(
            connection="billit",
            party_id="574991",
            filter_expr="OrderType eq 'Invoice' and OrderDirection eq 'Cost'",
        )

        self.assertEqual(body["Items"][0]["OrderID"], 1077603)
        req = responses.calls[0].request
        self.assertEqual(req.headers["Authorization"], "Bearer oauth_access_fixture")
        self.assertEqual(req.headers["PartyID"], "574991")
        self.assertIn("%24filter=OrderType+eq+%27Invoice%27", req.url)

    @responses.activate
    def test_api_key_fallback_uses_apikey_and_party_header(self):
        responses.add(responses.GET, f"{BASE}/v1/account", json={"PartyID": 574991}, status=200)

        body = billit.account(connection="billit_apikey", party_id="574991")

        self.assertEqual(body["PartyID"], 574991)
        req = responses.calls[0].request
        self.assertEqual(req.headers["ApiKey"], "api_key_fixture")
        self.assertEqual(req.headers["PartyID"], "574991")
        self.assertNotIn("Authorization", req.headers)

    @responses.activate
    def test_file_omits_base64_content_by_default(self):
        responses.add(
            responses.GET,
            f"{BASE}/v1/files/file-1",
            json={
                "FileID": "file-1",
                "FileName": "invoice.xml",
                "MimeType": "text/xml",
                "FileContent": "PD94bWwgdmVyc2lvbj0iMS4wIj8+",
            },
            status=200,
        )

        body = billit.file(connection="billit", party_id="574991", file_id="file-1")

        self.assertEqual(body["FileContent"], "<base64 omitted; rerun with --include-content if needed>")

    @responses.activate
    def test_cli_accepts_documented_metadata_only_flag(self):
        responses.add(
            responses.GET,
            f"{BASE}/v1/files/file-1",
            json={
                "FileID": "file-1",
                "FileName": "invoice.pdf",
                "MimeType": "application/pdf",
                "FileContent": "JVBERi0x",
            },
            status=200,
        )

        rc = billit.main(["--party-id", "574991", "file", "file-1", "--metadata-only"])

        self.assertEqual(rc, 0)

    @responses.activate
    def test_cli_peppol_inbox_trims_output_and_sets_headers(self):
        responses.add(
            responses.GET,
            f"{BASE}/v1/peppol/inbox",
            json={
                "InboxItems": [
                    {
                        "InboxItemID": 69791,
                        "SenderPeppolID": "0208:0759529202",
                        "ReceiverPeppolID": "9925:BE0437295999",
                        "ReceiverCompanyID": "BE0437295999",
                        "PeppolDocumentType": "IMR",
                        "CreationDate": "2025-04-08T22:31:39.544541",
                        "PeppolFileID": "8758f630-1fbe-478c-b872-77f5652e2999",
                    }
                ]
            },
            status=200,
        )

        rc = billit.main(["--party-id", "574991", "peppol-inbox"])

        self.assertEqual(rc, 0)
        req = responses.calls[0].request
        self.assertEqual(req.headers["Authorization"], "Bearer oauth_access_fixture")
        self.assertEqual(req.headers["PartyID"], "574991")


if __name__ == "__main__":
    unittest.main()
