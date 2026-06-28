"""Fixture test for the Lemon Squeezy integration (manifest-only, driven via lib.api).

Lemon Squeezy is a manifest-only integration: there is no per-key Python connector. It is a
JSON:API store whose list responses carry the next-page URL in the body at ``links.next`` (a full
absolute URL), so lib.api's ``body_url`` pagination style drives the loop. These tests drive the
generic path:

  - the YAML manifest loads and maps every lib.api field (style=body_url, next_url_field,
    items_field, auth.strategy, base_url, page_size, the JSON:API Accept header);
  - ``client(m).collect()`` stitches ≥2 fixture pages in order by following ``links.next``;
  - the bearer credential AND the Accept header ride EVERY request, including the continuation;
  - ``api.pick`` selects nested JSON:API attribute fields;
  - token-prefix hygiene: no real Lemon Squeezy key prefix lands in the connector dir.

No live creds, no network: HTTP is mocked with ``responses``. Bodies mirror the Lemon Squeezy API
documentation example payloads, trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_lemonsqueezy_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import api  # noqa: E402

BASE = "https://api.lemonsqueezy.com/v1"


# ---------------------------------------------------------------------------
# Documented example payloads (JSON:API shape, trimmed to support-relevant fields)
# ---------------------------------------------------------------------------

def _customer(cid: str, email: str, name: str) -> dict:
    return {
        "type": "customers",
        "id": cid,
        "attributes": {
            "name": name,
            "email": email,
            "status": "subscribed",
            "total_revenue_currency": "USD",
            "created_at": "2024-01-15T10:00:00.000000Z",
        },
        "links": {"self": f"{BASE}/customers/{cid}"},
    }


def _order(oid: str, identifier: str, total: int = 2900) -> dict:
    return {
        "type": "orders",
        "id": oid,
        "attributes": {
            "identifier": identifier,
            "status": "paid",
            "total": total,
            "currency": "USD",
            "refunded": False,
            "created_at": "2024-02-01T12:00:00.000000Z",
        },
    }


def _page(items: list, next_url: str | None = None) -> dict:
    """Wrap items in a JSON:API envelope with optional links.next (body_url drives off this)."""
    links: dict = {"first": f"{BASE}/customers"}
    if next_url:
        links["next"] = next_url
    return {
        "data": items,
        "meta": {"current_page": 1, "total": len(items), "per_page": 100},
        "links": links,
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_LEMONSQUEEZY")
        os.environ["RC_CONN_LEMONSQUEEZY"] = "fake_ls_token"
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        api.load_manifests()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_LEMONSQUEEZY", None)
        else:
            os.environ["RC_CONN_LEMONSQUEEZY"] = self._saved


# ---------------------------------------------------------------------------
# 1. Manifest loads from YAML and maps every field
# ---------------------------------------------------------------------------

class TestManifestLoading(_Base):
    def test_manifest_loaded_via_yaml_loader(self):
        self.assertIn("lemonsqueezy", api.MANIFESTS)
        m = api.MANIFESTS["lemonsqueezy"]
        self.assertEqual(m.key, "lemonsqueezy")
        self.assertEqual(m.base_url, "https://api.lemonsqueezy.com/v1")
        self.assertEqual(m.auth.strategy, "bearer")
        self.assertEqual(m.pagination.style, "body_url")
        self.assertEqual(m.pagination.next_url_field, "links.next")
        self.assertEqual(m.pagination.items_field, "data")
        self.assertEqual(m.pagination.page_size, 100)
        self.assertEqual(m.default_headers["Accept"], "application/vnd.api+json")
        self.assertEqual(m.rate_limit_remaining_header, "X-RateLimit-Remaining")


# ---------------------------------------------------------------------------
# 2. body_url pagination stitches ≥2 pages via links.next
# ---------------------------------------------------------------------------

class TestPagination(_Base):
    @responses_lib.activate
    def test_collect_follows_links_next_across_two_pages(self):
        page2_url = f"{BASE}/customers?page[number]=2&page[size]=100"
        c1 = _customer("101", "alice@example.com", "Alice")
        c2 = _customer("102", "bob@example.com", "Bob")
        responses_lib.add(responses_lib.GET, f"{BASE}/customers",
                          json=_page([c1], next_url=page2_url), status=200,
                          headers={"X-RateLimit-Remaining": "4999"})
        responses_lib.add(responses_lib.GET, page2_url,
                          json=_page([c2], next_url=None), status=200,
                          headers={"X-RateLimit-Remaining": "4998"})

        m = api.MANIFESTS["lemonsqueezy"]
        result = api.client(m, token_key="lemonsqueezy").collect("/customers")

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual([it["id"] for it in result["items"]], ["101", "102"])  # in order
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_bearer_and_accept_headers_ride_every_request(self):
        page2_url = f"{BASE}/customers?page[number]=2&page[size]=100"
        responses_lib.add(responses_lib.GET, f"{BASE}/customers",
                          json=_page([_customer("201", "x@x.com", "X")], next_url=page2_url), status=200)
        responses_lib.add(responses_lib.GET, page2_url,
                          json=_page([_customer("202", "y@y.com", "Y")]), status=200)

        m = api.MANIFESTS["lemonsqueezy"]
        api.client(m, token_key="lemonsqueezy").collect("/customers")

        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], "Bearer fake_ls_token")
            self.assertEqual(call.request.headers["Accept"], "application/vnd.api+json")

    @responses_lib.activate
    def test_single_page_stops_without_links_next(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/customers",
                          json=_page([_customer("401", "a@a.com", "A")]), status=200)
        m = api.MANIFESTS["lemonsqueezy"]
        result = api.client(m, token_key="lemonsqueezy").collect("/customers")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_lib_api_cli_drives_manifest(self):
        """`python -m lib.api get lemonsqueezy /orders --paginate` works end-to-end."""
        page2_url = f"{BASE}/orders?page[number]=2"
        responses_lib.add(responses_lib.GET, f"{BASE}/orders",
                          json=_page([_order("O1", "LS-A")], next_url=page2_url), status=200)
        responses_lib.add(responses_lib.GET, page2_url,
                          json=_page([_order("O2", "LS-B")]), status=200)

        rc = api._main(["get", "lemonsqueezy", "/orders", "--paginate",
                        "--pick", "id,attributes.status,attributes.total"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], "Bearer fake_ls_token")
            self.assertEqual(call.request.headers["Accept"], "application/vnd.api+json")


# ---------------------------------------------------------------------------
# 3. api.pick pre-selects nested JSON:API attributes
# ---------------------------------------------------------------------------

class TestPickIntegration(_Base):
    def test_pick_selects_nested_attribute_fields(self):
        order = _order("O9", "LS-PICK-001", total=1500)
        result = api.pick(order, "id,attributes.status,attributes.total,attributes.currency")
        self.assertEqual(result["id"], "O9")
        self.assertEqual(result["attributes.status"], "paid")
        self.assertEqual(result["attributes.total"], 1500)
        self.assertEqual(result["attributes.currency"], "USD")


# ---------------------------------------------------------------------------
# 4. Token-prefix hygiene guard (scoped to connector dir)
# ---------------------------------------------------------------------------

class TestTokenPrefixHygiene(unittest.TestCase):
    """CI guard: no real Lemon Squeezy API key prefix may land in connector files (only manifest.yaml).

    Prefixes are split with string concatenation so this file doesn't flag itself.
    """

    _TOKEN_PREFIXES = ("eyJ" "1c",)  # LS JWT prefix split to avoid false positive on this file

    def test_no_token_prefixes_in_lemonsqueezy_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "lemonsqueezy"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
