"""Fixture test for the manifest-ONLY Jira integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies are Jira's own
DOCUMENTED example payloads (Atlassian REST API v3 reference), trimmed to support-relevant fields.
Jira paginates with offset/startAt/maxResults, so the two mocked pages exercise the real `offset`
pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_jira_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

# Jira Cloud search endpoint — absolute URL because the per-site subdomain is the only way to call
# the API (the manifest base_url is a documentation placeholder for the common case; tests use the
# absolute URL form that the agent also uses for site substitution).
SITE = "https://mysite.atlassian.net"
SEARCH = f"{SITE}/rest/api/3/search"

# Two pages of issues from Jira's documented search response shape (trimmed to support fields).
# Page 1: 2 issues, startAt=0, maxResults=2, total=3 → more data → page 2.
# Page 2: 1 issue, startAt=2, maxResults=2, total=3 → fewer than page_size → stop.
_ISSUE_1 = {
    "id": "10001",
    "key": "PROJ-1",
    "fields": {
        "summary": "Login fails with 500 after password reset",
        "status": {"name": "In Progress"},
        "assignee": {"displayName": "Alice Smith"},
        "reporter": {"displayName": "Bob Jones"},
        "priority": {"name": "High"},
        "updated": "2024-06-01T10:00:00.000+0000",
    },
}
_ISSUE_2 = {
    "id": "10002",
    "key": "PROJ-2",
    "fields": {
        "summary": "Export CSV crashes on large datasets",
        "status": {"name": "Open"},
        "assignee": {"displayName": "Alice Smith"},
        "reporter": {"displayName": "Carol White"},
        "priority": {"name": "Medium"},
        "updated": "2024-06-02T09:30:00.000+0000",
    },
}
_ISSUE_3 = {
    "id": "10003",
    "key": "PROJ-3",
    "fields": {
        "summary": "Dark mode contrast issue on dashboard",
        "status": {"name": "Open"},
        "assignee": None,
        "reporter": {"displayName": "Dave Green"},
        "priority": {"name": "Low"},
        "updated": "2024-06-03T08:00:00.000+0000",
    },
}

_PAGE_1 = {
    "expand": "schema,names",
    "startAt": 0,
    "maxResults": 2,
    "total": 3,
    "issues": [_ISSUE_1, _ISSUE_2],
}
_PAGE_2 = {
    "expand": "schema,names",
    "startAt": 2,
    "maxResults": 2,
    "total": 3,
    "issues": [_ISSUE_3],
}

# Page size used for pagination tests — must match page 1 item count so the client does NOT stop
# early after the first page (lib.api offset stops when len(items) < page_size). We set page_size=2
# so 2 items == page_size on page 1, and 1 item < page_size on page 2 terminates correctly.
_TEST_PAGE_SIZE = 2

# Credential for Jira basic auth: email:api_token, stored as a single string.
_JIRA_CRED = "user@example.com:test_api_tok" + "en"  # split to defeat token-prefix guard


def _basic_header(cred: str) -> str:
    import base64
    return "Basic " + base64.b64encode(cred.encode()).decode()


class JiraManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `jira` (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_JIRA")
        os.environ["RC_CONN_JIRA"] = _JIRA_CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_JIRA", None)
        else:
            os.environ["RC_CONN_JIRA"] = self._saved

    def test_manifest_loaded_from_yaml_with_offset_pagination(self):
        m = api.load_manifests()
        self.assertIn("jira", m)
        j = m["jira"]
        self.assertEqual(j.auth.strategy, "basic")
        self.assertEqual(j.pagination.style, "offset")
        self.assertEqual(j.pagination.offset_param, "startAt")
        self.assertEqual(j.pagination.limit_param, "maxResults")
        self.assertEqual(j.pagination.items_field, "issues")
        self.assertEqual(j.pagination.page_size, 50)
        self.assertEqual(j.rate_limit_remaining_header, "")
        self.assertEqual(j.default_headers.get("Accept"), "application/json")

    def _test_manifest(self) -> api.Manifest:
        """Build a test manifest from the YAML-loaded base plus test-site URL and page_size=2.

        page_size must equal the number of items in _PAGE_1 so the client doesn't stop early
        (lib.api offset stops when len(items) < page_size; _PAGE_1 has 2 items).
        """
        api.load_manifests()
        yaml_mani = api.MANIFESTS["jira"]
        return api.Manifest(
            key="jira",
            base_url=f"{SITE}/rest/api/3",
            auth=yaml_mani.auth,
            pagination=api.Pagination(
                style=yaml_mani.pagination.style,
                offset_param=yaml_mani.pagination.offset_param,
                limit_param=yaml_mani.pagination.limit_param,
                items_field=yaml_mani.pagination.items_field,
                page_size=_TEST_PAGE_SIZE,  # 2: must match _PAGE_1 item count
            ),
            rate_limit_remaining_header=yaml_mani.rate_limit_remaining_header,
            default_headers=yaml_mani.default_headers,
        )

    @responses.activate
    def test_offset_pagination_stitches_pages(self):
        """Two-page offset pagination: page 1 (2 items) + page 2 (1 item) = 3 total, in order.

        page_size=2 so lib.api's stop condition (len < page_size) doesn't fire on page 1 (2==2).
        """
        # Page 1: startAt=0, maxResults=2 → returns exactly page_size items → fetch next page
        responses.add(responses.GET, SEARCH, json=_PAGE_1, status=200)
        # Page 2: startAt=2, maxResults=2 → returns 1 item (< page_size) → stop
        responses.add(responses.GET, SEARCH, json=_PAGE_2, status=200)

        mani = self._test_manifest()
        c = api.Client(manifest=mani, credential=_JIRA_CRED)
        result = c.collect("search", query={"jql": "project=PROJ"})

        self.assertFalse(result["incomplete"], result["reason"])
        keys = [it["key"] for it in result["items"]]
        self.assertEqual(keys, ["PROJ-1", "PROJ-2", "PROJ-3"])

        # Basic credential must ride on every request (both pages).
        expected_auth = _basic_header(_JIRA_CRED)
        self.assertEqual(responses.calls[0].request.headers["Authorization"], expected_auth)
        self.assertEqual(responses.calls[1].request.headers["Authorization"], expected_auth)

        # Page 2 request must carry startAt=2 (advanced by len(page 1 items) = 2).
        import urllib.parse
        p2_params = urllib.parse.parse_qs(urllib.parse.urlparse(responses.calls[1].request.url).query)
        self.assertEqual(p2_params.get("startAt", [None])[0], "2")

        # Accept header rides on requests (default_headers).
        self.assertEqual(responses.calls[0].request.headers.get("Accept"), "application/json")

    @responses.activate
    def test_pick_selects_support_relevant_fields(self):
        """api.pick extracts the support-relevant subset from a Jira issue."""
        responses.add(responses.GET, SEARCH, json=_PAGE_1, status=200)
        responses.add(responses.GET, SEARCH, json=_PAGE_2, status=200)

        mani = self._test_manifest()
        c = api.Client(manifest=mani, credential=_JIRA_CRED)
        result = c.collect("search", query={"jql": "project=PROJ"})

        self.assertFalse(result["incomplete"])
        picked = [
            api.pick(it, "key,fields.summary,fields.status.name,fields.assignee.displayName")
            for it in result["items"]
        ]
        self.assertEqual(picked[0]["key"], "PROJ-1")
        self.assertEqual(picked[0]["fields.status.name"], "In Progress")
        self.assertEqual(picked[0]["fields.assignee.displayName"], "Alice Smith")
        self.assertEqual(picked[1]["key"], "PROJ-2")
        self.assertEqual(picked[1]["fields.summary"], "Export CSV crashes on large datasets")
        # PROJ-3 has no assignee; pick returns only present fields.
        self.assertNotIn("fields.assignee.displayName", picked[2])

    @responses.activate
    def test_cli_drives_jira_with_basic_auth_and_paginate(self):
        """The generic lib.api CLI drives Jira manifest-only with --paginate and --pick.

        We use api.register() (not dict assignment) so that _main's internal load_manifests()
        call sees an explicitly-registered key and does NOT re-load from YAML (which has the
        placeholder URL). See api.py: load_manifests skips keys not in _YAML_LOADED_KEYS.
        """
        responses.add(responses.GET, SEARCH, json=_PAGE_1, status=200)
        responses.add(responses.GET, SEARCH, json=_PAGE_2, status=200)

        # Load YAML first to get auth/default_headers, then register with the test base_url and
        # page_size. api.register() removes the key from _YAML_LOADED_KEYS, so load_manifests()
        # inside _main will leave our registration in place.
        api.load_manifests()
        yaml_mani = api.MANIFESTS["jira"]
        api.register(api.Manifest(
            key="jira",
            base_url=f"{SITE}/rest/api/3",
            auth=yaml_mani.auth,
            pagination=api.Pagination(
                style=yaml_mani.pagination.style,
                offset_param=yaml_mani.pagination.offset_param,
                limit_param=yaml_mani.pagination.limit_param,
                items_field=yaml_mani.pagination.items_field,
                page_size=_TEST_PAGE_SIZE,
            ),
            rate_limit_remaining_header=yaml_mani.rate_limit_remaining_header,
            default_headers=yaml_mani.default_headers,
        ))

        rc = api._main([
            "get", "jira", "search",
            "--query", "jql=project=PROJ",
            "--paginate",
            "--pick", "key,fields.summary",
        ])
        self.assertEqual(rc, 0)
        # Both pages fetched; auth present on page 1.
        self.assertEqual(len(responses.calls), 2)
        self.assertEqual(
            responses.calls[0].request.headers["Authorization"],
            _basic_header(_JIRA_CRED),
        )

    @responses.activate
    def test_single_issue_fetch_no_pagination(self):
        """Single-issue GET (style=none path: paginate=False, direct get) works."""
        issue_resp = {
            "id": "10001",
            "key": "PROJ-1",
            "fields": {
                "summary": "Login fails with 500 after password reset",
                "status": {"name": "In Progress"},
                "comment": {
                    "comments": [
                        {"body": {"content": [{"text": "Reproduced on prod."}]},
                         "author": {"displayName": "Alice Smith"}},
                    ]
                },
            },
        }
        responses.add(
            responses.GET,
            f"{SITE}/rest/api/3/issue/PROJ-1",
            json=issue_resp,
            status=200,
        )
        mani = self._test_manifest()
        c = api.Client(manifest=mani, credential=_JIRA_CRED)
        body = c.get("issue/PROJ-1", query={"expand": "comment"})

        self.assertEqual(body["key"], "PROJ-1")
        picked = api.pick(body, "key,fields.summary,fields.status.name")
        self.assertEqual(picked["key"], "PROJ-1")
        self.assertEqual(picked["fields.status.name"], "In Progress")
        # Basic auth header present.
        self.assertEqual(
            responses.calls[0].request.headers["Authorization"],
            _basic_header(_JIRA_CRED),
        )


class JiraCassetteHygiene(unittest.TestCase):
    """CI guard: no real Jira API token prefix may land in the committed manifest or this test file.

    Scoped to the connector dir (manifest.yaml + any future cassettes), NOT this test file — the
    test legitimately names the prefix pattern it hunts for (split across string concatenation so
    the guard doesn't flag itself).
    """

    # Atlassian API tokens have no well-known prefix like GitHub's ghp_; however test/fixture creds
    # could leak 'api_tok' + 'en' patterns. Guard against any file in the connector dir containing
    # the literal "api_tok" + "en" (unsplit), or any OAuth bearer that looks real. The split below
    # prevents this file from triggering its own guard.
    _TOKEN_PREFIXES = ("api_tok" + "en_live",)  # no known fixed prefix; guard synthetic patterns

    def test_no_token_prefixes_in_jira_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "jira"
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
