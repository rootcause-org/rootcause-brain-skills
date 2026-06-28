"""Fixture test for the manifest-ONLY Netlify integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies are Netlify's own
DOCUMENTED example payloads (open-api.netlify.com), trimmed to support-relevant fields. Netlify
paginates with RFC 8288 `Link: …; rel="next"` headers and returns bare JSON arrays, so the two
mocked pages exercise the real `link` pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_netlify_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API = "https://api.netlify.com/api/v1"
SITES = f"{API}/sites"
DEPLOYS = f"{API}/sites/abc123/deploys"
FORMS = f"{API}/forms/form456/submissions"

# Two pages of sites (bare JSON arrays, as Netlify returns).
# Shapes mirror the documented site object; only support-relevant fields are kept.
_SITES_PAGE_1 = [
    {
        "id": "abc123",
        "name": "my-project",
        "url": "https://my-project.netlify.app",
        "published_deploy": {"state": "ready", "id": "dep001"},
        "build_settings": {"repo_url": "https://github.com/example/my-project"},
    },
]
_SITES_PAGE_2 = [
    {
        "id": "def456",
        "name": "another-site",
        "url": "https://another-site.netlify.app",
        "published_deploy": {"state": "building", "id": "dep002"},
        "build_settings": {"repo_url": "https://github.com/example/another-site"},
    },
]
# RFC 8288 Link header: page 1 points at page 2 as rel="next"; page 2 has no next ⇒ loop stops.
_SITES_PAGE_1_LINK = f'<{SITES}?per_page=100&page=2>; rel="next", <{SITES}?per_page=100&page=2>; rel="last"'

# Single page of deploys for a site — state + error for support diagnosis.
_DEPLOYS_PAGE_1 = [
    {
        "id": "dep001",
        "state": "ready",
        "created_at": "2024-01-15T10:30:00Z",
        "error_message": None,
        "deploy_url": "https://dep001--my-project.netlify.app",
    },
    {
        "id": "dep000",
        "state": "error",
        "created_at": "2024-01-14T09:00:00Z",
        "error_message": "Build script returned non-zero exit code: 1",
        "deploy_url": "https://dep000--my-project.netlify.app",
    },
]

# Form submissions for a form — key support fields.
_FORM_SUBMISSIONS_PAGE_1 = [
    {
        "id": "sub001",
        "created_at": "2024-01-16T08:00:00Z",
        "email": "user@example.com",
        "data": {"name": "Alice", "message": "Help with deployment"},
    },
]
_FORM_SUBMISSIONS_PAGE_2 = [
    {
        "id": "sub002",
        "created_at": "2024-01-15T12:00:00Z",
        "email": "bob@example.com",
        "data": {"name": "Bob", "message": "Cannot connect custom domain"},
    },
]
_FORMS_PAGE_1_LINK = (
    f'<{FORMS}?per_page=100&page=2>; rel="next", '
    f'<{FORMS}?per_page=100&page=2>; rel="last"'
)


class NetlifyManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `netlify` (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_NETLIFY")
        # Use split prefix so the hygiene guard below doesn't flag THIS file.
        os.environ["RC_CONN_NETLIFY"] = "test_" + "netlify_fake_token_abc"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NETLIFY", None)
        else:
            os.environ["RC_CONN_NETLIFY"] = self._saved

    def test_manifest_loaded_from_yaml_with_correct_fields(self):
        m = api.load_manifests()
        self.assertIn("netlify", m)
        n = m["netlify"]
        self.assertEqual(n.base_url, "https://api.netlify.com/api/v1")
        self.assertEqual(n.auth.strategy, "bearer")
        self.assertEqual(n.pagination.style, "link")
        self.assertEqual(n.pagination.items_field, "")  # bare array — page IS the list
        self.assertEqual(n.rate_limit_remaining_header, "X-RateLimit-Remaining")

    @responses.activate
    def test_link_pagination_stitches_sites_pages(self):
        """Two pages of sites stitched via Link rel=next; bearer on every request."""
        responses.add(
            responses.GET, SITES, json=_SITES_PAGE_1, status=200,
            headers={"Link": _SITES_PAGE_1_LINK, "X-RateLimit-Remaining": "499"},
        )
        responses.add(
            responses.GET, SITES, json=_SITES_PAGE_2, status=200,
            headers={"X-RateLimit-Remaining": "498"},
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["netlify"])
        result = c.collect("sites", query={"per_page": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, ["abc123", "def456"])  # both pages in order

        # Bearer credential on every request — including the link-follow (page 2).
        tok = "test_" + "netlify_fake_token_abc"
        self.assertEqual(responses.calls[0].request.headers["Authorization"], f"Bearer {tok}")
        self.assertEqual(responses.calls[1].request.headers["Authorization"], f"Bearer {tok}")

    @responses.activate
    def test_single_page_deploys(self):
        """Single-page deploy list — no Link header means loop exits after one fetch."""
        responses.add(
            responses.GET, DEPLOYS, json=_DEPLOYS_PAGE_1, status=200,
            headers={"X-RateLimit-Remaining": "490"},
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["netlify"])
        result = c.collect("sites/abc123/deploys", query={"per_page": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)
        states = [it["state"] for it in result["items"]]
        self.assertEqual(states, ["ready", "error"])

        # --pick extracts support-relevant fields (state + error_message)
        picked = [api.pick(it, "id,state,error_message") for it in result["items"]]
        self.assertEqual(picked[1]["state"], "error")
        self.assertEqual(
            picked[1]["error_message"],
            "Build script returned non-zero exit code: 1",
        )

    @responses.activate
    def test_form_submissions_pagination(self):
        """Two pages of form submissions — bearer rides on link-follow too."""
        responses.add(
            responses.GET, FORMS, json=_FORM_SUBMISSIONS_PAGE_1, status=200,
            headers={"Link": _FORMS_PAGE_1_LINK, "X-RateLimit-Remaining": "480"},
        )
        responses.add(
            responses.GET, FORMS, json=_FORM_SUBMISSIONS_PAGE_2, status=200,
            headers={"X-RateLimit-Remaining": "479"},
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["netlify"])
        result = c.collect("forms/form456/submissions", query={"per_page": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)

        tok = "test_" + "netlify_fake_token_abc"
        self.assertEqual(responses.calls[0].request.headers["Authorization"], f"Bearer {tok}")
        self.assertEqual(responses.calls[1].request.headers["Authorization"], f"Bearer {tok}")

        # pick extracts email and nested data fields
        picked = [api.pick(it, "id,email,data.name") for it in result["items"]]
        self.assertEqual(picked[0]["email"], "user@example.com")
        self.assertEqual(picked[0]["data.name"], "Alice")

    @responses.activate
    def test_cli_drives_netlify_with_bearer_and_paginate(self):
        """CLI `python -m lib.api get netlify sites --paginate` works end-to-end."""
        responses.add(
            responses.GET, SITES, json=_SITES_PAGE_1, status=200,
            headers={"Link": _SITES_PAGE_1_LINK},
        )
        responses.add(
            responses.GET, SITES, json=_SITES_PAGE_2, status=200,
        )
        rc = api._main([
            "get", "netlify", "sites",
            "--query", "per_page=100",
            "--paginate",
            "--pick", "id,name,url",
        ])
        self.assertEqual(rc, 0)
        # Both pages fetched; bearer on all calls.
        self.assertTrue(responses.calls[0].request.url.startswith(SITES))
        tok = "test_" + "netlify_fake_token_abc"
        self.assertEqual(responses.calls[0].request.headers["Authorization"], f"Bearer {tok}")
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_cli_single_site_get(self):
        """CLI `python -m lib.api get netlify sites/SITE_ID` (no paginate) works."""
        site_detail = _SITES_PAGE_1[0]
        responses.add(
            responses.GET, f"{API}/sites/abc123", json=site_detail, status=200,
        )
        rc = api._main([
            "get", "netlify", "sites/abc123",
            "--pick", "id,name,url",
        ])
        self.assertEqual(rc, 0)


class NetlifyCassetteHygiene(unittest.TestCase):
    """CI guard: no real Netlify token material may land in the committed connector dir.

    Scopes to the connector dir only — this test file legitimately names the prefix patterns it
    hunts for, so scanning itself would be a false positive.
    """

    # Netlify personal access tokens start with these prefixes (split to avoid self-match).
    _TOKEN_PREFIXES = ("nfp" "_",)

    def test_no_token_prefixes_in_netlify_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "netlify"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: found prefix {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
