"""Fixture test for the manifest-ONLY GitHub integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies are GitHub's own
DOCUMENTED example issue payloads (docs.github.com "List repository issues"), trimmed to the fields
this test asserts on. GitHub paginates with RFC 8288 `Link: …; rel="next"` headers, so the two
mocked pages exercise the real `link` pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_github_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API = "https://api.github.com"
ISSUES = f"{API}/repos/octocat/Hello-World/issues"

# Two pages of issues (bare JSON arrays, as GitHub returns). Shapes mirror the documented example
# issue object; only support-relevant fields are kept. Page 1 advertises page 2 via the Link header.
_PAGE_1 = [
    {
        "number": 1347,
        "title": "Found a bug",
        "state": "open",
        "html_url": "https://github.com/octocat/Hello-World/issues/1347",
        "labels": [{"name": "bug"}, {"name": "help wanted"}],
    },
]
_PAGE_2 = [
    {
        "number": 1300,
        "title": "Feature request: dark mode",
        "state": "closed",
        "html_url": "https://github.com/octocat/Hello-World/issues/1300",
        "labels": [{"name": "enhancement"}],
    },
]
# RFC 8288 Link header: page 1 points at page 2 as rel="next"; page 2 has no next ⇒ loop stops.
_PAGE_1_LINK = f'<{ISSUES}?per_page=100&page=2>; rel="next", <{ISSUES}?per_page=100&page=2>; rel="last"'


class GithubManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `github` (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GITHUB")
        os.environ["RC_CONN_GITHUB"] = "tok_github_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GITHUB", None)
        else:
            os.environ["RC_CONN_GITHUB"] = self._saved

    def test_manifest_loaded_from_yaml_with_link_pagination(self):
        m = api.load_manifests()
        self.assertIn("github", m)
        g = m["github"]
        self.assertEqual(g.base_url, "https://api.github.com")
        self.assertEqual(g.auth.strategy, "bearer")
        self.assertEqual(g.pagination.style, "link")
        self.assertEqual(g.rate_limit_remaining_header, "X-RateLimit-Remaining")
        # Default headers carry the documented Accept + pinned API version.
        self.assertEqual(g.default_headers["Accept"], "application/vnd.github+json")
        self.assertEqual(g.default_headers["X-GitHub-Api-Version"], "2022-11-28")

    @responses.activate
    def test_link_pagination_stitches_pages_and_pick_selects_fields(self):
        # Page 1: bare array + Link rel="next" → page 2. Page 2: bare array, no Link → stop.
        responses.add(responses.GET, ISSUES, json=_PAGE_1, status=200,
                      headers={"Link": _PAGE_1_LINK, "X-RateLimit-Remaining": "4999"})
        responses.add(responses.GET, ISSUES, json=_PAGE_2, status=200,
                      headers={"X-RateLimit-Remaining": "4998"})

        api.load_manifests()
        c = api.client(api.MANIFESTS["github"])
        result = c.collect("repos/octocat/Hello-World/issues", query={"per_page": 100, "state": "all"})

        self.assertFalse(result["incomplete"], result["reason"])
        nums = [it["number"] for it in result["items"]]
        self.assertEqual(nums, [1347, 1300])  # both pages stitched, in order

        # The bearer credential rode along on the link-follow request too (not just page 1).
        self.assertEqual(responses.calls[0].request.headers["Authorization"], "Bearer tok_github_test")
        self.assertEqual(responses.calls[1].request.headers["Authorization"], "Bearer tok_github_test")
        # Documented default headers are present on the wire.
        self.assertEqual(responses.calls[0].request.headers["Accept"], "application/vnd.github+json")

        # --pick prunes the big issue object down to the few support-relevant fields.
        picked = [api.pick(it, "number,title,state,labels.*.name") for it in result["items"]]
        self.assertEqual(picked[0]["number"], 1347)
        self.assertEqual(picked[0]["state"], "open")
        self.assertEqual(picked[0]["labels.*.name"], ["bug", "help wanted"])
        self.assertEqual(picked[1]["labels.*.name"], ["enhancement"])

    @responses.activate
    def test_cli_drives_github_with_bearer_and_paginate(self):
        responses.add(responses.GET, ISSUES, json=_PAGE_1, status=200, headers={"Link": _PAGE_1_LINK})
        responses.add(responses.GET, ISSUES, json=_PAGE_2, status=200)
        rc = api._main([
            "get", "github", "repos/octocat/Hello-World/issues",
            "--query", "per_page=100", "--paginate", "--pick", "number,state",
        ])
        self.assertEqual(rc, 0)
        # First call hit the real base URL with the bearer; both pages were fetched.
        self.assertTrue(responses.calls[0].request.url.startswith(ISSUES))
        self.assertEqual(responses.calls[0].request.headers["Authorization"], "Bearer tok_github_test")
        self.assertEqual(len(responses.calls), 2)


class GithubCassetteHygiene(unittest.TestCase):
    """CI guard: no real GitHub token prefix may land in the committed manifest/fixtures.

    Scopes to the connector dir (manifest + any future cassette), NOT this test file — the test
    legitimately names the prefixes it hunts for, so scanning itself would be a false positive.
    """

    # GitHub token prefixes (PAT classic `ghp_`, OAuth `gho_`, fine-grained `github_pat_`, …).
    _TOKEN_PREFIXES = ("ghp" "_", "gho" "_", "ghu" "_", "ghs" "_", "ghr" "_", "github_pat" "_")

    def test_no_token_prefixes_in_github_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "github"
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
