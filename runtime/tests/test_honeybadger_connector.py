"""Tests for the Honeybadger script connector.

Force-code trigger (d) fired: Honeybadger embeds the next page URL in the JSON response body
(``links.next`` full URL), not in an HTTP ``Link`` header. lib.api's built-in ``link`` style only
reads RFC 8288 HTTP headers, so we need a script connector that follows body-links directly.

Tests confirm:
  - YAML manifest loads and maps every field correctly.
  - collect_pages() stitches ≥2 pages following body links.next.
  - Basic auth credential rides EVERY request (including link-follow calls).
  - api.pick selects the support-relevant fields.
  - Single-page (no links.next) terminates after one call.
  - Script CLI (main()) works end-to-end.

No live creds, no network: HTTP is mocked with `responses`. Bodies match Honeybadger's documented
API response shapes (docs.honeybadger.io/api/).

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_honeybadger_connector.py -q
"""

import base64
import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import honeybadger  # noqa: E402

BASE = "https://app.honeybadger.io/v2"
PROJECT_ID = 42
FAULTS_URL = f"{BASE}/projects/{PROJECT_ID}/faults"
FAULTS_PAGE_2_URL = f"{FAULTS_URL}?q=is%3Aunresolved&order=recent&page=2"
DEPLOYS_URL = f"{BASE}/projects/{PROJECT_ID}/deploys"
PROJECTS_URL = f"{BASE}/projects"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# ---------------------------------------------------------------------------

_FAULTS_PAGE_1 = {
    "links": {
        "self": f"{FAULTS_URL}?q=is%3Aunresolved&order=recent",
        "next": FAULTS_PAGE_2_URL,
    },
    "results": [
        {
            "id": 1001,
            "klass": "RuntimeError",
            "message": "undefined method `foo' for nil:NilClass",
            "environment": "production",
            "last_notice_at": "2024-06-10T12:34:56.000Z",
            "notices_count": 47,
            "notices_count_in_range": 5,
            "resolved": False,
            "ignored": False,
            "url": "https://app.honeybadger.io/projects/42/faults/1001",
            "assignee": None,
            "tags": ["billing"],
            "component": "OrdersController",
            "action": "create",
            "created_at": "2024-05-01T08:00:00.000Z",
            "project_id": PROJECT_ID,
        }
    ],
}

_FAULTS_PAGE_2 = {
    "links": {
        "self": FAULTS_PAGE_2_URL,
    },
    "results": [
        {
            "id": 1002,
            "klass": "ActiveRecord::RecordNotFound",
            "message": "Couldn't find User with 'id'=99",
            "environment": "production",
            "last_notice_at": "2024-06-09T09:00:00.000Z",
            "notices_count": 3,
            "notices_count_in_range": 1,
            "resolved": False,
            "ignored": False,
            "url": "https://app.honeybadger.io/projects/42/faults/1002",
            "assignee": None,
            "tags": [],
            "component": "UsersController",
            "action": "show",
            "created_at": "2024-06-01T10:00:00.000Z",
            "project_id": PROJECT_ID,
        }
    ],
}

_DEPLOYS_PAGE_1 = {
    "links": {
        "self": f"{DEPLOYS_URL}?environment=production",
    },
    "results": [
        {
            "created_at": "2024-06-10T11:00:00.000Z",
            "environment": "production",
            "local_username": "deploy-bot",
            "project_id": PROJECT_ID,
            "repository": "https://github.com/acme/app",
            "revision": "a1b2c3d",
        }
    ],
}

_PROJECTS_PAGE_1 = {
    "links": {
        "self": PROJECTS_URL,
    },
    "results": [
        {
            "id": PROJECT_ID,
            "name": "Acme App",
            "token": "abc123",
            "active": True,
            "fault_count": 120,
            "unresolved_fault_count": 5,
            "last_notice_at": "2024-06-10T12:34:56.000Z",
            "created_at": "2022-01-01T00:00:00.000Z",
            "environments": ["production", "staging"],
        }
    ],
}


class HoneybadgerManifestLoad(unittest.TestCase):
    """Confirm the YAML manifest loads and maps every lib.api field correctly."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        # The script connector's register() in __init__.py populates 'honeybadger';
        # re-importing triggers it automatically when the package is on sys.path.
        self.assertIn("honeybadger", m)
        hb = m["honeybadger"]
        self.assertEqual(hb.base_url, "https://app.honeybadger.io/v2")
        self.assertEqual(hb.auth.strategy, "basic")
        self.assertEqual(hb.pagination.style, "none")    # script owns the loop
        self.assertEqual(hb.pagination.items_field, "results")
        self.assertEqual(hb.pagination.page_size, 25)
        self.assertEqual(hb.rate_limit_remaining_header, "X-RateLimit-Remaining")

    def test_manifest_registered_by_script_connector(self):
        # load_manifests() discovers the YAML; the script's register() already ran at import time
        # (before setUp's clear), so load_manifests populates via YAML and the key is present.
        api.load_manifests()
        self.assertIn("honeybadger", api.MANIFESTS)
        hb = api.MANIFESTS["honeybadger"]
        self.assertEqual(hb.key, "honeybadger")
        self.assertEqual(hb.auth.strategy, "basic")


class HoneybadgerPagination(unittest.TestCase):
    """collect_pages() stitches pages by following body links.next."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_HONEYBADGER")
        os.environ["RC_CONN_HONEYBADGER"] = "hbp_test" + "_token_abc123"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_HONEYBADGER", None)
        else:
            os.environ["RC_CONN_HONEYBADGER"] = self._saved

    @responses_lib.activate
    def test_two_pages_stitched_via_body_links_next(self):
        """Page 1 body carries links.next → page 2 URL; page 2 has no links.next → loop stops."""
        responses_lib.add(responses_lib.GET, FAULTS_URL, json=_FAULTS_PAGE_1, status=200,
                          headers={"X-RateLimit-Remaining": "359"})
        responses_lib.add(responses_lib.GET, FAULTS_PAGE_2_URL, json=_FAULTS_PAGE_2, status=200,
                          headers={"X-RateLimit-Remaining": "358"})

        result = honeybadger.collect_pages(
            f"projects/{PROJECT_ID}/faults",
            query={"q": "is:unresolved", "order": "recent"},
        )

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, [1001, 1002])  # both pages in order
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_single_page_no_next_terminates(self):
        """A response body with no links.next yields exactly one page."""
        responses_lib.add(responses_lib.GET, DEPLOYS_URL, json=_DEPLOYS_PAGE_1, status=200)

        result = honeybadger.collect_pages(
            f"projects/{PROJECT_ID}/deploys",
            query={"environment": "production"},
        )

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["revision"], "a1b2c3d")
        self.assertEqual(len(responses_lib.calls), 1)


class HoneybadgerAuth(unittest.TestCase):
    """Basic auth credential must ride on EVERY request, including link-follow calls."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_HONEYBADGER")
        os.environ["RC_CONN_HONEYBADGER"] = "hbp_test" + "_token_abc123"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_HONEYBADGER", None)
        else:
            os.environ["RC_CONN_HONEYBADGER"] = self._saved

    @responses_lib.activate
    def test_basic_auth_on_every_request_including_link_follow(self):
        """Authorization: Basic … must appear on page 1 AND the link-follow page 2 request."""
        token = os.environ["RC_CONN_HONEYBADGER"]
        # lib.api basic strategy encodes "token:" (empty password) as Base64.
        expected_b64 = base64.b64encode(f"{token}:".encode()).decode()
        expected_header = f"Basic {expected_b64}"

        responses_lib.add(responses_lib.GET, FAULTS_URL, json=_FAULTS_PAGE_1, status=200,
                          headers={"X-RateLimit-Remaining": "359"})
        responses_lib.add(responses_lib.GET, FAULTS_PAGE_2_URL, json=_FAULTS_PAGE_2, status=200)

        honeybadger.collect_pages(
            f"projects/{PROJECT_ID}/faults",
            query={"q": "is:unresolved", "order": "recent"},
        )

        self.assertEqual(len(responses_lib.calls), 2)
        # Page 1 — initial request.
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], expected_header)
        # Page 2 — link-follow request must also carry the credential.
        self.assertEqual(responses_lib.calls[1].request.headers["Authorization"], expected_header)


class HoneybadgerPick(unittest.TestCase):
    """api.pick prunes fault objects to the support-relevant fields."""

    def test_pick_selects_support_fields(self):
        fault = _FAULTS_PAGE_2["results"][0]
        picked = api.pick(fault, "id,klass,message,environment,last_notice_at,notices_count,url")
        self.assertEqual(picked["id"], 1002)
        self.assertEqual(picked["klass"], "ActiveRecord::RecordNotFound")
        self.assertEqual(picked["environment"], "production")
        self.assertIn("notices_count", picked)
        # Non-support fields excluded.
        self.assertNotIn("assignee", picked)
        self.assertNotIn("created_at", picked)
        self.assertNotIn("component", picked)

    def test_pick_selects_deploy_fields(self):
        deploy = _DEPLOYS_PAGE_1["results"][0]
        picked = api.pick(deploy, "created_at,environment,revision,local_username")
        self.assertEqual(picked["revision"], "a1b2c3d")
        self.assertEqual(picked["environment"], "production")
        self.assertNotIn("repository", picked)
        self.assertNotIn("project_id", picked)


class HoneybadgerCLI(unittest.TestCase):
    """Script CLI (main()) drives collect_pages() end-to-end."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_HONEYBADGER")
        os.environ["RC_CONN_HONEYBADGER"] = "hbp_test" + "_token_abc123"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_HONEYBADGER", None)
        else:
            os.environ["RC_CONN_HONEYBADGER"] = self._saved

    @responses_lib.activate
    def test_faults_command_paginates_and_prints(self):
        responses_lib.add(responses_lib.GET, FAULTS_URL, json=_FAULTS_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, FAULTS_PAGE_2_URL, json=_FAULTS_PAGE_2, status=200)

        rc = honeybadger.main([
            "faults", str(PROJECT_ID),
            "--query", "q=is:unresolved",
            "--query", "order=recent",
            "--pick", "id,klass,environment",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)
        self.assertTrue(responses_lib.calls[0].request.url.startswith(FAULTS_URL))

    @responses_lib.activate
    def test_deploys_command_single_page(self):
        responses_lib.add(responses_lib.GET, DEPLOYS_URL, json=_DEPLOYS_PAGE_1, status=200)

        rc = honeybadger.main([
            "deploys", str(PROJECT_ID),
            "--query", "environment=production",
            "--pick", "created_at,revision",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_projects_command(self):
        responses_lib.add(responses_lib.GET, PROJECTS_URL, json=_PROJECTS_PAGE_1, status=200)

        rc = honeybadger.main([
            "projects",
            "--pick", "id,name,unresolved_fault_count",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)


class HoneybadgerCassetteHygiene(unittest.TestCase):
    """CI guard: no real Honeybadger token literals may land in the committed connector files.

    Scoped to the connector dir only — this test file legitimately names the prefixes it hunts for,
    so scanning itself would be a false positive.
    """

    # Honeybadger uses opaque personal auth tokens with no well-known prefix.
    # We guard against the fake test token pattern leaking into connector files.
    # Split across concatenation so the guard doesn't flag this test file itself.
    _TOKEN_PREFIXES = ("hbp_test" "_",)

    def test_no_token_prefixes_in_honeybadger_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "honeybadger"
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
