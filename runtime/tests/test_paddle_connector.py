"""Fixture tests for the Paddle integration (manifest-only, driven via lib.api).

Paddle is a manifest-only integration: there is no per-key Python connector. lib.api's `body_url`
pagination style follows the next-page URL embedded in the JSON body at ``meta.pagination.next``
(a full absolute URL) and stops when it is null. These tests drive the generic path:

  - the YAML manifest loads and maps every lib.api field (style=body_url, next_url_field,
    items_field, auth.strategy, base_url, page_size);
  - ``client(m).collect()`` stitches ≥2 fixture pages in order by following ``meta.pagination.next``;
  - the bearer credential rides EVERY request, including the continuation page;
  - ``api.pick`` selects the support-relevant fields;
  - token-prefix hygiene: no real Paddle API key prefix lands in the connector dir.

No live creds, no network. HTTP is mocked with ``responses``. Bodies mirror Paddle's documented
example payloads, trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_paddle_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API_BASE = "https://api.paddle.com"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# ---------------------------------------------------------------------------

_CUSTOMER_1 = {
    "id": "ctm_01h8441jn5pcwrfhwh78jqt8hk",
    "email": "alice@example.com",
    "name": "Alice Example",
    "status": "active",
    "created_at": "2024-01-15T10:00:00Z",
    "locale": "en",
    "marketing_consent": False,
}

_SUB_1 = {
    "id": "sub_01h9jj8h8bnmf9c58pjiyf9jd",
    "status": "active",
    "billing_cycle": {"frequency": 1, "interval": "month"},
    "next_billed_at": "2026-07-15T10:00:00Z",
    "paused_at": None,
    "canceled_at": None,
    "items": [
        {
            "price": {
                "description": "Pro Monthly",
                "unit_price": {"amount": "4900", "currency_code": "USD"},
            }
        }
    ],
}

_TXN_1 = {
    "id": "txn_01h9jj8h8bnmf9c58pjiyf9jd",
    "status": "completed",
    "billed_at": "2026-06-15T10:00:00Z",
    "created_at": "2026-06-15T09:58:00Z",
    "details": {
        "totals": {
            "total": "4900",
            "currency_code": "USD",
            "tax": "0",
        }
    },
    "billing_period": {"starts_at": "2026-06-15T00:00:00Z", "ends_at": "2026-07-15T00:00:00Z"},
}

# Page-2 customer (for pagination test)
_CUSTOMER_2 = {
    "id": "ctm_02aaaa",
    "email": "bob@example.com",
    "name": "Bob Example",
    "status": "active",
    "created_at": "2024-02-01T10:00:00Z",
    "locale": "en",
    "marketing_consent": False,
}


def _page(items: list, has_more: bool, next_url: str | None = None) -> dict:
    """Build a Paddle-shaped response envelope. body_url stops on a null `next`."""
    return {
        "data": items,
        "meta": {
            "request_id": "req_test_01",
            "pagination": {
                "per_page": 50,
                "next": next_url,
                "has_more": has_more,
                "estimated_total": len(items),
            },
        },
    }


class _PaddleBase(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.get("RC_CONN_PADDLE")
        # Fake token with a split prefix so the hygiene guard can't flag this file itself.
        os.environ["RC_CONN_PADDLE"] = "pdl_sdbx_" + "apikey_testfakekey0000000000_FakeSecret22_abc"
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        api.load_manifests()

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("RC_CONN_PADDLE", None)
        else:
            os.environ["RC_CONN_PADDLE"] = self._saved_env


# ---------------------------------------------------------------------------
# 1. Manifest loading
# ---------------------------------------------------------------------------

class TestPaddleManifest(_PaddleBase):
    def test_yaml_loads_and_maps_every_field(self):
        self.assertIn("paddle", api.MANIFESTS)
        p = api.MANIFESTS["paddle"]
        self.assertEqual(p.key, "paddle")
        self.assertEqual(p.base_url, "https://api.paddle.com")
        self.assertEqual(p.auth.strategy, "bearer")
        self.assertEqual(p.pagination.style, "body_url")
        self.assertEqual(p.pagination.next_url_field, "meta.pagination.next")
        self.assertEqual(p.pagination.items_field, "data")
        self.assertEqual(p.pagination.page_size, 50)
        self.assertEqual(p.rate_limit_remaining_header, "")  # no remaining-count header


# ---------------------------------------------------------------------------
# 2. body_url pagination: next URL embedded at meta.pagination.next (absolute)
# ---------------------------------------------------------------------------

class TestPaddlePagination(_PaddleBase):
    @responses_lib.activate
    def test_collect_stitches_two_pages_via_body_next_url(self):
        page2_url = f"{API_BASE}/customers?per_page=50&after=ctm_02aaaa"
        responses_lib.add(
            responses_lib.GET, f"{API_BASE}/customers",
            json=_page([_CUSTOMER_1], has_more=True, next_url=page2_url), status=200,
        )
        responses_lib.add(
            responses_lib.GET, page2_url,
            json=_page([_CUSTOMER_2], has_more=False, next_url=None), status=200,
        )

        m = api.MANIFESTS["paddle"]
        result = api.client(m, token_key="paddle").collect("/customers")

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, ["ctm_01h8441jn5pcwrfhwh78jqt8hk", "ctm_02aaaa"])  # in order
        self.assertEqual(len(responses_lib.calls), 2)
        self.assertIn("after=ctm_02aaaa", responses_lib.calls[1].request.url)

    @responses_lib.activate
    def test_bearer_credential_on_all_pages_including_continuation(self):
        page2_url = f"{API_BASE}/customers?per_page=50&after=ctm_02aaaa"
        responses_lib.add(
            responses_lib.GET, f"{API_BASE}/customers",
            json=_page([_CUSTOMER_1], has_more=True, next_url=page2_url), status=200,
        )
        responses_lib.add(
            responses_lib.GET, page2_url,
            json=_page([_CUSTOMER_2], has_more=False), status=200,
        )

        m = api.MANIFESTS["paddle"]
        api.client(m, token_key="paddle").collect("/customers")

        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(auth.startswith("Bearer "), f"Missing Bearer on {call.request.url}")
            self.assertIn("testfakekey", auth)

    @responses_lib.activate
    def test_single_page_no_continuation(self):
        """A null `next` on the first page stops pagination immediately (has_more ignored)."""
        responses_lib.add(
            responses_lib.GET, f"{API_BASE}/customers",
            json=_page([_CUSTOMER_1], has_more=False, next_url=None), status=200,
        )
        m = api.MANIFESTS["paddle"]
        result = api.client(m, token_key="paddle").collect("/customers")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_lib_api_cli_drives_manifest(self):
        """`python -m lib.api get paddle /customers --paginate` works end-to-end."""
        page2_url = f"{API_BASE}/customers?after=ctm_02aaaa"
        responses_lib.add(
            responses_lib.GET, f"{API_BASE}/customers",
            json=_page([_CUSTOMER_1], has_more=True, next_url=page2_url), status=200,
        )
        responses_lib.add(
            responses_lib.GET, page2_url,
            json=_page([_CUSTOMER_2], has_more=False), status=200,
        )
        rc = api._main(["get", "paddle", "/customers", "--paginate", "--pick", "id,email,status"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertTrue(call.request.headers.get("Authorization", "").startswith("Bearer "))


# ---------------------------------------------------------------------------
# 3. api.pick on Paddle fields
# ---------------------------------------------------------------------------

class TestPaddlePick(_PaddleBase):
    def test_pick_selects_support_fields_from_customer(self):
        picked = api.pick(_CUSTOMER_1, "id,email,name,status")
        self.assertEqual(picked["id"], "ctm_01h8441jn5pcwrfhwh78jqt8hk")
        self.assertEqual(picked["email"], "alice@example.com")
        self.assertEqual(picked["status"], "active")

    def test_pick_nested_subscription_fields(self):
        picked = api.pick(_SUB_1, "id,status,next_billed_at,items.*.price.unit_price.amount")
        self.assertEqual(picked["id"], "sub_01h9jj8h8bnmf9c58pjiyf9jd")
        self.assertEqual(picked["status"], "active")
        self.assertEqual(picked["items.*.price.unit_price.amount"], ["4900"])

    def test_pick_transaction_totals(self):
        picked = api.pick(_TXN_1, "id,status,billed_at,details.totals.total,details.totals.currency_code")
        self.assertEqual(picked["id"], "txn_01h9jj8h8bnmf9c58pjiyf9jd")
        self.assertEqual(picked["details.totals.total"], "4900")
        self.assertEqual(picked["details.totals.currency_code"], "USD")


# ---------------------------------------------------------------------------
# 4. Token-prefix hygiene
# ---------------------------------------------------------------------------

class TestPaddleHygiene(unittest.TestCase):
    """CI guard: no real Paddle API key prefix may land in the connector files (only manifest.yaml).

    Scoped to the connector dir, NOT this test file — the test legitimately names the prefix it
    hunts for, so scanning itself would be a false positive.
    """

    # Split the prefix literal so the guard can't flag this file itself.
    _TOKEN_PREFIXES = ("pdl_live_" "apikey",)  # concatenated string literal

    def test_no_token_prefixes_in_paddle_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "paddle"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix in (".pyc",):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref}")
        self.assertEqual(offenders, [], f"real token prefix found in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
