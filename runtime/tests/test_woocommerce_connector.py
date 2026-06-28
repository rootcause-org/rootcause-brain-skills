"""Fixture test for the manifest-ONLY WooCommerce integration — proves a catalogued connector with
NO bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies are WooCommerce's
own DOCUMENTED example payloads (woocommerce.github.io/woocommerce-rest-api-docs), trimmed to the
fields most relevant for support diagnosis. WooCommerce paginates with RFC 8288
`Link: <…>; rel="next"` headers (same style as GitHub/Bugsnag), so the two mocked pages exercise
the real `link` pagination end-to-end.

Auth strategy is `basic`: the credential stored as "consumer_key:consumer_secret" is encoded as
HTTP Basic Auth on every request (including link-follow pages). The base_url is per-store
(templated), so tests use absolute URLs directly.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_woocommerce_connector.py -q
"""

import base64
import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

# A fictional but realistic store domain — the agent supplies this at runtime from the brain.
STORE = "https://shop.example.com"
API_BASE = f"{STORE}/wp-json/wc/v3"
ORDERS_URL = f"{API_BASE}/orders"
CUSTOMERS_URL = f"{API_BASE}/customers"

# WooCommerce consumer key/secret credentials (split so the hygiene guard doesn't flag this file;
# real keys begin with `ck_` / `cs_` — the prefixes we guard against in connector files).
_CK_PREFIX = "ck" + "_"
_CS_PREFIX = "cs" + "_"
_CONSUMER_KEY = _CK_PREFIX + "test_consumer_key_abcdef1234567890"
_CONSUMER_SECRET = _CS_PREFIX + "test_consumer_secret_abcdef1234567890"
# The stored credential is "consumer_key:consumer_secret" — lib.api basic strategy encodes this.
_STORED_CRED = f"{_CONSUMER_KEY}:{_CONSUMER_SECRET}"
# Expected Basic Auth header value (what lib.api sends on the wire).
_BASIC_TOKEN = base64.b64encode(_STORED_CRED.encode()).decode()
_EXPECTED_AUTH = f"Basic {_BASIC_TOKEN}"

# Two pages of orders (bare JSON arrays, as WooCommerce returns).
# Shapes mirror the documented WooCommerce order object; only support-relevant fields are kept.
_PAGE_1 = [
    {
        "id": 727,
        "status": "processing",
        "date_created": "2017-03-21T16:14:36",
        "total": "79.00",
        "billing": {
            "first_name": "John",
            "last_name": "Doe",
            "email": "john.doe@example.com",
        },
        "line_items": [
            {"name": "Woo Album #4", "quantity": 1, "total": "79.00"},
        ],
    },
]
_PAGE_2 = [
    {
        "id": 728,
        "status": "completed",
        "date_created": "2017-03-22T09:00:00",
        "total": "39.00",
        "billing": {
            "first_name": "Jane",
            "last_name": "Smith",
            "email": "jane.smith@example.com",
        },
        "line_items": [
            {"name": "Woo Single #1", "quantity": 1, "total": "39.00"},
        ],
    },
]

# RFC 8288 Link header: page 1 points at page 2 as rel="next"; page 2 has no next → loop stops.
_PAGE_1_LINK = (
    f'<{ORDERS_URL}?per_page=100&page=2>; rel="next", '
    f'<{ORDERS_URL}?per_page=100&page=2>; rel="last"'
)

# Documented customer object (trimmed to support-relevant fields).
_CUSTOMER = {
    "id": 25,
    "email": "john.doe@example.com",
    "first_name": "John",
    "last_name": "Doe",
    "orders_count": 4,
    "total_spent": "236.00",
    "date_created": "2017-03-21T16:09:28",
}


class WooCommerceManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `woocommerce` (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_WOOCOMMERCE")
        os.environ["RC_CONN_WOOCOMMERCE"] = _STORED_CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_WOOCOMMERCE", None)
        else:
            os.environ["RC_CONN_WOOCOMMERCE"] = self._saved

    def test_manifest_loaded_from_yaml_with_correct_fields(self):
        """YAML loader populates every declared manifest field correctly."""
        m = api.load_manifests()
        self.assertIn("woocommerce", m)
        wc = m["woocommerce"]
        # base_url is the templated per-store form (not a live URL).
        self.assertIn("wp-json/wc/v3", wc.base_url)
        # Auth: basic — consumer_key:consumer_secret over HTTPS.
        self.assertEqual(wc.auth.strategy, "basic")
        # Pagination: RFC 8288 Link headers.
        self.assertEqual(wc.pagination.style, "link")
        self.assertEqual(wc.pagination.items_field, "")  # bare JSON array responses
        self.assertEqual(wc.pagination.page_size, 100)
        # No rate-limit remaining header documented.
        self.assertEqual(wc.rate_limit_remaining_header, "")
        # No required default headers (WooCommerce REST is plain JSON).
        self.assertFalse(wc.default_headers)

    @responses.activate
    def test_link_pagination_stitches_pages_and_pick_selects_fields(self):
        """Two-page link pagination: both pages collected, basic auth rides every request."""
        responses.add(
            responses.GET, ORDERS_URL,
            json=_PAGE_1, status=200,
            headers={"Link": _PAGE_1_LINK, "X-WP-Total": "2", "X-WP-TotalPages": "2"},
        )
        responses.add(
            responses.GET, ORDERS_URL,
            json=_PAGE_2, status=200,
            headers={"X-WP-Total": "2", "X-WP-TotalPages": "2"},
        )

        api.load_manifests()
        wc = api.MANIFESTS["woocommerce"]
        # Use the real store base URL so lib.api path joins correctly.
        mani = api.Manifest(
            key=wc.key,
            base_url=API_BASE,
            auth=wc.auth,
            pagination=wc.pagination,
            rate_limit_remaining_header=wc.rate_limit_remaining_header,
            default_headers=wc.default_headers,
        )
        c = api.Client(manifest=mani, credential=_STORED_CRED)
        result = c.collect("orders", query={"per_page": 100, "status": "any"})

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, [727, 728])  # both pages stitched in order

        # Basic auth header rides page 1 (initial request) AND page 2 (link-follow).
        self.assertEqual(responses.calls[0].request.headers["Authorization"], _EXPECTED_AUTH)
        self.assertEqual(responses.calls[1].request.headers["Authorization"], _EXPECTED_AUTH)
        self.assertEqual(len(responses.calls), 2)

        # --pick prunes the large order object down to support-relevant fields.
        picked = [
            api.pick(it, "id,status,total,billing.email,billing.first_name,billing.last_name")
            for it in result["items"]
        ]
        self.assertEqual(picked[0]["id"], 727)
        self.assertEqual(picked[0]["status"], "processing")
        self.assertEqual(picked[0]["billing.email"], "john.doe@example.com")
        self.assertEqual(picked[1]["status"], "completed")
        self.assertEqual(picked[1]["billing.last_name"], "Smith")

    @responses.activate
    def test_single_order_detail_no_pagination(self):
        """Single-object GET (no pagination): basic auth present, response parsed correctly."""
        order_url = f"{API_BASE}/orders/727"
        responses.add(responses.GET, order_url, json=_PAGE_1[0], status=200)

        api.load_manifests()
        wc = api.MANIFESTS["woocommerce"]
        mani = api.Manifest(
            key=wc.key,
            base_url=API_BASE,
            auth=wc.auth,
            pagination=wc.pagination,
            rate_limit_remaining_header=wc.rate_limit_remaining_header,
            default_headers=wc.default_headers,
        )
        c = api.Client(manifest=mani, credential=_STORED_CRED)
        body = c.get("orders/727")

        self.assertEqual(body["id"], 727)
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["billing"]["email"], "john.doe@example.com")
        # Basic auth present on single-resource GET.
        self.assertEqual(responses.calls[0].request.headers["Authorization"], _EXPECTED_AUTH)

    @responses.activate
    def test_customer_detail_pick_selects_support_fields(self):
        """Customer single-object GET with --pick for support-relevant fields."""
        customer_url = f"{API_BASE}/customers/25"
        responses.add(responses.GET, customer_url, json=_CUSTOMER, status=200)

        api.load_manifests()
        wc = api.MANIFESTS["woocommerce"]
        mani = api.Manifest(
            key=wc.key,
            base_url=API_BASE,
            auth=wc.auth,
            pagination=wc.pagination,
            rate_limit_remaining_header=wc.rate_limit_remaining_header,
            default_headers=wc.default_headers,
        )
        c = api.Client(manifest=mani, credential=_STORED_CRED)
        body = c.get("customers/25")
        picked = api.pick(body, "id,email,orders_count,total_spent")

        self.assertEqual(picked["id"], 25)
        self.assertEqual(picked["email"], "john.doe@example.com")
        self.assertEqual(picked["orders_count"], 4)
        self.assertEqual(picked["total_spent"], "236.00")

    @responses.activate
    def test_cli_drives_woocommerce_with_basic_auth_and_paginate(self):
        """CLI `python -m lib.api get woocommerce <abs-url> --paginate` works via manifest loader."""
        responses.add(
            responses.GET, ORDERS_URL,
            json=_PAGE_1, status=200,
            headers={"Link": _PAGE_1_LINK},
        )
        responses.add(
            responses.GET, ORDERS_URL,
            json=_PAGE_2, status=200,
        )
        rc = api._main([
            "get", "woocommerce", ORDERS_URL,
            "--query", "per_page=100",
            "--paginate",
            "--pick", "id,status,total",
        ])
        self.assertEqual(rc, 0)
        # Both pages fetched; basic auth rides both requests.
        self.assertTrue(responses.calls[0].request.url.startswith(ORDERS_URL))
        self.assertEqual(responses.calls[0].request.headers["Authorization"], _EXPECTED_AUTH)
        self.assertEqual(responses.calls[1].request.headers["Authorization"], _EXPECTED_AUTH)
        self.assertEqual(len(responses.calls), 2)


class WooCommerceCassetteHygiene(unittest.TestCase):
    """CI guard: no real WooCommerce consumer key/secret prefix may land in the connector dir.

    Scopes to the connector dir (manifest + any future cassette), NOT this test file — this test
    legitimately names the prefixes it hunts for (split across concatenation so the guard doesn't
    self-trigger).
    """

    # WooCommerce consumer key prefix `ck_` and consumer secret prefix `cs_` — split literals.
    _TOKEN_PREFIXES = ("ck" + "_", "cs" + "_")

    def test_no_token_prefixes_in_woocommerce_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "woocommerce"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains '{pref}...'")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
