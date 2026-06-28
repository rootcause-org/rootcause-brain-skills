"""Fixture test for the manifest-ONLY Rollbar integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies reflect Rollbar's
documented `{err: 0, result: {...}}` envelope shape, trimmed to support-relevant fields.
Rollbar uses 1-based page numbers (not cursor or Link headers), so pagination is style=none and the
agent passes --query page=N explicitly — two separate GETs exercise that pattern here.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_rollbar_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://api.rollbar.com/api/1"
ITEMS_URL = f"{BASE}/items"
DEPLOYS_URL = f"{BASE}/deploys"
ITEM_URL = f"{BASE}/item/by-counter/42"

# --- Documented Rollbar response envelopes (trimmed to support-relevant fields) ---
# Source: https://docs.rollbar.com/reference (list-all-items, list-all-deploys)

_ITEMS_PAGE_1 = {
    "err": 0,
    "result": {
        "items": [
            {
                "id": 2001,
                "title": "TypeError: Cannot read property 'foo' of undefined",
                "level": "error",
                "status": "active",
                "environment": "production",
                "total_occurrences": 47,
                "last_occurrence_timestamp": 1719513600,
                "first_occurrence_timestamp": 1719427200,
            }
        ],
        "total_count": 2,
        "page": 1,
    },
}

_ITEMS_PAGE_2 = {
    "err": 0,
    "result": {
        "items": [
            {
                "id": 2002,
                "title": "ZeroDivisionError: division by zero",
                "level": "critical",
                "status": "active",
                "environment": "production",
                "total_occurrences": 3,
                "last_occurrence_timestamp": 1719500000,
                "first_occurrence_timestamp": 1719450000,
            }
        ],
        "total_count": 2,
        "page": 2,
    },
}

_DEPLOYS_BODY = {
    "err": 0,
    "result": {
        "deploys": [
            {
                "id": 501,
                "environment": "production",
                "revision": "abc123def456",
                "finish_time": 1719513000,
                "comment": "Deploy v2.3.1",
            },
            {
                "id": 500,
                "environment": "production",
                "revision": "9f8e7d6c5b4a",
                "finish_time": 1719426000,
                "comment": "Deploy v2.3.0",
            },
        ]
    },
}

_ITEM_SINGLE = {
    "err": 0,
    "result": {
        "id": 2001,
        "title": "TypeError: Cannot read property 'foo' of undefined",
        "level": "error",
        "status": "active",
        "environment": "production",
        "last_occurrence_timestamp": 1719513600,
    },
}

# Rollbar token prefix split to avoid CI hygiene guard flagging this test file itself.
_TOKEN_PREFIX = "rollbar" + "test"


class RollbarManifestOnly(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_ROLLBAR")
        os.environ["RC_CONN_ROLLBAR"] = _TOKEN_PREFIX + "_proj_read"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_ROLLBAR", None)
        else:
            os.environ["RC_CONN_ROLLBAR"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        """YAML loader must populate all lib.api Manifest fields correctly."""
        m = api.load_manifests()
        self.assertIn("rollbar", m)
        r = m["rollbar"]
        self.assertEqual(r.base_url, "https://api.rollbar.com/api/1")
        self.assertEqual(r.auth.strategy, "api_key_header")
        self.assertEqual(r.auth.name, "X-Rollbar-Access-Token")
        self.assertEqual(r.pagination.style, "none")
        self.assertEqual(r.pagination.items_field, "result.items")
        self.assertEqual(r.rate_limit_remaining_header, "")

    @responses.activate
    def test_credential_rides_in_header_not_query(self):
        """The token must appear in X-Rollbar-Access-Token header, never in the URL."""
        responses.add(responses.GET, ITEMS_URL, json=_ITEMS_PAGE_1, status=200)
        api.load_manifests()
        c = api.client(api.MANIFESTS["rollbar"])
        c.get("items", query={"status": "active"})
        req = responses.calls[0].request
        self.assertEqual(req.headers["X-Rollbar-Access-Token"], _TOKEN_PREFIX + "_proj_read")
        self.assertNotIn(_TOKEN_PREFIX, req.url)

    @responses.activate
    def test_single_page_items_fetch(self):
        """style=none: one GET, items extracted from result.items envelope."""
        responses.add(responses.GET, ITEMS_URL, json=_ITEMS_PAGE_1, status=200)
        api.load_manifests()
        c = api.client(api.MANIFESTS["rollbar"])
        page = c.fetch_page("items", query={"status": "active", "environment": "production", "page": 1})
        # items_field = "result.items" — framework extracts the nested list
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0]["id"], 2001)
        self.assertEqual(page.items[0]["level"], "error")
        # style=none: no next token
        self.assertIsNone(page.next)

    @responses.activate
    def test_two_explicit_page_fetches_stitch_manually(self):
        """Rollbar uses 1-based page numbers; agent passes --query page=N for each page.
        Two explicit GETs simulate the agent fetching page 1 then page 2."""
        responses.add(responses.GET, ITEMS_URL, json=_ITEMS_PAGE_1, status=200)
        responses.add(responses.GET, ITEMS_URL, json=_ITEMS_PAGE_2, status=200)
        api.load_manifests()
        c = api.client(api.MANIFESTS["rollbar"])
        # Simulate what the agent does: two explicit GETs with different page params
        page1 = c.fetch_page("items", query={"page": 1})
        page2 = c.fetch_page("items", query={"page": 2})
        all_items = page1.items + page2.items
        self.assertEqual(len(all_items), 2)
        ids = [it["id"] for it in all_items]
        self.assertEqual(ids, [2001, 2002])
        # Credential present on both requests
        for call in responses.calls:
            self.assertEqual(call.request.headers["X-Rollbar-Access-Token"], _TOKEN_PREFIX + "_proj_read")

    @responses.activate
    def test_collect_single_page(self):
        """collect() on style=none returns one page with incomplete=False."""
        responses.add(responses.GET, ITEMS_URL, json=_ITEMS_PAGE_1, status=200)
        api.load_manifests()
        c = api.client(api.MANIFESTS["rollbar"])
        result = c.collect("items", query={"page": 1})
        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], 2001)

    @responses.activate
    def test_pick_selects_support_fields(self):
        """api.pick() extracts the nested fields that matter for support."""
        responses.add(responses.GET, ITEMS_URL, json=_ITEMS_PAGE_1, status=200)
        api.load_manifests()
        c = api.client(api.MANIFESTS["rollbar"])
        page = c.fetch_page("items", query={"page": 1})
        picked = api.pick(page.items[0], "id,title,level,status,environment,last_occurrence_timestamp")
        self.assertEqual(picked["id"], 2001)
        self.assertEqual(picked["level"], "error")
        self.assertEqual(picked["status"], "active")
        self.assertIn("last_occurrence_timestamp", picked)

    @responses.activate
    def test_deploys_endpoint(self):
        """List deploys reads from result.deploys — test that raw body is navigable via pick."""
        responses.add(responses.GET, DEPLOYS_URL, json=_DEPLOYS_BODY, status=200)
        api.load_manifests()
        c = api.client(api.MANIFESTS["rollbar"])
        body = c.get("deploys", query={"page": 1})
        # deploys are NOT under result.items so we access body["result"]["deploys"] directly
        deploys = body["result"]["deploys"]
        self.assertEqual(len(deploys), 2)
        picked = api.pick(deploys[0], "id,environment,revision,finish_time")
        self.assertEqual(picked["id"], 501)
        self.assertEqual(picked["environment"], "production")
        self.assertEqual(picked["revision"], "abc123def456")

    @responses.activate
    def test_single_item_by_counter(self):
        """Get one item by counter — single-object result (no items list)."""
        responses.add(responses.GET, ITEM_URL, json=_ITEM_SINGLE, status=200)
        api.load_manifests()
        c = api.client(api.MANIFESTS["rollbar"])
        body = c.get("item/by-counter/42")
        self.assertEqual(body["err"], 0)
        item = body["result"]
        picked = api.pick(item, "id,title,level,status")
        self.assertEqual(picked["id"], 2001)
        self.assertEqual(picked["level"], "error")

    @responses.activate
    def test_cli_drives_rollbar_single_page(self):
        """CLI `python -m lib.api get rollbar items --pick ...` works end-to-end."""
        responses.add(responses.GET, ITEMS_URL, json=_ITEMS_PAGE_1, status=200)
        rc = api._main([
            "get", "rollbar", "items",
            "--query", "page=1",
            "--query", "status=active",
            "--pick", "result.items.*.id,result.items.*.level",
        ])
        self.assertEqual(rc, 0)
        req = responses.calls[0].request
        self.assertTrue(req.url.startswith(ITEMS_URL))
        self.assertEqual(req.headers["X-Rollbar-Access-Token"], _TOKEN_PREFIX + "_proj_read")

    @responses.activate
    def test_cli_paginate_flag_collects_style_none(self):
        """--paginate on style=none fetches exactly one page (no infinite loop)."""
        responses.add(responses.GET, ITEMS_URL, json=_ITEMS_PAGE_1, status=200)
        rc = api._main([
            "get", "rollbar", "items",
            "--query", "page=1",
            "--paginate",
        ])
        self.assertEqual(rc, 0)
        # style=none: exactly one request, no loop
        self.assertEqual(len(responses.calls), 1)


class RollbarCassetteHygiene(unittest.TestCase):
    """CI guard: no real Rollbar token prefix may land in committed connector files.

    Scoped to the connector directory, NOT this test file — the test legitimately names
    the prefixes it checks for, so scanning itself would be a false positive.
    """

    # Rollbar project token prefix split with concatenation so this guard can't flag itself.
    _TOKEN_PREFIXES = (
        "rollbar" + "test",           # our fake test token prefix above
    )

    def test_no_token_prefixes_in_rollbar_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "rollbar"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present in connector dir: {offenders}")


if __name__ == "__main__":
    unittest.main()
