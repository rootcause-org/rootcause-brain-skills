"""Fixture test for the manifest-ONLY Chargebee integration — proves a catalogued connector with
NO bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. Bodies mirror Chargebee's documented
example payloads (apidocs.chargebee.com/docs/api), trimmed to support-relevant fields.

Chargebee paginates via `next_offset` (opaque cursor in the response body), sent back as the
`offset` query param — this exercises lib.api's `cursor` pagination style end-to-end.
List responses wrap items under `"list"` with each item typed as `{"customer": {...}}`.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_chargebee_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

SITE = "acme-test"
BASE = f"https://{SITE}.chargebee.com/api/v2"
CUSTOMERS_URL = f"{BASE}/customers"
SUBSCRIPTIONS_URL = f"{BASE}/subscriptions"

# ---------------------------------------------------------------------------
# Documented example payloads (Chargebee API docs), trimmed to relevant fields.
# List envelope: {"list": [{"customer": {...}}, ...], "next_offset": "…"}
# ---------------------------------------------------------------------------

_PAGE_1 = {
    "list": [
        {
            "customer": {
                "id": "cust_Az4tHdVVAq12Dk",
                "first_name": "Jane",
                "last_name": "Doe",
                "email": "jane.doe@example.com",
                "billing_address": {
                    "city": "San Francisco",
                    "country": "US",
                },
            }
        }
    ],
    "next_offset": "1693000000000",  # opaque cursor — more results exist
}

_PAGE_2 = {
    "list": [
        {
            "customer": {
                "id": "cust_Bq7uIeWWBr34El",
                "first_name": "John",
                "last_name": "Smith",
                "email": "john.smith@example.com",
                "billing_address": {
                    "city": "London",
                    "country": "GB",
                },
            }
        }
    ]
    # no next_offset → cursor stops here
}

_SUBSCRIPTION_PAGE = {
    "list": [
        {
            "subscription": {
                "id": "sub_Az4tHdVVAq12Dk",
                "customer_id": "cust_Az4tHdVVAq12Dk",
                "status": "active",
                "plan_id": "premium-monthly",
                "next_billing_at": 1693000000,
                "current_term_end": 1693000000,
            }
        }
    ]
}


class ChargebeeManifestOnly(unittest.TestCase):
    def setUp(self):
        # Fresh registry so YAML loader is the only source for `chargebee`.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_CHARGEBEE")
        # Split so the token-prefix hygiene guard (which scans THIS file) does not flag itself.
        os.environ["RC_CONN_CHARGEBEE"] = "test_" + "cbkey_live_abc123"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_CHARGEBEE", None)
        else:
            os.environ["RC_CONN_CHARGEBEE"] = self._saved

    # ------------------------------------------------------------------
    # 1. YAML loads and maps every manifest field correctly
    # ------------------------------------------------------------------

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("chargebee", m)
        cb = m["chargebee"]
        self.assertIn("{site}.chargebee.com", cb.base_url)
        self.assertEqual(cb.auth.strategy, "basic")
        self.assertEqual(cb.pagination.style, "cursor")
        self.assertEqual(cb.pagination.cursor_field, "next_offset")
        self.assertEqual(cb.pagination.cursor_param, "offset")
        self.assertEqual(cb.pagination.has_more_field, "")
        self.assertEqual(cb.pagination.items_field, "list")
        self.assertEqual(cb.pagination.page_size, 100)
        self.assertEqual(cb.rate_limit_remaining_header, "")

    # ------------------------------------------------------------------
    # 2. Cursor pagination stitches ≥2 pages via next_offset
    # ------------------------------------------------------------------

    @responses_lib.activate
    def test_cursor_pagination_stitches_two_pages(self):
        # Page 1: contains next_offset → lib.api sends it back as `offset` for page 2.
        # Page 2: no next_offset → loop terminates.
        responses_lib.add(
            responses_lib.GET, CUSTOMERS_URL, json=_PAGE_1, status=200
        )
        responses_lib.add(
            responses_lib.GET, CUSTOMERS_URL, json=_PAGE_2, status=200
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["chargebee"])
        result = c.collect(CUSTOMERS_URL, query={"limit": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)
        # Items are the raw wrapped objects from the `list` array.
        ids = [it["customer"]["id"] for it in result["items"]]
        self.assertIn("cust_Az4tHdVVAq12Dk", ids)
        self.assertIn("cust_Bq7uIeWWBr34El", ids)

        # Page 2 request must carry offset=<next_offset value from page 1>.
        page2_req = responses_lib.calls[1].request
        self.assertIn("offset=1693000000000", page2_req.url)

    # ------------------------------------------------------------------
    # 3. Basic auth credential rides every request (incl. page 2)
    # ------------------------------------------------------------------

    @responses_lib.activate
    def test_basic_auth_on_every_request(self):
        import base64

        responses_lib.add(
            responses_lib.GET, CUSTOMERS_URL, json=_PAGE_1, status=200
        )
        responses_lib.add(
            responses_lib.GET, CUSTOMERS_URL, json=_PAGE_2, status=200
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["chargebee"])
        c.collect(CUSTOMERS_URL, query={"limit": 100})

        for call in responses_lib.calls:
            auth_header = call.request.headers.get("Authorization", "")
            self.assertTrue(
                auth_header.startswith("Basic "),
                f"Expected Basic auth on all calls, got: {auth_header!r}",
            )
            # Decode and verify key is the username, password empty.
            encoded = auth_header[len("Basic "):]
            decoded = base64.b64decode(encoded).decode()
            self.assertTrue(
                decoded.endswith(":"),
                f"Expected 'key:' Basic encoding, got: {decoded!r}",
            )

    # ------------------------------------------------------------------
    # 4. api.pick selects support-relevant fields from wrapped items
    # ------------------------------------------------------------------

    def test_pick_selects_nested_fields(self):
        item = _PAGE_1["list"][0]
        selected = api.pick(item, "customer.id,customer.email,customer.billing_address")
        self.assertEqual(selected["customer.id"], "cust_Az4tHdVVAq12Dk")
        self.assertEqual(selected["customer.email"], "jane.doe@example.com")
        self.assertEqual(selected["customer.billing_address"]["city"], "San Francisco")

    # ------------------------------------------------------------------
    # 5. Single-page (no next_offset) works correctly
    # ------------------------------------------------------------------

    @responses_lib.activate
    def test_single_page_subscription_list(self):
        responses_lib.add(
            responses_lib.GET, SUBSCRIPTIONS_URL, json=_SUBSCRIPTION_PAGE, status=200
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["chargebee"])
        result = c.collect(SUBSCRIPTIONS_URL)

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 1)
        sub = result["items"][0]["subscription"]
        self.assertEqual(sub["status"], "active")
        self.assertEqual(sub["plan_id"], "premium-monthly")

    # ------------------------------------------------------------------
    # 6. CLI drive works end-to-end
    # ------------------------------------------------------------------

    @responses_lib.activate
    def test_cli_drives_chargebee_paginate(self):
        responses_lib.add(
            responses_lib.GET, CUSTOMERS_URL, json=_PAGE_1, status=200
        )
        responses_lib.add(
            responses_lib.GET, CUSTOMERS_URL, json=_PAGE_2, status=200
        )

        rc = api._main([
            "get", "chargebee", CUSTOMERS_URL,
            "--query", "limit=100",
            "--paginate",
            "--pick", "customer.id,customer.email",
        ])
        self.assertEqual(rc, 0)
        # Two pages were fetched; auth was set on each.
        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertIn("Basic ", call.request.headers.get("Authorization", ""))


class ChargebeeTokenHygiene(unittest.TestCase):
    """CI guard: no real Chargebee API key prefix may land in committed connector files.

    Scopes to the connector dir only — this test file legitimately names the prefix it hunts
    for (split across string concatenation) so scanning itself would produce a false positive.
    """

    # Chargebee live key prefix: split so the guard doesn't flag THIS source file.
    _TOKEN_PREFIXES = ("cbkey_live" "_",)

    def test_no_token_prefixes_in_chargebee_files(self):
        connector_dir = (
            Path(__file__).resolve().parents[1] / "lib" / "connectors" / "chargebee"
        )
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
