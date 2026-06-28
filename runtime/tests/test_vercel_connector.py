"""Fixture test for the manifest-ONLY Vercel integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies mirror Vercel's
documented example payloads (vercel.com/docs/rest-api/deployments/list-deployments), trimmed to
the support-relevant fields. Vercel paginates with a cursor: `pagination.next` (a JS millisecond
timestamp) is sent back as `?until=<value>`; null means no more pages.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_vercel_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API = "https://api.vercel.com"
DEPLOYMENTS = f"{API}/v7/deployments"

# Two pages of deployments. Page 1 carries pagination.next (timestamp); page 2 sets it to null.
_PAGE_1 = {
    "deployments": [
        {
            "uid": "dpl_abc123",
            "name": "my-app",
            "projectId": "prj_xyz789",
            "url": "my-app-abc123.vercel.app",
            "state": "READY",
            "target": "production",
            "createdAt": 1700000000000,
            "errorCode": None,
            "errorMessage": None,
            "inspectorUrl": "https://vercel.com/team/my-app/deployments/dpl_abc123",
        }
    ],
    "pagination": {
        "count": 1,
        "next": 1699999000000,  # timestamp cursor for the next page
        "prev": None,
    },
}
_PAGE_2 = {
    "deployments": [
        {
            "uid": "dpl_def456",
            "name": "my-app",
            "projectId": "prj_xyz789",
            "url": "my-app-def456.vercel.app",
            "state": "ERROR",
            "target": "production",
            "createdAt": 1699999000000,
            "errorCode": "BUILD_ERROR",
            "errorMessage": "Build failed",
            "inspectorUrl": "https://vercel.com/team/my-app/deployments/dpl_def456",
        }
    ],
    "pagination": {
        "count": 1,
        "next": None,   # null → loop stops
        "prev": 1700000000000,
    },
}


class VercelManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is the only thing that populates `vercel`.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_VERCEL")
        os.environ["RC_CONN_VERCEL"] = "tok_vercel_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_VERCEL", None)
        else:
            os.environ["RC_CONN_VERCEL"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("vercel", m)
        v = m["vercel"]
        self.assertEqual(v.base_url, "https://api.vercel.com")
        self.assertEqual(v.auth.strategy, "bearer")
        self.assertEqual(v.pagination.style, "cursor")
        self.assertEqual(v.pagination.cursor_field, "pagination.next")
        self.assertEqual(v.pagination.cursor_param, "until")
        self.assertEqual(v.pagination.has_more_field, "")
        self.assertEqual(v.pagination.items_field, "")
        self.assertEqual(v.pagination.page_size, 100)
        # No remaining-count header for Vercel.
        self.assertEqual(v.rate_limit_remaining_header, "")

    @responses_lib.activate
    def test_cursor_pagination_stitches_two_pages(self):
        # Page 1: returns pagination.next = timestamp → lib.api sends it as ?until=<ts> on page 2.
        # Page 2: pagination.next = null → loop stops.
        responses_lib.add(responses_lib.GET, DEPLOYMENTS, json=_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, DEPLOYMENTS, json=_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["vercel"])
        # collect() will visit both pages; items_field="" so _extract_items sees the full body
        # (not a list), yielding no items by the generic extractor — that's expected for Vercel
        # because its envelope key varies per endpoint. We validate pagination stitching via
        # the raw page bodies instead.
        pages = list(c.paginate("v7/deployments", query={"projectId": "prj_xyz789", "limit": 20}))
        self.assertEqual(len(pages), 2)

        # Page 1 has a next cursor, page 2 does not.
        self.assertIsNotNone(pages[0].next)
        self.assertIsNone(pages[1].next)

        # The bearer credential rode on BOTH requests.
        self.assertEqual(
            responses_lib.calls[0].request.headers["Authorization"],
            "Bearer tok_vercel_test",
        )
        self.assertEqual(
            responses_lib.calls[1].request.headers["Authorization"],
            "Bearer tok_vercel_test",
        )

        # Second request must have sent `until=<timestamp>` (the cursor from page 1).
        import urllib.parse
        p1_next = _PAGE_1["pagination"]["next"]
        qs2 = urllib.parse.parse_qs(urllib.parse.urlparse(responses_lib.calls[1].request.url).query)
        self.assertIn("until", qs2)
        self.assertEqual(qs2["until"][0], str(p1_next))

    @responses_lib.activate
    def test_pick_selects_support_fields_from_deployment(self):
        """pick() extracts dotted paths from a raw deployment body."""
        api.load_manifests()
        deployment = _PAGE_1["deployments"][0]
        result = api.pick(deployment, "uid,state,errorCode,url")
        self.assertEqual(result["uid"], "dpl_abc123")
        self.assertEqual(result["state"], "READY")
        self.assertIn("errorCode", result)
        self.assertEqual(result["url"], "my-app-abc123.vercel.app")

    @responses_lib.activate
    def test_cli_drives_vercel_with_bearer_single_page(self):
        """python -m lib.api get vercel … fetches a single page and prints JSON."""
        responses_lib.add(responses_lib.GET, DEPLOYMENTS, json=_PAGE_1, status=200)
        rc = api._main([
            "get", "vercel", "/v7/deployments",
            "--query", "projectId=prj_xyz789",
            "--query", "limit=20",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(
            responses_lib.calls[0].request.url.startswith(DEPLOYMENTS)
        )
        self.assertEqual(
            responses_lib.calls[0].request.headers["Authorization"],
            "Bearer tok_vercel_test",
        )
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_cli_paginate_collects_all_pages(self):
        """--paginate collects both pages via the cursor."""
        responses_lib.add(responses_lib.GET, DEPLOYMENTS, json=_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, DEPLOYMENTS, json=_PAGE_2, status=200)
        rc = api._main([
            "get", "vercel", "/v7/deployments",
            "--query", "projectId=prj_xyz789",
            "--paginate",
        ])
        self.assertEqual(rc, 0)
        # Both pages fetched.
        self.assertEqual(len(responses_lib.calls), 2)


class VercelCassetteHygiene(unittest.TestCase):
    """CI guard: no real Vercel token prefix may land in the committed manifest or fixtures.

    Scopes to the connector dir only — this test file itself legitimately names the prefixes
    it hunts for, so scanning itself would be a false positive.
    """

    # Vercel personal access token prefix: "Bearer " is presentation, the raw token
    # chars vary but internal Vercel tokens often start with specific chars.
    # We guard the concrete prefix used in seeded connections: split to avoid self-trigger.
    _TOKEN_PREFIXES = (
        "dpl_",           # deployment ID (not a token but guard anyway)
        "Bearer " "dpl",  # guard composite forms
    )

    def test_no_token_prefixes_in_vercel_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "vercel"
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
