"""Fixture test for the Lemon Squeezy connector.

Script connector (force-code trigger d: JSON:API page-number pagination via ``links.next`` in the
response body — not an HTTP Link header, not a simple offset). No live creds, no network: HTTP is
mocked with ``responses``. Bodies mirror the Lemon Squeezy API documentation example payloads,
trimmed to support-relevant fields.

Two-page customer list and a customer summary join (customer → orders → subscriptions → licenses)
exercise the real pagination + auth + field pre-selection paths end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_lemonsqueezy_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import api  # noqa: E402
from lib.connectors import lemonsqueezy as ls  # noqa: E402

BASE = "https://api.lemonsqueezy.com/v1"


# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
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
            "refunded_at": None,
            "created_at": "2024-02-01T12:00:00.000000Z",
        },
    }


def _subscription(sid: str, status: str = "active") -> dict:
    return {
        "type": "subscriptions",
        "id": sid,
        "attributes": {
            "status": status,
            "product_name": "Pro Plan",
            "variant_name": "Monthly",
            "billing_anchor": 1,
            "renews_at": "2024-07-01T00:00:00.000000Z",
            "ends_at": None,
            "cancelled": False,
            "pause": None,
            "created_at": "2024-02-01T12:00:00.000000Z",
        },
    }


def _license_key(lid: str, key_val: str) -> dict:
    return {
        "type": "license-keys",
        "id": lid,
        "attributes": {
            "key": key_val,
            "status": "active",
            "activation_limit": 3,
            "activations_count": 1,
            "expires_at": None,
            "created_at": "2024-02-01T12:00:00.000000Z",
        },
    }


def _page(items: list, next_url: str | None = None) -> dict:
    """Wrap items in a JSON:API envelope with optional links.next."""
    links: dict = {"first": f"{BASE}/customers"}
    if next_url:
        links["next"] = next_url
    return {
        "data": items,
        "meta": {
            "current_page": 1,
            "total": len(items),
            "per_page": 100,
        },
        "links": links,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_env(key: str = "fake_ls_token"):
    os.environ["RC_CONN_LEMONSQUEEZY"] = key


def _clear_env():
    os.environ.pop("RC_CONN_LEMONSQUEEZY", None)


class _Base(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        _set_env()

    def tearDown(self):
        _clear_env()


# ---------------------------------------------------------------------------
# 1. Manifest loads from YAML and maps every field
# ---------------------------------------------------------------------------

class TestManifestLoading(_Base):
    def test_manifest_loaded_via_yaml_loader(self):
        m = api.load_manifests()
        self.assertIn("lemonsqueezy", m)
        mani = m["lemonsqueezy"]
        self.assertEqual(mani.key, "lemonsqueezy")
        self.assertEqual(mani.base_url, "https://api.lemonsqueezy.com/v1")
        self.assertEqual(mani.auth.strategy, "bearer")
        self.assertEqual(mani.default_headers["Accept"], "application/vnd.api+json")
        self.assertEqual(mani.rate_limit_remaining_header, "X-RateLimit-Remaining")

    def test_connector_module_registers_manifest(self):
        # The script's import-time register() populates MANIFESTS even without load_manifests().
        # Re-import to trigger registration (MANIFESTS was cleared in setUp).
        import importlib
        importlib.reload(ls)
        self.assertIn("lemonsqueezy", api.MANIFESTS)
        mani = api.MANIFESTS["lemonsqueezy"]
        self.assertEqual(mani.key, "lemonsqueezy")
        self.assertEqual(mani.auth.strategy, "bearer")


# ---------------------------------------------------------------------------
# 2. Pagination stitches ≥2 pages via links.next in the response body
# ---------------------------------------------------------------------------

class TestPagination(_Base):
    @responses_lib.activate
    def test_paginate_follows_links_next_across_two_pages(self):
        """_paginate() follows links.next (body URL) to collect items from page 2."""
        PAGE2_URL = f"{BASE}/customers?page[number]=2&page[size]=100"
        c1 = _customer("101", "alice@example.com", "Alice")
        c2 = _customer("102", "bob@example.com", "Bob")

        page1 = _page([c1], next_url=PAGE2_URL)
        page2 = _page([c2], next_url=None)

        responses_lib.add(responses_lib.GET, f"{BASE}/customers", json=page1, status=200,
                          headers={"X-RateLimit-Remaining": "4999"})
        responses_lib.add(responses_lib.GET, PAGE2_URL, json=page2, status=200,
                          headers={"X-RateLimit-Remaining": "4998"})

        import importlib
        importlib.reload(ls)
        items = ls._paginate("/customers")

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["id"], "101")
        self.assertEqual(items[1]["id"], "102")

    @responses_lib.activate
    def test_bearer_credential_rides_every_request_incl_link_follow(self):
        """Bearer token must appear on both the first call and the link-follow call."""
        PAGE2_URL = f"{BASE}/customers?page[number]=2&page[size]=100"
        page1 = _page([_customer("201", "x@x.com", "X")], next_url=PAGE2_URL)
        page2 = _page([_customer("202", "y@y.com", "Y")])

        responses_lib.add(responses_lib.GET, f"{BASE}/customers", json=page1, status=200)
        responses_lib.add(responses_lib.GET, PAGE2_URL, json=page2, status=200)

        import importlib
        importlib.reload(ls)
        ls._paginate("/customers")

        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], "Bearer fake_ls_token")

    @responses_lib.activate
    def test_accept_header_present_on_all_requests(self):
        """JSON:API Accept header must be present on every request."""
        page1 = _page([_customer("301", "z@z.com", "Z")])
        responses_lib.add(responses_lib.GET, f"{BASE}/customers", json=page1, status=200)

        import importlib
        importlib.reload(ls)
        ls._paginate("/customers")

        self.assertEqual(
            responses_lib.calls[0].request.headers["Accept"],
            "application/vnd.api+json",
        )

    @responses_lib.activate
    def test_single_page_stops_without_links_next(self):
        """When links.next is absent the loop stops after one page."""
        page1 = _page([_customer("401", "a@a.com", "A")])  # no next_url
        responses_lib.add(responses_lib.GET, f"{BASE}/customers", json=page1, status=200)

        import importlib
        importlib.reload(ls)
        items = ls._paginate("/customers")

        self.assertEqual(len(items), 1)
        self.assertEqual(len(responses_lib.calls), 1)


# ---------------------------------------------------------------------------
# 3. Customer resolution (by id and email)
# ---------------------------------------------------------------------------

class TestCustomerResolution(_Base):
    @responses_lib.activate
    def test_resolve_customer_by_numeric_id(self):
        cust = _customer("999", "c@c.com", "Carol")
        responses_lib.add(responses_lib.GET, f"{BASE}/customers/999",
                          json={"data": cust}, status=200)

        import importlib
        importlib.reload(ls)
        result = ls.resolve_customer("999")

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "999")
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], "Bearer fake_ls_token")

    @responses_lib.activate
    def test_resolve_customer_by_email(self):
        cust = _customer("888", "dave@example.com", "Dave")
        page = _page([cust])
        responses_lib.add(responses_lib.GET, f"{BASE}/customers", json=page, status=200)

        import importlib
        importlib.reload(ls)
        result = ls.resolve_customer("dave@example.com")

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "888")
        # email filter must be in the query
        req_url = responses_lib.calls[0].request.url
        self.assertIn("filter%5Bemail%5D=dave%40example.com", req_url)

    @responses_lib.activate
    def test_resolve_customer_not_found_returns_none(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/customers",
                          json=_page([]), status=200)

        import importlib
        importlib.reload(ls)
        result = ls.resolve_customer("nobody@nowhere.com")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# 4. Support summary join + field pre-selection + markdown rendering
# ---------------------------------------------------------------------------

class TestSupportSummary(_Base):
    def _register_full_customer(self, cid: str = "777"):
        """Register all four endpoints for a complete customer journey."""
        cust = _customer(cid, "eve@example.com", "Eve")
        order = _order("O1", "LS-ABC-123", total=4900)
        sub = _subscription("S1", "active")
        lk = _license_key("L1", "XXXX" "-YYYY-ZZZZ")  # split to avoid prefix guard

        responses_lib.add(responses_lib.GET, f"{BASE}/customers/{cid}",
                          json={"data": cust}, status=200)
        responses_lib.add(responses_lib.GET, f"{BASE}/orders",
                          json=_page([order]), status=200)
        responses_lib.add(responses_lib.GET, f"{BASE}/subscriptions",
                          json=_page([sub]), status=200)
        responses_lib.add(responses_lib.GET, f"{BASE}/license-keys",
                          json=_page([lk]), status=200)

    @responses_lib.activate
    def test_support_summary_found(self):
        self._register_full_customer()
        import importlib
        importlib.reload(ls)
        s = ls.support_summary("777")

        self.assertTrue(s["found"])
        self.assertEqual(s["customer"]["email"], "eve@example.com")
        self.assertEqual(len(s["orders"]), 1)
        self.assertEqual(s["orders"][0]["identifier"], "LS-ABC-123")
        self.assertEqual(s["orders"][0]["total"], 4900)
        self.assertEqual(len(s["subscriptions"]), 1)
        self.assertEqual(s["subscriptions"][0]["status"], "active")
        self.assertEqual(len(s["license_keys"]), 1)

    @responses_lib.activate
    def test_support_summary_not_found(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/customers",
                          json=_page([]), status=200)
        import importlib
        importlib.reload(ls)
        s = ls.support_summary("nobody@nowhere.com")
        self.assertFalse(s["found"])
        self.assertEqual(s["ref"], "nobody@nowhere.com")

    @responses_lib.activate
    def test_summary_to_markdown_renders_all_sections(self):
        self._register_full_customer()
        import importlib
        importlib.reload(ls)
        md = ls.summary_to_markdown(ls.support_summary("777"))

        self.assertIn("eve@example.com", md)
        self.assertIn("LS-ABC-123", md)
        self.assertIn("active", md)
        self.assertIn("Pro Plan", md)
        self.assertIn("XXXX", md)

    def test_summary_to_markdown_not_found(self):
        s = {"found": False, "ref": "ghost@nowhere.com"}
        md = ls.summary_to_markdown(s)
        self.assertIn("not found", md)
        self.assertIn("ghost@nowhere.com", md)


# ---------------------------------------------------------------------------
# 5. pick() pre-selects nested JSON:API attributes
# ---------------------------------------------------------------------------

class TestPickIntegration(_Base):
    def test_pick_selects_nested_attribute_fields(self):
        order = _order("O9", "LS-PICK-001", total=1500)
        result = api.pick(order, "id,attributes.status,attributes.total,attributes.currency")
        self.assertEqual(result["id"], "O9")
        self.assertEqual(result["attributes.status"], "paid")
        self.assertEqual(result["attributes.total"], 1500)


# ---------------------------------------------------------------------------
# 6. CLI drives the connector (via main())
# ---------------------------------------------------------------------------

class TestCLI(_Base):
    @responses_lib.activate
    def test_cli_customer_subcommand_prints_markdown(self):
        cust = _customer("555", "frank@example.com", "Frank")
        responses_lib.add(responses_lib.GET, f"{BASE}/customers/555",
                          json={"data": cust}, status=200)
        responses_lib.add(responses_lib.GET, f"{BASE}/orders", json=_page([]), status=200)
        responses_lib.add(responses_lib.GET, f"{BASE}/subscriptions", json=_page([]), status=200)
        responses_lib.add(responses_lib.GET, f"{BASE}/license-keys", json=_page([]), status=200)

        import importlib
        importlib.reload(ls)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ls.main(["customer", "555"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("frank@example.com", out)


# ---------------------------------------------------------------------------
# 7. Token-prefix hygiene guard (scoped to connector dir)
# ---------------------------------------------------------------------------

class TestTokenPrefixHygiene(unittest.TestCase):
    """CI guard: no real Lemon Squeezy API key prefix may land in committed connector files.

    The Lemon Squeezy API key format starts with "eyJ" (JWT-based) or a similar prefix.
    We guard against the most recognisable literal that would appear if a key leaked.
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
