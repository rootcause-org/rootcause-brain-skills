"""Fixture test for the manifest-ONLY Statuspage integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies are
Statuspage's own documented example payloads, trimmed to the fields relevant to support diagnosis.
Statuspage paginates by 1-based PAGE NUMBER (page / per_page), so two mocked pages exercise the
real `page` pagination style end-to-end.

Auth uses `api_key_header` with name `Authorization`. The injected credential value includes the
`OAuth ` prefix (the operator stores the full header value), and the tests verify that prefix
rides every request verbatim.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_statuspage_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://api.statuspage.io/v1"
PAGE_ID = "kctbh9vrtdwd"
INCIDENTS_URL = f"{BASE}/pages/{PAGE_ID}/incidents"
COMPONENTS_URL = f"{BASE}/pages/{PAGE_ID}/components"

# Fixture bodies from Statuspage docs (trimmed to support-relevant fields).
# Two pages of incidents — bare JSON arrays, as Statuspage returns.
_INCIDENTS_PAGE_1 = [
    {
        "id": "cp3dhx28j8k5",
        "name": "Investigating elevated error rates",
        "status": "investigating",
        "impact": "major",
        "shortlink": "https://stspg.io/cp3dhx28j8k5",
        "created_at": "2024-01-15T10:00:00.000Z",
        "resolved_at": None,
        "incident_updates": [],
    },
]
_INCIDENTS_PAGE_2 = [
    {
        "id": "7k4z1m9t6b2x",
        "name": "API latency spike",
        "status": "resolved",
        "impact": "minor",
        "shortlink": "https://stspg.io/7k4z1m9t6b2x",
        "created_at": "2024-01-10T08:30:00.000Z",
        "resolved_at": "2024-01-10T09:15:00.000Z",
        "incident_updates": [],
    },
]

# Fixture: components list (single page — fewer than page_size triggers stop)
_COMPONENTS_PAGE_1 = [
    {
        "id": "b13bxk10mj22",
        "name": "API",
        "status": "operational",
        "description": "The Statuspage REST API",
        "created_at": "2022-03-01T00:00:00.000Z",
        "updated_at": "2024-01-15T10:05:00.000Z",
    },
    {
        "id": "ftgk25m9vt7p",
        "name": "Management Dashboard",
        "status": "degraded_performance",
        "description": "The web management UI",
        "created_at": "2022-03-01T00:00:00.000Z",
        "updated_at": "2024-01-15T10:02:00.000Z",
    },
]

# The token value the operator stores includes the `OAuth ` prefix (the full header value).
# Split here so the CI token-prefix guard doesn't flag this test file itself.
_CRED = "OAuth " + "sp1_test_token_abc123"


class StatuspageManifestOnly(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_STATUSPAGE")
        os.environ["RC_CONN_STATUSPAGE"] = _CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_STATUSPAGE", None)
        else:
            os.environ["RC_CONN_STATUSPAGE"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("statuspage", m)
        sp = m["statuspage"]
        self.assertEqual(sp.base_url, "https://api.statuspage.io/v1")
        self.assertEqual(sp.auth.strategy, "api_key_header")
        self.assertEqual(sp.auth.name, "Authorization")
        self.assertEqual(sp.pagination.style, "page")
        self.assertEqual(sp.pagination.page_param, "page")
        self.assertEqual(sp.pagination.page_start, 1)  # 1-based
        self.assertEqual(sp.pagination.limit_param, "per_page")
        self.assertEqual(sp.pagination.page_size, 100)
        self.assertEqual(sp.pagination.items_field, "")  # bare array responses
        self.assertEqual(sp.rate_limit_remaining_header, "")

    @responses_lib.activate
    def test_page_number_pagination_stitches_two_pages(self):
        # page_size overridden to 1 so each fixture page is "full" → page=1 (full), page=2 (full),
        # page=3 (empty) → stop. The page NUMBER advances 1→2→3, NOT an item-count offset.
        responses_lib.add(responses_lib.GET, INCIDENTS_URL, json=_INCIDENTS_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, INCIDENTS_URL, json=_INCIDENTS_PAGE_2, status=200)
        responses_lib.add(responses_lib.GET, INCIDENTS_URL, json=[], status=200)

        api.load_manifests()
        sp = api.MANIFESTS["statuspage"]
        import dataclasses
        sp_small = dataclasses.replace(
            sp,
            pagination=dataclasses.replace(sp.pagination, page_size=1),
        )
        c = api.Client(manifest=sp_small, credential=_CRED)
        result = c.collect(f"pages/{PAGE_ID}/incidents")

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, ["cp3dhx28j8k5", "7k4z1m9t6b2x"])  # both pages stitched

        # Page NUMBER advances 1 → 2 → 3 (not 0 → 1 → 2 offsets).
        self.assertEqual(len(responses_lib.calls), 3)
        self.assertIn("page=1", responses_lib.calls[0].request.url)
        self.assertIn("page=2", responses_lib.calls[1].request.url)
        self.assertIn("page=3", responses_lib.calls[2].request.url)

        # The credential rode on every request — including the page-2 and page-3 fetches.
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], _CRED)

    @responses_lib.activate
    def test_credential_header_format(self):
        """The full `OAuth <key>` value must appear verbatim as the Authorization header."""
        responses_lib.add(responses_lib.GET, INCIDENTS_URL, json=_INCIDENTS_PAGE_1, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["statuspage"])
        c.get(f"pages/{PAGE_ID}/incidents")

        self.assertEqual(len(responses_lib.calls), 1)
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], _CRED)

    @responses_lib.activate
    def test_pick_selects_support_fields(self):
        """api.pick extracts the four key support fields from an incident."""
        responses_lib.add(responses_lib.GET, INCIDENTS_URL, json=_INCIDENTS_PAGE_1, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["statuspage"])
        incidents = c.get(f"pages/{PAGE_ID}/incidents")

        self.assertIsInstance(incidents, list)
        picked = api.pick(incidents[0], "id,name,status,impact,shortlink,created_at")
        self.assertEqual(picked["id"], "cp3dhx28j8k5")
        self.assertEqual(picked["status"], "investigating")
        self.assertEqual(picked["impact"], "major")
        self.assertEqual(picked["shortlink"], "https://stspg.io/cp3dhx28j8k5")

    @responses_lib.activate
    def test_components_single_page(self):
        """A page with fewer items than page_size stops pagination immediately."""
        responses_lib.add(responses_lib.GET, COMPONENTS_URL, json=_COMPONENTS_PAGE_1, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["statuspage"])
        result = c.collect(f"pages/{PAGE_ID}/components")

        self.assertFalse(result["incomplete"], result["reason"])
        # 2 items < page_size 100 → single page fetched, loop stops
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(len(responses_lib.calls), 1)
        self.assertEqual(result["items"][0]["name"], "API")
        self.assertEqual(result["items"][1]["status"], "degraded_performance")

    @responses_lib.activate
    def test_cli_get_incidents(self):
        """CLI `python -m lib.api get statuspage …` round-trips through the manifest and auth."""
        responses_lib.add(responses_lib.GET, INCIDENTS_URL, json=_INCIDENTS_PAGE_1, status=200)

        rc = api._main([
            "get", "statuspage", f"pages/{PAGE_ID}/incidents",
            "--pick", "id,name,status,impact",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)
        self.assertTrue(responses_lib.calls[0].request.url.startswith(INCIDENTS_URL))
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], _CRED)

    @responses_lib.activate
    def test_cli_paginate_incidents(self):
        """CLI --paginate auto-collects all pages."""
        responses_lib.add(responses_lib.GET, INCIDENTS_URL, json=_INCIDENTS_PAGE_1, status=200)
        # Second call: empty → stops (manifest page_size=100, first page has 1 item < 100)
        # Actually: 1 < 100 means it already stops. So one call is enough for paginate test.
        rc = api._main([
            "get", "statuspage", f"pages/{PAGE_ID}/incidents",
            "--paginate",
            "--pick", "id,status",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)


class StatuspageCassetteHygiene(unittest.TestCase):
    """CI guard: no real Statuspage API key prefix may appear in the connector dir.

    Scopes to the connector dir only — this test file legitimately references the prefix patterns
    so scanning itself would be a false positive.
    """

    # Statuspage API key prefixes — split to avoid triggering the guard on this file itself.
    _TOKEN_PREFIXES = ("sp1" "_", "sp_",)

    def test_no_token_prefixes_in_statuspage_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "statuspage"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains {pref!r}")
        self.assertEqual(offenders, [], f"token-like prefix in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
