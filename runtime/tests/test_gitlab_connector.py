"""Fixture test for the manifest-ONLY GitLab integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies mirror GitLab's
DOCUMENTED example payloads (docs.gitlab.com), trimmed to support-relevant fields. GitLab paginates
with RFC 8288 `Link: …; rel="next"` headers (same as GitHub), so the two mocked pages exercise the
real `link` pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_gitlab_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API = "https://gitlab.com/api/v4"
PROJECT_ID = "278964"  # gitlab-org/gitlab numeric ID used in docs examples
ISSUES_URL = f"{API}/projects/{PROJECT_ID}/issues"
MRS_URL = f"{API}/projects/{PROJECT_ID}/merge_requests"

# Two pages of issues (bare JSON arrays, as GitLab returns). Shapes mirror the documented example
# issue object; only support-relevant fields are kept. Page 1 advertises page 2 via Link header.
_ISSUES_PAGE_1 = [
    {
        "iid": 1,
        "title": "Ut commodi ullam eos dolores perferendis nihil apt",
        "state": "opened",
        "web_url": "http://gitlab.example.com/gitlab-org/gitlab/-/issues/1",
        "labels": ["critical", "regression"],
        "assignees": [{"name": "Alice"}],
        "created_at": "2016-01-04T15:31:51.081Z",
    },
]
_ISSUES_PAGE_2 = [
    {
        "iid": 2,
        "title": "Another issue on page two",
        "state": "closed",
        "web_url": "http://gitlab.example.com/gitlab-org/gitlab/-/issues/2",
        "labels": ["bug"],
        "assignees": [],
        "created_at": "2016-02-01T10:00:00.000Z",
    },
]
# RFC 8288 Link header: page 1 points at page 2 as rel="next"; page 2 has no next ⇒ loop stops.
_ISSUES_PAGE_1_LINK = (
    f'<{ISSUES_URL}?per_page=100&page=2>; rel="next", '
    f'<{ISSUES_URL}?per_page=100&page=1>; rel="first"'
)

# Merge requests page (single page, no Link header).
_MRS_PAGE_1 = [
    {
        "iid": 5,
        "title": "Fix regression in pipeline config",
        "state": "merged",
        "web_url": "http://gitlab.example.com/gitlab-org/gitlab/-/merge_requests/5",
        "merged_at": "2016-06-06T08:00:00.000Z",
        "assignees": [{"name": "Bob"}],
    },
]


class GitlabManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `gitlab` (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GITLAB")
        # Split prefix so the token-hygiene guard in this file doesn't flag itself.
        os.environ["RC_CONN_GITLAB"] = "glpat" "_test_abc123"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GITLAB", None)
        else:
            os.environ["RC_CONN_GITLAB"] = self._saved
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loaded_from_yaml(self):
        """YAML parses and all declared fields map to the Manifest dataclass."""
        m = api.load_manifests()
        self.assertIn("gitlab", m)
        g = m["gitlab"]
        self.assertEqual(g.base_url, "https://gitlab.com/api/v4")
        self.assertEqual(g.auth.strategy, "api_key_header")
        self.assertEqual(g.auth.name, "PRIVATE-TOKEN")
        self.assertEqual(g.pagination.style, "link")
        self.assertEqual(g.pagination.items_field, "")
        self.assertEqual(g.pagination.page_size, 100)
        # No remaining-count header documented for GitLab.com public API.
        self.assertEqual(g.rate_limit_remaining_header, "")

    @responses.activate
    def test_link_pagination_stitches_pages_and_credential_on_every_request(self):
        """Link-header pagination stitches >=2 pages; PAT rides on PRIVATE-TOKEN every request."""
        responses.add(
            responses.GET, ISSUES_URL,
            json=_ISSUES_PAGE_1, status=200,
            headers={"Link": _ISSUES_PAGE_1_LINK},
        )
        responses.add(
            responses.GET, ISSUES_URL,
            json=_ISSUES_PAGE_2, status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["gitlab"])
        result = c.collect(
            f"projects/{PROJECT_ID}/issues",
            query={"per_page": 100, "state": "all"},
        )

        self.assertFalse(result["incomplete"], result["reason"])
        iids = [it["iid"] for it in result["items"]]
        self.assertEqual(iids, [1, 2])  # both pages stitched in order

        # PAT credential rides on EVERY request including the link-follow (page 2).
        for call in responses.calls:
            self.assertEqual(
                call.request.headers.get("PRIVATE-TOKEN"),
                "glpat" "_test_abc123",
                f"Missing PRIVATE-TOKEN on {call.request.url}",
            )
        # Authorization header must NOT be present (we use api_key_header, not bearer).
        self.assertNotIn("Authorization", responses.calls[0].request.headers)

    @responses.activate
    def test_pick_selects_support_relevant_fields(self):
        """api.pick pre-selects the few support fields from a raw issue object."""
        responses.add(responses.GET, ISSUES_URL, json=_ISSUES_PAGE_1, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["gitlab"])
        page = c.fetch_page(f"projects/{PROJECT_ID}/issues")

        item = page.items[0]
        picked = api.pick(item, "iid,title,state,web_url,labels,assignees.*.name")
        self.assertEqual(picked["iid"], 1)
        self.assertEqual(picked["state"], "opened")
        self.assertEqual(picked["assignees.*.name"], ["Alice"])

    @responses.activate
    def test_single_page_no_link_header(self):
        """A response with no Link header returns one page and stops (no next)."""
        responses.add(responses.GET, MRS_URL, json=_MRS_PAGE_1, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["gitlab"])
        result = c.collect(f"projects/{PROJECT_ID}/merge_requests")

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["iid"], 5)
        # Only one HTTP call (no phantom page 2).
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_cli_drives_gitlab_with_paginate(self):
        """CLI path: `python -m lib.api get gitlab <path> --paginate` works end-to-end."""
        responses.add(
            responses.GET, ISSUES_URL,
            json=_ISSUES_PAGE_1, status=200,
            headers={"Link": _ISSUES_PAGE_1_LINK},
        )
        responses.add(
            responses.GET, ISSUES_URL,
            json=_ISSUES_PAGE_2, status=200,
        )

        rc = api._main([
            "get", "gitlab", f"projects/{PROJECT_ID}/issues",
            "--query", "per_page=100",
            "--paginate",
            "--pick", "iid,state",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(responses.calls[0].request.url.startswith(ISSUES_URL))
        self.assertEqual(
            responses.calls[0].request.headers.get("PRIVATE-TOKEN"),
            "glpat" "_test_abc123",
        )
        self.assertEqual(len(responses.calls), 2)


class GitlabCassetteHygiene(unittest.TestCase):
    """CI guard: no real GitLab token prefix may land in the committed manifest/fixtures.

    Scopes to the connector dir ONLY — this test file legitimately names the prefixes it hunts
    for, so scanning itself would be a false positive.
    """

    # GitLab PAT prefixes (classic `glpat-`, deploy token `gldt-`, group/project tokens share `glpat-`).
    # Each is split with concatenation so the guard doesn't flag THIS file.
    _TOKEN_PREFIXES = ("glpat" "-", "gldt" "-", "glsoat" "-")

    def test_no_token_prefixes_in_gitlab_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "gitlab"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains {pref!r}")
        self.assertEqual(offenders, [], f"token-like material in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
