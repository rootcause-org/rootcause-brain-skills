"""Fixture tests for the Better Stack integration — manifest-only, driven via the generic lib.api path.

There is no Better Stack Python module anymore: the integration is the manifest row. lib.api's
``body_url`` pagination style follows ``pagination.next`` (a body-embedded ABSOLUTE URL, null when
exhausted) all by itself.

Tests cover:
  - YAML loads via lib.api's manifest loader and maps every runtime field (body_url style,
    next_url_field, items_field, page_size, bearer auth, base_url).
  - body_url pagination stitches ≥2 pages by following pagination.next, stopping when next is null.
  - The bearer credential rides EVERY request including continuation pages.
  - api.pick selects support-relevant fields from JSON:API shaped objects.
  - The generic CLI (`python -m lib.api get betterstack … --paginate`) drives it end-to-end.
  - Token-prefix hygiene: no real Better Stack API token prefix lands in the connector dir.

No live creds, no network. HTTP is mocked with ``responses``.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_betterstack_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API_BASE = "https://uptime.betterstack.com/api/v2"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# JSON:API shape: { "id": "…", "type": "…", "attributes": { … } }
# ---------------------------------------------------------------------------

_MONITOR_1 = {
    "id": "2",
    "type": "monitor",
    "attributes": {
        "url": "https://uptime.betterstack.com",
        "pronounceable_name": "Uptime homepage",
        "monitor_type": "keyword",
        "status": "up",
        "last_checked_at": "2020-09-01T14:17:46.000Z",
        "check_frequency": 30,
        "paused_at": None,
    },
}

_MONITOR_2 = {
    "id": "3",
    "type": "monitor",
    "attributes": {
        "url": "https://example.com/api/health",
        "pronounceable_name": "API health check",
        "monitor_type": "status",
        "status": "down",
        "last_checked_at": "2020-09-02T10:00:00.000Z",
        "check_frequency": 60,
        "paused_at": None,
    },
}

_INCIDENT_1 = {
    "id": "25",
    "type": "incident",
    "attributes": {
        "name": "uptime homepage",
        "url": "https://uptime.betterstack.com/",
        "cause": "Status 404",
        "started_at": "2020-03-09T17:37:56.662Z",
        "acknowledged_at": None,
        "resolved_at": None,
        "status": "Started",
    },
}


def _page(items: list, next_url: str | None = None) -> dict:
    """Build a Better Stack list response envelope with pagination."""
    return {
        "data": items,
        "pagination": {
            "first": f"{API_BASE}/monitors?page=1",
            "last": f"{API_BASE}/monitors?page=2",
            "prev": None,
            "next": next_url,
        },
    }


class _BetterStackBase(unittest.TestCase):
    def setUp(self):
        # YAML loader is the sole source of truth each test.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved_env = os.environ.get("RC_CONN_BETTERSTACK")
        # Split the prefix so the hygiene guard can't flag this file itself.
        os.environ["RC_CONN_BETTERSTACK"] = "bt_" + "test_fakesecrettoken0000000_abcdef"

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("RC_CONN_BETTERSTACK", None)
        else:
            os.environ["RC_CONN_BETTERSTACK"] = self._saved_env


# ---------------------------------------------------------------------------
# 1. Manifest loading
# ---------------------------------------------------------------------------

class TestBetterStackManifest(_BetterStackBase):
    def test_yaml_loads_and_maps_every_field(self):
        m = api.load_manifests()
        self.assertIn("betterstack", m)
        b = m["betterstack"]
        self.assertEqual(b.key, "betterstack")
        self.assertEqual(b.base_url, "https://uptime.betterstack.com/api/v2")
        self.assertEqual(b.auth.strategy, "bearer")
        self.assertEqual(b.pagination.style, "body_url")
        self.assertEqual(b.pagination.next_url_field, "pagination.next")
        self.assertEqual(b.pagination.items_field, "data")
        self.assertEqual(b.pagination.page_size, 50)
        self.assertEqual(b.rate_limit_remaining_header, "")


# ---------------------------------------------------------------------------
# 2. body_url pagination
# ---------------------------------------------------------------------------

class TestBetterStackPagination(_BetterStackBase):
    @responses_lib.activate
    def test_pagination_stitches_two_pages_via_body_next_url(self):
        """collect() follows pagination.next as an absolute URL across 2 pages, in order."""
        page2_url = f"{API_BASE}/monitors?page=2"
        responses_lib.add(
            responses_lib.GET, f"{API_BASE}/monitors",
            json=_page([_MONITOR_1], next_url=page2_url), status=200,
        )
        responses_lib.add(
            responses_lib.GET, page2_url,
            json=_page([_MONITOR_2], next_url=None), status=200,
        )

        m = api.load_manifests()["betterstack"]
        result = api.client(m, token_key="betterstack").collect("monitors")

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, ["2", "3"])  # both pages, in order
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_bearer_credential_on_all_pages_including_continuation(self):
        """Bearer token rides every request, including the continuation (page 2)."""
        page2_url = f"{API_BASE}/monitors?page=2"
        responses_lib.add(
            responses_lib.GET, f"{API_BASE}/monitors",
            json=_page([_MONITOR_1], next_url=page2_url), status=200,
        )
        responses_lib.add(
            responses_lib.GET, page2_url,
            json=_page([_MONITOR_2], next_url=None), status=200,
        )

        m = api.load_manifests()["betterstack"]
        api.client(m, token_key="betterstack").collect("monitors")

        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(auth.startswith("Bearer "), f"Missing Bearer on {call.request.url}")
            self.assertIn("fakesecrettoken", auth)

    @responses_lib.activate
    def test_single_page_no_continuation(self):
        """pagination.next=null on first page stops pagination immediately."""
        responses_lib.add(
            responses_lib.GET, f"{API_BASE}/monitors",
            json=_page([_MONITOR_1], next_url=None), status=200,
        )

        m = api.load_manifests()["betterstack"]
        result = api.client(m, token_key="betterstack").collect("monitors")

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses_lib.calls), 1)


# ---------------------------------------------------------------------------
# 3. api.pick on JSON:API shaped objects
# ---------------------------------------------------------------------------

class TestBetterStackPick(_BetterStackBase):
    def test_pick_monitor_attributes(self):
        picked = api.pick(_MONITOR_1, "id,attributes.status,attributes.pronounceable_name,attributes.url")
        self.assertEqual(picked["id"], "2")
        self.assertEqual(picked["attributes.status"], "up")
        self.assertEqual(picked["attributes.pronounceable_name"], "Uptime homepage")

    def test_pick_incident_attributes(self):
        picked = api.pick(
            _INCIDENT_1,
            "id,attributes.name,attributes.status,attributes.cause,attributes.started_at,attributes.resolved_at",
        )
        self.assertEqual(picked["id"], "25")
        self.assertEqual(picked["attributes.status"], "Started")
        self.assertEqual(picked["attributes.cause"], "Status 404")
        self.assertIsNone(picked.get("attributes.resolved_at"))


# ---------------------------------------------------------------------------
# 4. Generic CLI drive (python -m lib.api)
# ---------------------------------------------------------------------------

class TestBetterStackCLI(_BetterStackBase):
    @responses_lib.activate
    def test_cli_paginate_stitches_pages_with_pick(self):
        """`get betterstack monitors --paginate --pick …` stitches pages, auth on both."""
        page2_url = f"{API_BASE}/monitors?page=2"
        responses_lib.add(
            responses_lib.GET, f"{API_BASE}/monitors",
            json=_page([_MONITOR_1], next_url=page2_url), status=200,
        )
        responses_lib.add(
            responses_lib.GET, page2_url,
            json=_page([_MONITOR_2], next_url=None), status=200,
        )

        rc = api._main([
            "get", "betterstack", "monitors", "--paginate",
            "--pick", "id,attributes.status",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(auth.startswith("Bearer "))
            self.assertIn("fakesecrettoken", auth)

    @responses_lib.activate
    def test_cli_single_page_read(self):
        """`get betterstack monitors` (no --paginate) does a single-page read with bearer auth."""
        responses_lib.add(
            responses_lib.GET, f"{API_BASE}/monitors",
            json=_page([_MONITOR_1], next_url=f"{API_BASE}/monitors?page=2"), status=200,
        )
        rc = api._main(["get", "betterstack", "monitors"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)
        auth = responses_lib.calls[0].request.headers.get("Authorization", "")
        self.assertTrue(auth.startswith("Bearer "))
        self.assertIn("fakesecrettoken", auth)


# ---------------------------------------------------------------------------
# 5. Token-prefix hygiene
# ---------------------------------------------------------------------------

class TestBetterStackHygiene(unittest.TestCase):
    """CI guard: no real Better Stack API token prefix may land in the connector files.

    Scoped to the connector dir (only manifest.yaml remains), NOT this test file — the test
    legitimately names the prefix it hunts for, so scanning itself would be a false positive.
    """

    # Better Stack live tokens follow "bt_live_...". Split the literal so the guard can't flag this file.
    _TOKEN_PREFIXES = ("bt_" "live",)

    def test_no_token_prefixes_in_betterstack_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "betterstack"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file() or path.suffix == ".pyc":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref}")
        self.assertEqual(offenders, [], f"real token prefix found in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
