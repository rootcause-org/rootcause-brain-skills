"""Fixture test for the myPonto/Ponto Connect integration (manifest-only, driven via lib.api).

Ponto Connect exposes account-information reads as JSON:API GET endpoints. List responses carry
items under ``data`` and the next-page URL under body ``links.next``, so lib.api's ``body_url``
pagination style drives the loop. OAuth uses authorization code + PKCE with the ``ai`` and
``offline_access`` scopes; the runtime receives only a bearer access token.

No live creds, no network: HTTP is mocked with ``responses``. Bodies mirror the documented Ponto
Connect account/transaction shapes, trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --no-project \\
        pytest tests/test_myponto_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import api  # noqa: E402

BASE = "https://api.ibanity.com/ponto-connect"
ACCOUNTS = f"{BASE}/accounts"
TXNS = f"{BASE}/accounts/acc_1/transactions"


def _page(items: list[dict], next_url: str | None = None) -> dict:
    return {
        "data": items,
        "links": {"first": ACCOUNTS, "next": next_url},
        "meta": {"paging": {"limit": 100}},
    }


def _account(account_id: str, iban: str, balance: float = 1240.52) -> dict:
    return {
        "type": "account",
        "id": account_id,
        "attributes": {
            "reference": iban,
            "referenceType": "IBAN",
            "currency": "EUR",
            "currentBalance": balance,
            "availableBalance": balance,
            "description": "Main operating account",
            "holderName": "Acme BV",
            "synchronizedAt": "2026-06-30T08:15:00Z",
        },
        "relationships": {
            "financialInstitution": {"data": {"type": "financialInstitution", "id": "fi_1"}},
            "transactions": {"links": {"related": f"{BASE}/accounts/{account_id}/transactions"}},
        },
    }


def _transaction(tx_id: str, amount: float, name: str) -> dict:
    return {
        "type": "transaction",
        "id": tx_id,
        "attributes": {
            "amount": amount,
            "currency": "EUR",
            "status": "booked",
            "executionDate": "2026-06-28",
            "valueDate": "2026-06-28",
            "counterpartName": name,
            "counterpartReference": "BE56300694353788",
            "remittanceInformation": "INV-2026-0042",
            "remittanceInformationType": "unstructured",
            "description": "Invoice payment",
        },
    }


class MyPontoManifest(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_MYPONTO")
        os.environ["RC_CONN_MYPONTO"] = "tok_ponto_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MYPONTO", None)
        else:
            os.environ["RC_CONN_MYPONTO"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        manifests = api.load_manifests()
        self.assertIn("myponto", manifests)
        m = manifests["myponto"]
        self.assertEqual(m.key, "myponto")
        self.assertEqual(m.base_url, BASE)
        self.assertEqual(m.auth.strategy, "bearer")
        self.assertEqual(m.pagination.style, "body_url")
        self.assertEqual(m.pagination.next_url_field, "links.next")
        self.assertEqual(m.pagination.items_field, "data")
        self.assertEqual(m.pagination.page_size, 100)
        self.assertEqual(m.default_headers["Accept"], "application/vnd.api+json")


class MyPontoPagination(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_MYPONTO")
        os.environ["RC_CONN_MYPONTO"] = "tok_ponto_test"
        api.load_manifests()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MYPONTO", None)
        else:
            os.environ["RC_CONN_MYPONTO"] = self._saved

    @responses_lib.activate
    def test_accounts_follow_links_next_and_keep_bearer(self):
        page2_url = f"{ACCOUNTS}?page[after]=acc_1&page[limit]=100"
        responses_lib.add(responses_lib.GET, ACCOUNTS, json=_page([_account("acc_1", "BE111")], page2_url), status=200)
        responses_lib.add(responses_lib.GET, page2_url, json=_page([_account("acc_2", "BE222")]), status=200)

        m = api.MANIFESTS["myponto"]
        result = api.client(m, token_key="myponto").collect("/accounts", query={"page[limit]": "100"})

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual([it["id"] for it in result["items"]], ["acc_1", "acc_2"])
        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], "Bearer tok_ponto_test")
            self.assertEqual(call.request.headers["Accept"], "application/vnd.api+json")

    @responses_lib.activate
    def test_transactions_can_be_picked_for_support_context(self):
        tx = _transaction("tx_1", 250.75, "Customer NV")
        responses_lib.add(responses_lib.GET, TXNS, json=_page([tx]), status=200)

        m = api.MANIFESTS["myponto"]
        result = api.client(m, token_key="myponto").collect(
            "/accounts/acc_1/transactions",
            query={"from": "2026-06-01", "page[limit]": "100"},
        )

        self.assertFalse(result["incomplete"], result["reason"])
        picked = api.pick(result["items"][0], "id,attributes.amount,attributes.counterpartName,attributes.remittanceInformation")
        self.assertEqual(picked["id"], "tx_1")
        self.assertEqual(picked["attributes.amount"], 250.75)
        self.assertEqual(picked["attributes.counterpartName"], "Customer NV")
        self.assertEqual(picked["attributes.remittanceInformation"], "INV-2026-0042")

    @responses_lib.activate
    def test_cli_drives_manifest(self):
        responses_lib.add(responses_lib.GET, ACCOUNTS, json=_page([_account("acc_1", "BE111")]), status=200)

        rc = api._main([
            "get", "myponto", "/accounts",
            "--query", "page[limit]=100",
            "--paginate",
            "--pick", "id,attributes.reference,attributes.currentBalance",
        ])

        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], "Bearer tok_ponto_test")


if __name__ == "__main__":
    unittest.main()
