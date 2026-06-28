"""Tests for the Honeybadger integration (manifest-only, driven via lib.api).

Honeybadger is a manifest-only integration: there is no per-key Python connector. lib.api's
``body_url`` pagination style follows the next-page URL embedded in the JSON body at ``links.next``
(a full absolute URL) and stops when it is absent. Items live under ``results``. Auth is HTTP
Basic with the token as username and an empty password (lib.api encodes ``<token>:`` as Base64).
These tests drive the generic path:

  - the YAML manifest loads and maps every lib.api field (style=body_url, next_url_field,
    items_field, auth.strategy, base_url, page_size, rate-limit header);
  - ``client(m).collect()`` stitches ≥2 fixture pages in order following ``links.next``;
  - the Basic-auth credential rides EVERY request, including the continuation page;
  - ``api.pick`` selects the support-relevant fault fields;
  - token-prefix hygiene: no fake test token leaks into the connector dir.

No live creds, no network: HTTP is mocked with `responses`. Bodies match Honeybadger's documented
API response shapes (docs.honeybadger.io/api/).

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_honeybadger_connector.py -q
"""

import base64
import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://app.honeybadger.io/v2"
PROJECT_ID = 42
FAULTS_PATH = f"projects/{PROJECT_ID}/faults"   # relative — _join'd onto base_url for page 1
FAULTS_URL = f"{BASE}/projects/{PROJECT_ID}/faults"
FAULTS_PAGE_2_URL = f"{FAULTS_URL}?q=is%3Aunresolved&order=recent&page=2"
DEPLOYS_URL = f"{BASE}/projects/{PROJECT_ID}/deploys"

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
            "resolved": False,
            "url": "https://app.honeybadger.io/projects/42/faults/1001",
            "assignee": None,
            "component": "OrdersController",
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
            "resolved": False,
            "url": "https://app.honeybadger.io/projects/42/faults/1002",
            "assignee": None,
            "component": "UsersController",
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


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_HONEYBADGER")
        # Token only; no trailing colon — lib.api's basic strategy appends the empty password.
        os.environ["RC_CONN_HONEYBADGER"] = "hbp_test" + "_token_abc123"
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        api.load_manifests()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_HONEYBADGER", None)
        else:
            os.environ["RC_CONN_HONEYBADGER"] = self._saved


# ---------------------------------------------------------------------------
# 1. Manifest loads from YAML and maps every field
# ---------------------------------------------------------------------------

class HoneybadgerManifestLoad(_Base):
    def test_manifest_loaded_from_yaml(self):
        self.assertIn("honeybadger", api.MANIFESTS)
        hb = api.MANIFESTS["honeybadger"]
        self.assertEqual(hb.key, "honeybadger")
        self.assertEqual(hb.base_url, "https://app.honeybadger.io/v2")
        self.assertEqual(hb.auth.strategy, "basic")
        self.assertEqual(hb.pagination.style, "body_url")
        self.assertEqual(hb.pagination.next_url_field, "links.next")
        self.assertEqual(hb.pagination.items_field, "results")
        self.assertEqual(hb.pagination.page_size, 25)
        self.assertEqual(hb.rate_limit_remaining_header, "X-RateLimit-Remaining")


# ---------------------------------------------------------------------------
# 2. body_url pagination stitches ≥2 pages via links.next
# ---------------------------------------------------------------------------

class HoneybadgerPagination(_Base):
    @responses_lib.activate
    def test_two_pages_stitched_via_body_links_next(self):
        responses_lib.add(responses_lib.GET, FAULTS_URL, json=_FAULTS_PAGE_1, status=200,
                          headers={"X-RateLimit-Remaining": "359"})
        responses_lib.add(responses_lib.GET, FAULTS_PAGE_2_URL, json=_FAULTS_PAGE_2, status=200,
                          headers={"X-RateLimit-Remaining": "358"})

        m = api.MANIFESTS["honeybadger"]
        result = api.client(m, token_key="honeybadger").collect(
            FAULTS_PATH, query={"q": "is:unresolved", "order": "recent"},
        )

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual([it["id"] for it in result["items"]], [1001, 1002])  # both pages, in order
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_single_page_no_next_terminates(self):
        responses_lib.add(responses_lib.GET, DEPLOYS_URL, json=_DEPLOYS_PAGE_1, status=200)

        m = api.MANIFESTS["honeybadger"]
        result = api.client(m, token_key="honeybadger").collect(
            f"projects/{PROJECT_ID}/deploys", query={"environment": "production"},
        )

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["revision"], "a1b2c3d")
        self.assertEqual(len(responses_lib.calls), 1)


# ---------------------------------------------------------------------------
# 3. Basic auth must ride EVERY request, including link-follow continuation
# ---------------------------------------------------------------------------

class HoneybadgerAuth(_Base):
    @responses_lib.activate
    def test_basic_auth_on_every_request_including_continuation(self):
        token = os.environ["RC_CONN_HONEYBADGER"]
        # lib.api basic strategy encodes "<token>:" (empty password) as Base64.
        expected_b64 = base64.b64encode(f"{token}:".encode()).decode()
        expected_header = f"Basic {expected_b64}"

        responses_lib.add(responses_lib.GET, FAULTS_URL, json=_FAULTS_PAGE_1, status=200,
                          headers={"X-RateLimit-Remaining": "359"})
        responses_lib.add(responses_lib.GET, FAULTS_PAGE_2_URL, json=_FAULTS_PAGE_2, status=200)

        m = api.MANIFESTS["honeybadger"]
        api.client(m, token_key="honeybadger").collect(
            FAULTS_PATH, query={"q": "is:unresolved", "order": "recent"},
        )

        self.assertEqual(len(responses_lib.calls), 2)
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], expected_header)
        self.assertEqual(responses_lib.calls[1].request.headers["Authorization"], expected_header)

    @responses_lib.activate
    def test_lib_api_cli_drives_manifest(self):
        """`python -m lib.api get honeybadger projects/42/faults --paginate` works end-to-end."""
        token = os.environ["RC_CONN_HONEYBADGER"]
        expected_header = f"Basic {base64.b64encode(f'{token}:'.encode()).decode()}"
        responses_lib.add(responses_lib.GET, FAULTS_URL, json=_FAULTS_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, FAULTS_PAGE_2_URL, json=_FAULTS_PAGE_2, status=200)

        rc = api._main([
            "get", "honeybadger", FAULTS_PATH,
            "--query", "q=is:unresolved", "--query", "order=recent",
            "--paginate", "--pick", "id,klass,environment",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], expected_header)


# ---------------------------------------------------------------------------
# 4. api.pick prunes fault objects to support-relevant fields
# ---------------------------------------------------------------------------

class HoneybadgerPick(_Base):
    def test_pick_selects_support_fields(self):
        fault = _FAULTS_PAGE_2["results"][0]
        picked = api.pick(fault, "id,klass,message,environment,last_notice_at,notices_count,url")
        self.assertEqual(picked["id"], 1002)
        self.assertEqual(picked["klass"], "ActiveRecord::RecordNotFound")
        self.assertEqual(picked["environment"], "production")
        self.assertIn("notices_count", picked)
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


# ---------------------------------------------------------------------------
# 5. Token-prefix hygiene guard (scoped to connector dir)
# ---------------------------------------------------------------------------

class HoneybadgerHygiene(unittest.TestCase):
    """CI guard: no fake test token may land in the connector files (only manifest.yaml).

    Split across concatenation so the guard doesn't flag this test file itself.
    """

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
