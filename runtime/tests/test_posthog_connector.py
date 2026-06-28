"""Fixture test for the PostHog script connector — proves force-code trigger (d) is handled:
PostHog embeds the next-page URL in the JSON body ({"next": "<url>", "results": [...]}), not
in an HTTP Link header; the connector's collect_pages() follows it with _send_url so auth rides
on every request.

No live creds, no network: HTTP is mocked with `responses`. Bodies mirror PostHog's documented
paginated envelope (count / next / previous / results). Two-page tests exercise the JSON-body
next-URL follow path end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_posthog_connector.py -q
"""

import io
import json
import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import posthog as ph  # noqa: E402

BASE = "https://us.posthog.com"
PROJECT_ID = "12345"
PERSONS_URL = f"{BASE}/api/projects/{PROJECT_ID}/persons"
FLAGS_URL = f"{BASE}/api/projects/{PROJECT_ID}/feature_flags"
RECORDINGS_URL = f"{BASE}/api/projects/{PROJECT_ID}/session_recordings"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields).
# PostHog envelope: {"count": N, "next": "<url or null>", "previous": "…", "results": [...]}
# ---------------------------------------------------------------------------

_PERSONS_PAGE_1 = {
    "count": 2,
    "next": f"{PERSONS_URL}?limit=100&offset=1",
    "previous": None,
    "results": [
        {
            "id": 1,
            "uuid": "017c0b29-9ee3-0000-0000-000000000001",
            "name": "alice@example.com",
            "distinct_ids": ["alice@example.com", "anon_abc123"],
            "properties": {
                "email": "alice@example.com",
                "plan": "pro",
                "$os": "Mac OS X",
            },
            "created_at": "2024-01-15T09:00:00Z",
            "last_seen_at": "2024-06-01T12:00:00Z",
        }
    ],
}

_PERSONS_PAGE_2 = {
    "count": 2,
    "next": None,
    "previous": f"{PERSONS_URL}?limit=100&offset=0",
    "results": [
        {
            "id": 2,
            "uuid": "017c0b29-9ee3-0000-0000-000000000002",
            "name": "bob@example.com",
            "distinct_ids": ["bob@example.com"],
            "properties": {
                "email": "bob@example.com",
                "plan": "free",
                "$os": "Windows",
            },
            "created_at": "2024-02-10T10:00:00Z",
            "last_seen_at": "2024-05-28T08:30:00Z",
        }
    ],
}

_FLAGS_PAGE_1 = {
    "count": 2,
    "next": f"{FLAGS_URL}?limit=100&offset=1",
    "previous": None,
    "results": [
        {
            "id": 101,
            "key": "dark-mode",
            "name": "Dark mode UI",
            "active": True,
            "archived": False,
            "created_at": "2024-03-01T00:00:00Z",
            "filters": {"rollout_percentage": 50},
        }
    ],
}

_FLAGS_PAGE_2 = {
    "count": 2,
    "next": None,
    "previous": f"{FLAGS_URL}?limit=100&offset=0",
    "results": [
        {
            "id": 102,
            "key": "new-onboarding",
            "name": "New onboarding flow",
            "active": False,
            "archived": False,
            "created_at": "2024-04-10T00:00:00Z",
            "filters": {"rollout_percentage": 0},
        }
    ],
}


class PosthogConnector(unittest.TestCase):
    def setUp(self):
        # Clean registry so YAML loader / register() is the sole source of truth each test.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_POSTHOG")
        # Split prefix literal so the token-hygiene guard doesn't flag this test file.
        os.environ["RC_CONN_POSTHOG"] = "phx" "_test_personal_api_key_for_testing"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_POSTHOG", None)
        else:
            os.environ["RC_CONN_POSTHOG"] = self._saved

    # -- manifest loading ---------------------------------------------------

    def test_manifest_loads_from_yaml_and_matches_registered(self):
        """YAML manifest loads cleanly and maps every field correctly."""
        m = api.load_manifests()
        self.assertIn("posthog", m)
        pg = m["posthog"]
        self.assertEqual(pg.base_url, "https://us.posthog.com")
        self.assertEqual(pg.auth.strategy, "bearer")
        # The script connector registers with style=none (it owns the loop).
        self.assertEqual(pg.pagination.style, "none")
        self.assertEqual(pg.pagination.items_field, "results")
        self.assertEqual(pg.rate_limit_remaining_header, "")

    def test_script_register_wins_over_yaml(self):
        """Explicit register() from the script module takes priority over YAML load.

        setUp clears MANIFESTS so we simulate a fresh state. Re-registering MANIFEST (as module
        import would do) and then calling load_manifests() must leave the Python-registered version
        intact (load_manifests is idempotent for explicitly registered keys).
        """
        # Simulate the connector module re-registering its manifest after setUp cleared the dict.
        api.register(ph.MANIFEST)
        self.assertIn("posthog", api.MANIFESTS)
        self.assertNotIn("posthog", api._YAML_LOADED_KEYS)  # explicit register, not YAML-owned

        # load_manifests must NOT clobber an explicitly registered key.
        api.load_manifests()
        self.assertIn("posthog", api.MANIFESTS)
        self.assertNotIn("posthog", api._YAML_LOADED_KEYS)
        self.assertEqual(api.MANIFESTS["posthog"].base_url, "https://us.posthog.com")

    # -- JSON-body next-URL pagination (force-code trigger d) ---------------

    @responses.activate
    def test_collect_pages_stitches_two_pages_via_json_body_next(self):
        """collect_pages() follows the JSON-body `next` URL for page 2, returns all items."""
        responses.add(responses.GET, PERSONS_URL, json=_PERSONS_PAGE_1, status=200)
        # lib.api follows the opaque next URL verbatim — register it as an exact URL.
        responses.add(
            responses.GET,
            f"{PERSONS_URL}?limit=100&offset=1",
            json=_PERSONS_PAGE_2,
            status=200,
        )

        result = ph.collect_pages(
            f"api/projects/{PROJECT_ID}/persons",
            query={"limit": 100, "search": "example.com"},
        )

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, [1, 2])  # both pages stitched, in order

    @responses.activate
    def test_bearer_credential_on_every_request_including_json_next_follow(self):
        """Bearer token must ride on the initial request AND the JSON-body next-URL follow."""
        responses.add(responses.GET, PERSONS_URL, json=_PERSONS_PAGE_1, status=200)
        responses.add(
            responses.GET,
            f"{PERSONS_URL}?limit=100&offset=1",
            json=_PERSONS_PAGE_2,
            status=200,
        )

        ph.collect_pages(f"api/projects/{PROJECT_ID}/persons", query={"limit": 100})

        self.assertEqual(len(responses.calls), 2)
        for call in responses.calls:
            auth_header = call.request.headers.get("Authorization", "")
            self.assertTrue(
                auth_header.startswith("Bearer phx"),
                f"expected Bearer phx… on every request, got: {auth_header!r}",
            )

    @responses.activate
    def test_single_page_when_next_is_null(self):
        """When next=None (or absent), collect_pages stops after one page."""
        single = dict(_PERSONS_PAGE_1, next=None)
        responses.add(responses.GET, PERSONS_URL, json=single, status=200)

        result = ph.collect_pages(f"api/projects/{PROJECT_ID}/persons")

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_collect_pages_feature_flags_two_pages(self):
        responses.add(responses.GET, FLAGS_URL, json=_FLAGS_PAGE_1, status=200)
        responses.add(
            responses.GET,
            f"{FLAGS_URL}?limit=100&offset=1",
            json=_FLAGS_PAGE_2,
            status=200,
        )

        result = ph.collect_pages(f"api/projects/{PROJECT_ID}/feature_flags", query={"limit": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        keys = [it["key"] for it in result["items"]]
        self.assertEqual(keys, ["dark-mode", "new-onboarding"])

    # -- field pre-selection ------------------------------------------------

    @responses.activate
    def test_pick_selects_support_fields_from_persons(self):
        single = dict(_PERSONS_PAGE_1, next=None)
        responses.add(responses.GET, PERSONS_URL, json=single, status=200)

        result = ph.collect_pages(f"api/projects/{PROJECT_ID}/persons")
        picked = [
            api.pick(it, "id,distinct_ids,properties.email,properties.plan,created_at")
            for it in result["items"]
        ]
        self.assertEqual(picked[0]["id"], 1)
        self.assertEqual(picked[0]["properties.email"], "alice@example.com")
        self.assertEqual(picked[0]["properties.plan"], "pro")
        self.assertIn("alice@example.com", picked[0]["distinct_ids"])

    @responses.activate
    def test_pick_selects_flag_fields(self):
        single_flags = dict(_FLAGS_PAGE_1, next=None)
        responses.add(responses.GET, FLAGS_URL, json=single_flags, status=200)

        result = ph.collect_pages(f"api/projects/{PROJECT_ID}/feature_flags")
        picked = [api.pick(it, "id,key,name,active") for it in result["items"]]
        self.assertEqual(picked[0]["key"], "dark-mode")
        self.assertTrue(picked[0]["active"])

    # -- single-item GET (no pagination needed) -----------------------------

    @responses.activate
    def test_single_person_get_via_lib_api_client(self):
        """Single-item GETs bypass collect_pages; lib.api client handles them directly."""
        person_url = f"{PERSONS_URL}/1/"
        responses.add(
            responses.GET,
            person_url,
            json=_PERSONS_PAGE_1["results"][0],
            status=200,
        )

        c = ph._client()
        body = c.get(f"api/projects/{PROJECT_ID}/persons/1/")

        self.assertEqual(body["id"], 1)
        self.assertEqual(body["distinct_ids"], ["alice@example.com", "anon_abc123"])
        auth = responses.calls[0].request.headers["Authorization"]
        self.assertTrue(auth.startswith("Bearer phx"))

    # -- CLI ----------------------------------------------------------------

    @responses.activate
    def test_cli_persons_paginate(self):
        responses.add(responses.GET, PERSONS_URL, json=_PERSONS_PAGE_1, status=200)
        responses.add(
            responses.GET,
            f"{PERSONS_URL}?limit=100&offset=1",
            json=_PERSONS_PAGE_2,
            status=200,
        )

        captured = io.StringIO()
        import sys
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = ph.main([
                "persons", PROJECT_ID,
                "--query", "limit=100",
                "--pick", "id,properties.email",
            ])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        out = json.loads(captured.getvalue())
        self.assertFalse(out["incomplete"])
        self.assertEqual(len(out["items"]), 2)
        self.assertEqual(out["items"][0]["id"], 1)
        self.assertEqual(out["items"][0]["properties.email"], "alice@example.com")
        # Both pages fetched, auth on both.
        self.assertEqual(len(responses.calls), 2)
        self.assertTrue(
            responses.calls[0].request.headers["Authorization"].startswith("Bearer phx")
        )

    @responses.activate
    def test_cli_feature_flags(self):
        single_flags = dict(_FLAGS_PAGE_1, next=None)
        responses.add(responses.GET, FLAGS_URL, json=single_flags, status=200)

        captured = io.StringIO()
        import sys
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = ph.main(["feature-flags", PROJECT_ID, "--pick", "key,active"])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        out = json.loads(captured.getvalue())
        self.assertEqual(out["items"][0]["key"], "dark-mode")


class PosthogCassetteHygiene(unittest.TestCase):
    """CI guard: no real PostHog personal API key prefix may land in connector dir files.

    Scopes to the connector directory ONLY — this test file legitimately names the prefix it hunts,
    so scanning itself would be a false positive.
    """

    # PostHog personal API key prefix is "phx_". Split to avoid tripping our own guard.
    _TOKEN_PREFIXES = ("phx" "_",)

    def test_no_token_prefixes_in_posthog_connector_files(self):
        connector_dir = (
            Path(__file__).resolve().parents[1] / "lib" / "connectors" / "posthog"
        )
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
