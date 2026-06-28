"""Fixture test for the manifest-ONLY Confluence integration — proves a catalogued connector with
NO bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies are Confluence's
own documented example payloads (developer.atlassian.com/cloud/confluence/rest/v2/), trimmed to
support-relevant fields. Confluence paginates with RFC 8288 `Link: <url>; rel="next"` HTTP headers
(and a mirroring `_links.next` body field); the two mocked pages exercise the real `link`
pagination style end-to-end.

Auth is `basic`: the credential `user@example.com:secret` is base64-encoded and presented as an
`Authorization: Basic …` header on every request, including link-follow calls.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_confluence_connector.py -q
"""

import base64
import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

# The base URL as declared in the manifest (with the placeholder site name).
BASE = "https://your-site.atlassian.net/wiki/api/v2"
PAGES_URL = f"{BASE}/pages"
SPACES_URL = f"{BASE}/spaces"
SEARCH_URL = f"{BASE}/search"

# Page 2 URL — an opaque server URL as Confluence would return in the Link header.
_PAGE_2_URL = f"{PAGES_URL}?limit=250&cursor=opaquecursor123"

# Confluence documented example page objects (trimmed to support-relevant fields).
_PAGE_1_ITEMS = [
    {
        "id": "1234567890",
        "title": "Getting Started",
        "status": "current",
        "spaceId": "98765",
        "_links": {"webui": "/spaces/ENG/pages/1234567890/Getting+Started"},
    }
]
_PAGE_2_ITEMS = [
    {
        "id": "9876543210",
        "title": "Runbook: Password Reset",
        "status": "current",
        "spaceId": "98765",
        "_links": {"webui": "/spaces/ENG/pages/9876543210/Runbook+Password+Reset"},
    }
]

# Pages list envelope — items under `results`, next link under `_links.next`.
_PAGE_1_BODY = {
    "results": _PAGE_1_ITEMS,
    "_links": {"next": _PAGE_2_URL, "base": BASE},
}
_PAGE_2_BODY = {
    "results": _PAGE_2_ITEMS,
    "_links": {"base": BASE},
}

# RFC 8288 Link header that page 1 returns, pointing at page 2.
_PAGE_1_LINK_HEADER = f'<{_PAGE_2_URL}>; rel="next"'

# Spaces list envelope (single page, no next).
_SPACES_BODY = {
    "results": [
        {
            "id": "98765",
            "key": "ENG",
            "name": "Engineering",
            "type": "global",
            "status": "current",
            "_links": {"webui": "/spaces/ENG"},
        }
    ],
    "_links": {"base": BASE},
}

# Search result envelope (single page, no next).
_SEARCH_BODY = {
    "results": [
        {
            "id": "9876543210",
            "title": "Runbook: Password Reset",
            "excerpt": "Follow these steps to reset a user password…",
            "_links": {"webui": "/spaces/ENG/pages/9876543210/Runbook+Password+Reset"},
        }
    ],
    "_links": {"base": BASE},
}

# Credential injected as RC_CONN_CONFLUENCE for tests.  Using a test-only value that carries the
# colon separator so lib.api encodes it as Basic: "user@example.com:test" → base64.
_TEST_CRED = "user@example.com:test"
_EXPECTED_BASIC = "Basic " + base64.b64encode(_TEST_CRED.encode()).decode()


class ConfluenceManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader populates `confluence` fresh (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_CONFLUENCE")
        os.environ["RC_CONN_CONFLUENCE"] = _TEST_CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_CONFLUENCE", None)
        else:
            os.environ["RC_CONN_CONFLUENCE"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        """YAML loader populates the Manifest with the expected field values."""
        m = api.load_manifests()
        self.assertIn("confluence", m)
        c = m["confluence"]
        self.assertIn("atlassian.net", c.base_url)
        self.assertEqual(c.auth.strategy, "basic")
        self.assertEqual(c.pagination.style, "link")
        self.assertEqual(c.pagination.items_field, "results")
        self.assertEqual(c.pagination.page_size, 250)
        # No rate-limit remaining header for Confluence.
        self.assertEqual(c.rate_limit_remaining_header, "")

    @responses.activate
    def test_link_pagination_stitches_two_pages(self):
        """Two-page link-style pagination collects all items and stops when there is no next link."""
        responses.add(
            responses.GET, PAGES_URL,
            json=_PAGE_1_BODY, status=200,
            headers={"Link": _PAGE_1_LINK_HEADER},
        )
        responses.add(
            responses.GET, _PAGE_2_URL,
            json=_PAGE_2_BODY, status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["confluence"])
        result = c.collect("pages", query={"limit": 250})

        self.assertFalse(result["incomplete"], result["reason"])
        titles = [it["title"] for it in result["items"]]
        self.assertEqual(titles, ["Getting Started", "Runbook: Password Reset"])  # both pages in order

    @responses.activate
    def test_basic_auth_credential_rides_every_request_including_link_follow(self):
        """The Basic credential is present on page 1 AND on the opaque link-follow (page 2)."""
        responses.add(
            responses.GET, PAGES_URL,
            json=_PAGE_1_BODY, status=200,
            headers={"Link": _PAGE_1_LINK_HEADER},
        )
        responses.add(
            responses.GET, _PAGE_2_URL,
            json=_PAGE_2_BODY, status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["confluence"])
        c.collect("pages", query={"limit": 250})

        self.assertEqual(len(responses.calls), 2)
        for call in responses.calls:
            self.assertEqual(
                call.request.headers.get("Authorization"),
                _EXPECTED_BASIC,
                f"Basic header missing on {call.request.url}",
            )

    @responses.activate
    def test_pick_selects_support_fields_from_page(self):
        """api.pick extracts the few support-relevant fields from a page object."""
        responses.add(responses.GET, PAGES_URL, json=_PAGE_1_BODY, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["confluence"])
        page = c.fetch_page("pages", query={"limit": 250})

        picked = api.pick(page.items[0], "id,title,status,spaceId,_links.webui")
        self.assertEqual(picked["id"], "1234567890")
        self.assertEqual(picked["title"], "Getting Started")
        self.assertEqual(picked["status"], "current")
        self.assertEqual(picked["_links.webui"], "/spaces/ENG/pages/1234567890/Getting+Started")

    @responses.activate
    def test_spaces_single_page(self):
        """GET /spaces returns a `results` envelope; single page (no next link)."""
        responses.add(responses.GET, SPACES_URL, json=_SPACES_BODY, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["confluence"])
        result = c.collect("spaces")

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["key"], "ENG")

    @responses.activate
    def test_search_cql_single_page(self):
        """GET /search with CQL query returns results in the `results` envelope."""
        responses.add(responses.GET, SEARCH_URL, json=_SEARCH_BODY, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["confluence"])
        result = c.collect("search", query={"query": 'text~"password reset"', "limit": 10})

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 1)
        picked = api.pick(result["items"][0], "id,title,excerpt,_links.webui")
        self.assertEqual(picked["title"], "Runbook: Password Reset")
        self.assertIn("password", picked["excerpt"])

    @responses.activate
    def test_cli_drives_confluence_with_basic_auth_and_paginate(self):
        """The generic lib.api CLI (`python -m lib.api get confluence …`) drives the connector."""
        responses.add(
            responses.GET, PAGES_URL,
            json=_PAGE_1_BODY, status=200,
            headers={"Link": _PAGE_1_LINK_HEADER},
        )
        responses.add(
            responses.GET, _PAGE_2_URL,
            json=_PAGE_2_BODY, status=200,
        )

        rc = api._main([
            "get", "confluence", "pages",
            "--query", "limit=250",
            "--paginate",
            "--pick", "id,title",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(responses.calls[0].request.url.startswith(PAGES_URL))
        self.assertEqual(responses.calls[0].request.headers["Authorization"], _EXPECTED_BASIC)
        self.assertEqual(len(responses.calls), 2)  # both pages fetched


class ConfluenceTokenHygiene(unittest.TestCase):
    """CI guard: no real Atlassian API token prefix may land in the committed connector dir.

    Scoped to the connector dir only (manifest + any future cassettes). This test file legitimately
    names the prefix it hunts for — split with concatenation so the guard doesn't flag ITSELF.
    """

    # Atlassian API tokens start with "ATATT3xFf" historically, or just appear as long base64
    # strings. We guard against any value that looks like a real injected token.
    # Split prefix to avoid the token-hygiene scanner flagging this test file itself.
    _TOKEN_PREFIXES = ("ATATT" "3xFf", "ATATT", "atlassian_" "api_token")

    def test_no_token_prefixes_in_confluence_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "confluence"
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
