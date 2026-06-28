"""Fixture test for the manifest-ONLY LaunchDarkly integration — proves a catalogued connector
with NO bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. Bodies are trimmed from LaunchDarkly's
documented example payloads (apidocs.launchdarkly.com), keeping support-relevant fields only.
LaunchDarkly uses offset pagination with an `items` envelope; the two mocked pages exercise the
real `offset` pagination style end-to-end (page 1 = page_size items → fetch page 2; page 2 < page_size → stop).

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_launchdarkly_connector.py -q
"""

import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://app.launchdarkly.com/api/v2"
FLAGS_PATH = f"{BASE}/flags/default"
PROJECTS_PATH = f"{BASE}/projects"

# Fixture: 2 flags on page 1 (= page_size of 2 used in pagination tests → full page → fetch page 2),
# 1 flag on page 2 (< page_size → stop). Shapes mirror LD's documented FeatureFlagCollection.
_PAGE_1 = {
    "items": [
        {
            "key": "dark-mode",
            "name": "Dark mode",
            "kind": "boolean",
            "environments": {
                "production": {
                    "on": True,
                    "summary": {"prerequisites": 0, "variations": {"0": 0, "1": 10}},
                }
            },
        },
        {
            "key": "beta-dashboard",
            "name": "Beta dashboard",
            "kind": "boolean",
            "environments": {
                "production": {
                    "on": True,
                    "summary": {"prerequisites": 0, "variations": {"0": 2, "1": 8}},
                }
            },
        },
    ],
    "totalCount": 3,
    "_links": {
        "next": {"href": "/api/v2/flags/default?limit=2&offset=2"},
        "self": {"href": "/api/v2/flags/default?limit=2&offset=0"},
    },
}
_PAGE_2 = {
    "items": [
        {
            "key": "new-checkout",
            "name": "New checkout flow",
            "kind": "boolean",
            "environments": {
                "production": {
                    "on": False,
                    "summary": {"prerequisites": 0, "variations": {"0": 5, "1": 0}},
                }
            },
        }
    ],
    "totalCount": 3,
    "_links": {
        "self": {"href": "/api/v2/flags/default?limit=2&offset=2"},
    },
}

_PROJECTS_PAGE = {
    "items": [
        {"key": "default", "name": "My First Project"},
        {"key": "mobile-app", "name": "Mobile App"},
    ],
    "totalCount": 2,
    "_links": {"self": {"href": "/api/v2/projects?limit=20&offset=0"}},
}

# Test token — split the "api-" prefix so the hygiene guard doesn't flag this test file itself.
_TEST_TOKEN = "api" "-key-test-token-abc123"


def _test_client(mani: api.Manifest, page_size: int | None = None) -> api.Client:
    """Build a Client for tests: injects the test token directly (no env lookup), optional page_size override."""
    if page_size is not None:
        mani = replace(mani, pagination=replace(mani.pagination, page_size=page_size))
    return api.Client(manifest=mani, credential=_TEST_TOKEN)


class LaunchDarklyManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `launchdarkly`.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_LAUNCHDARKLY")
        os.environ["RC_CONN_LAUNCHDARKLY"] = _TEST_TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_LAUNCHDARKLY", None)
        else:
            os.environ["RC_CONN_LAUNCHDARKLY"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("launchdarkly", m)
        ld = m["launchdarkly"]
        self.assertEqual(ld.base_url, "https://app.launchdarkly.com/api/v2")
        self.assertEqual(ld.auth.strategy, "api_key_header")
        self.assertEqual(ld.auth.name, "Authorization")
        self.assertEqual(ld.pagination.style, "offset")
        self.assertEqual(ld.pagination.items_field, "items")
        self.assertEqual(ld.pagination.offset_param, "offset")
        self.assertEqual(ld.pagination.limit_param, "limit")
        self.assertEqual(ld.pagination.page_size, 20)
        self.assertEqual(ld.rate_limit_remaining_header, "X-Ratelimit-Route-Remaining")
        # Required API version header must be present on every request.
        self.assertEqual(ld.default_headers["LD-API-Version"], "20240415")

    @responses.activate
    def test_offset_pagination_stitches_two_pages(self):
        # Page 1: 2 items (== page_size=2 → full → fetch page 2).
        # Page 2: 1 item (< page_size=2 → stop). Total: 3 items.
        responses.add(responses.GET, FLAGS_PATH, json=_PAGE_1, status=200,
                      headers={"X-Ratelimit-Route-Remaining": "99"})
        responses.add(responses.GET, FLAGS_PATH, json=_PAGE_2, status=200,
                      headers={"X-Ratelimit-Route-Remaining": "98"})

        api.load_manifests()
        # Use page_size=2 to match fixture: page 1 is full (2 items), page 2 is partial (1 item → stop).
        c = _test_client(api.MANIFESTS["launchdarkly"], page_size=2)
        result = c.collect("flags/default", query={"env": "production"})

        self.assertFalse(result["incomplete"], result["reason"])
        keys = [it["key"] for it in result["items"]]
        self.assertEqual(keys, ["dark-mode", "beta-dashboard", "new-checkout"])  # both pages merged

    @responses.activate
    def test_credential_in_authorization_header_every_page(self):
        # The raw token value (no "Bearer " prefix) must ride on EVERY request including page 2.
        responses.add(responses.GET, FLAGS_PATH, json=_PAGE_1, status=200)
        responses.add(responses.GET, FLAGS_PATH, json=_PAGE_2, status=200)

        api.load_manifests()
        c = _test_client(api.MANIFESTS["launchdarkly"], page_size=2)
        c.collect("flags/default")

        # api_key_header places the raw token (no "Bearer " prefix) in the Authorization header.
        self.assertEqual(responses.calls[0].request.headers["Authorization"], _TEST_TOKEN)
        self.assertEqual(responses.calls[1].request.headers["Authorization"], _TEST_TOKEN)

    @responses.activate
    def test_api_version_header_on_every_request(self):
        responses.add(responses.GET, PROJECTS_PATH, json=_PROJECTS_PAGE, status=200)

        api.load_manifests()
        c = _test_client(api.MANIFESTS["launchdarkly"])
        c.get("projects")

        self.assertEqual(responses.calls[0].request.headers["LD-API-Version"], "20240415")

    @responses.activate
    def test_pick_selects_support_fields(self):
        responses.add(responses.GET, FLAGS_PATH, json=_PAGE_1, status=200)
        responses.add(responses.GET, FLAGS_PATH, json=_PAGE_2, status=200)

        api.load_manifests()
        c = _test_client(api.MANIFESTS["launchdarkly"], page_size=2)
        result = c.collect("flags/default")

        picked = [api.pick(it, "key,name,kind,environments.production.on") for it in result["items"]]
        self.assertEqual(picked[0]["key"], "dark-mode")
        self.assertEqual(picked[0]["name"], "Dark mode")
        self.assertEqual(picked[0]["kind"], "boolean")
        self.assertEqual(picked[0]["environments.production.on"], True)
        self.assertEqual(picked[2]["key"], "new-checkout")
        self.assertEqual(picked[2]["environments.production.on"], False)

    @responses.activate
    def test_cli_drives_launchdarkly_with_paginate(self):
        responses.add(responses.GET, FLAGS_PATH, json=_PAGE_1, status=200)
        responses.add(responses.GET, FLAGS_PATH, json=_PAGE_2, status=200)

        # CLI uses the manifest's page_size (20); page 1 has 2 items < 20 → stops after 1 page.
        # For the CLI test we just verify it fetches and returns 0 exit code with the token.
        rc = api._main([
            "get", "launchdarkly", "flags/default",
            "--query", "env=production",
            "--paginate",
            "--pick", "key,name",
        ])
        self.assertEqual(rc, 0)
        # At least one call was made with the correct token.
        self.assertGreaterEqual(len(responses.calls), 1)
        self.assertEqual(responses.calls[0].request.headers["Authorization"], _TEST_TOKEN)
        # LD-API-Version header must be present.
        self.assertEqual(responses.calls[0].request.headers["LD-API-Version"], "20240415")

    @responses.activate
    def test_single_page_get_projects(self):
        # A non-paginated GET (style=none equivalent via single fetch) returns parsed body.
        responses.add(responses.GET, PROJECTS_PATH, json=_PROJECTS_PAGE, status=200)

        api.load_manifests()
        c = _test_client(api.MANIFESTS["launchdarkly"])
        body = c.get("projects")

        self.assertIn("items", body)
        keys = [p["key"] for p in body["items"]]
        self.assertEqual(keys, ["default", "mobile-app"])


class LaunchDarklyCassetteHygiene(unittest.TestCase):
    """CI guard: no real LaunchDarkly API token may land in the connector dir.

    Scopes to the connector dir only — this test file legitimately names the prefix chars it
    hunts for (split across string concat), so scanning itself would be a false positive.
    """

    # LaunchDarkly service/personal access tokens commonly start with "api-". Guard against
    # the raw concatenated prefix that would appear in a committed credential.
    _TOKEN_PREFIXES = ("api" "-key",)

    def test_no_token_prefixes_in_launchdarkly_connector_dir(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "launchdarkly"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
