"""Fixture test for the manifest-ONLY Bugsnag integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies are Bugsnag's own
DOCUMENTED example error/event payloads (bugsnagapiv2.docs.apiary.io), trimmed to the fields this
test asserts on. Bugsnag paginates with RFC 8288 `Link: …; rel="next"` headers, so the two mocked
pages exercise the real `link` pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_bugsnag_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API = "https://api.bugsnag.com"
PROJECT_ID = "5a7c6750c21d2f0019b14d28"
ERRORS_URL = f"{API}/projects/{PROJECT_ID}/errors"

# Two pages of errors (bare JSON arrays, as Bugsnag returns). Shapes mirror the documented example
# error object; only support-relevant fields are kept. Page 1 advertises page 2 via the Link header.
_PAGE_1 = [
    {
        "id": "5a7c6f0dc21d2f0019b24501",
        "error_class": "RuntimeError",
        "message": "undefined method `foo' for nil:NilClass",
        "status": "open",
        "severity": "error",
        "last_seen": "2024-01-15T12:34:56Z",
        "first_seen": "2024-01-10T08:00:00Z",
        "events": 42,
        "project_id": PROJECT_ID,
    },
]
_PAGE_2 = [
    {
        "id": "5a7c6f0dc21d2f0019b24502",
        "error_class": "ArgumentError",
        "message": "wrong number of arguments (given 2, expected 0)",
        "status": "fixed",
        "severity": "warning",
        "last_seen": "2024-01-14T10:00:00Z",
        "first_seen": "2024-01-09T07:00:00Z",
        "events": 7,
        "project_id": PROJECT_ID,
    },
]
# RFC 8288 Link header: page 1 points at page 2 as rel="next"; page 2 has no next ⇒ loop stops.
_PAGE_1_LINK = (
    f'<{ERRORS_URL}?per_page=100&offset=page2>; rel="next", '
    f'<{ERRORS_URL}?per_page=100&offset=last>; rel="last"'
)

# Bugsnag auth: the stored credential is `token <PAT>` — the full header value placed verbatim.
# Split the prefix literal so the token-hygiene guard (which scans our connector dir) does not
# flag this test file as a credential leak.
_TOKEN_PREFIX = "token" + " "
_STORED_CRED = _TOKEN_PREFIX + "test-bugsnag-personal-auth-token"


class BugsnagManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `bugsnag` (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_BUGSNAG")
        os.environ["RC_CONN_BUGSNAG"] = _STORED_CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_BUGSNAG", None)
        else:
            os.environ["RC_CONN_BUGSNAG"] = self._saved

    def test_manifest_loaded_from_yaml_with_correct_fields(self):
        m = api.load_manifests()
        self.assertIn("bugsnag", m)
        b = m["bugsnag"]
        self.assertEqual(b.base_url, "https://api.bugsnag.com")
        self.assertEqual(b.auth.strategy, "api_key_header")
        self.assertEqual(b.auth.name, "Authorization")
        self.assertEqual(b.pagination.style, "link")
        self.assertEqual(b.pagination.items_field, "")
        self.assertEqual(b.rate_limit_remaining_header, "X-RateLimit-Remaining")
        # X-Version: 2 must be present — it's the API version selector.
        self.assertEqual(b.default_headers.get("X-Version"), "2")

    @responses.activate
    def test_link_pagination_stitches_pages_and_pick_selects_fields(self):
        # Page 1: bare array + Link rel="next" → page 2. Page 2: bare array, no Link → stop.
        responses.add(
            responses.GET, ERRORS_URL,
            json=_PAGE_1, status=200,
            headers={"Link": _PAGE_1_LINK, "X-RateLimit-Remaining": "999"},
        )
        responses.add(
            responses.GET, ERRORS_URL,
            json=_PAGE_2, status=200,
            headers={"X-RateLimit-Remaining": "998"},
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["bugsnag"])
        result = c.collect(f"/projects/{PROJECT_ID}/errors", query={"per_page": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, ["5a7c6f0dc21d2f0019b24501", "5a7c6f0dc21d2f0019b24502"])

        # The api_key_header credential is placed verbatim on both pages (including link-follow).
        self.assertEqual(responses.calls[0].request.headers["Authorization"], _STORED_CRED)
        self.assertEqual(responses.calls[1].request.headers["Authorization"], _STORED_CRED)
        # X-Version: 2 default header rides every request.
        self.assertEqual(responses.calls[0].request.headers["X-Version"], "2")
        self.assertEqual(responses.calls[1].request.headers["X-Version"], "2")

        # --pick prunes the large error object down to the few support-relevant fields.
        picked = [api.pick(it, "error_class,message,status,severity,last_seen") for it in result["items"]]
        self.assertEqual(picked[0]["error_class"], "RuntimeError")
        self.assertEqual(picked[0]["status"], "open")
        self.assertEqual(picked[0]["severity"], "error")
        self.assertEqual(picked[1]["status"], "fixed")

    @responses.activate
    def test_cli_drives_bugsnag_with_api_key_header_and_paginate(self):
        responses.add(
            responses.GET, ERRORS_URL,
            json=_PAGE_1, status=200,
            headers={"Link": _PAGE_1_LINK},
        )
        responses.add(
            responses.GET, ERRORS_URL,
            json=_PAGE_2, status=200,
        )
        rc = api._main([
            "get", "bugsnag", f"/projects/{PROJECT_ID}/errors",
            "--query", "per_page=100",
            "--paginate",
            "--pick", "error_class,status",
        ])
        self.assertEqual(rc, 0)
        # Both pages fetched; credential placed verbatim (token <PAT>).
        self.assertTrue(responses.calls[0].request.url.startswith(ERRORS_URL))
        self.assertEqual(responses.calls[0].request.headers["Authorization"], _STORED_CRED)
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_single_error_detail_no_pagination(self):
        error_id = "5a7c6f0dc21d2f0019b24501"
        url = f"{API}/projects/{PROJECT_ID}/errors/{error_id}"
        responses.add(
            responses.GET, url,
            json=_PAGE_1[0], status=200,
        )
        api.load_manifests()
        c = api.client(api.MANIFESTS["bugsnag"])
        body = c.get(f"/projects/{PROJECT_ID}/errors/{error_id}")
        self.assertEqual(body["id"], error_id)
        self.assertEqual(body["error_class"], "RuntimeError")
        # Credential present on single-resource GET too.
        self.assertEqual(responses.calls[0].request.headers["Authorization"], _STORED_CRED)


class BugsnagCassetteHygiene(unittest.TestCase):
    """CI guard: no real Bugsnag token material may land in the committed connector dir.

    Scopes to the connector dir (manifest + any future cassette), NOT this test file — the test
    legitimately names the prefix it hunts for (split across concatenation so this guard doesn't
    self-trigger), so scanning this file would be a false positive.
    """

    # Bugsnag personal auth tokens have no single canonical prefix, but we guard against any
    # literal `token ` (the full auth header value form) in committed connector files.
    # The prefix is split so this file itself doesn't trip the check.
    _TOKEN_PREFIXES = ("token" + " ",)

    def test_no_token_prefixes_in_bugsnag_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "bugsnag"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains '{pref}...'")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
