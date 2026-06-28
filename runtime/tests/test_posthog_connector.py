"""Fixture tests for the PostHog integration — manifest-only, driven via the generic lib.api path.

There is no PostHog Python module anymore: the integration is the manifest row. lib.api's
``body_url`` pagination style follows the body ``next`` URL ({"next": "<absolute url>", "results":
[...]}, null when exhausted) all by itself, with auth riding every request.

No live creds, no network: HTTP is mocked with ``responses``. Bodies mirror PostHog's documented
paginated envelope (count / next / previous / results).

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_posthog_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://us.posthog.com"
PROJECT_ID = "12345"
PERSONS_PATH = f"api/projects/{PROJECT_ID}/persons"
FLAGS_PATH = f"api/projects/{PROJECT_ID}/feature_flags"
PERSONS_URL = f"{BASE}/{PERSONS_PATH}"
FLAGS_URL = f"{BASE}/{FLAGS_PATH}"

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
            "properties": {"email": "alice@example.com", "plan": "pro", "$os": "Mac OS X"},
            "created_at": "2024-01-15T09:00:00Z",
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
            "properties": {"email": "bob@example.com", "plan": "free", "$os": "Windows"},
            "created_at": "2024-02-10T10:00:00Z",
        }
    ],
}

_FLAGS_PAGE_1 = {
    "count": 2,
    "next": f"{FLAGS_URL}?limit=100&offset=1",
    "previous": None,
    "results": [
        {"id": 101, "key": "dark-mode", "name": "Dark mode UI", "active": True,
         "filters": {"rollout_percentage": 50}},
    ],
}

_FLAGS_PAGE_2 = {
    "count": 2,
    "next": None,
    "previous": f"{FLAGS_URL}?limit=100&offset=0",
    "results": [
        {"id": 102, "key": "new-onboarding", "name": "New onboarding flow", "active": False,
         "filters": {"rollout_percentage": 0}},
    ],
}


class _PosthogBase(unittest.TestCase):
    def setUp(self):
        # YAML loader is the sole source of truth each test.
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


# -- manifest loading -------------------------------------------------------

class TestPosthogManifest(_PosthogBase):
    def test_manifest_loads_from_yaml_and_maps_every_field(self):
        m = api.load_manifests()
        self.assertIn("posthog", m)
        pg = m["posthog"]
        self.assertEqual(pg.base_url, "https://us.posthog.com")
        self.assertEqual(pg.auth.strategy, "bearer")
        self.assertEqual(pg.pagination.style, "body_url")
        self.assertEqual(pg.pagination.next_url_field, "next")
        self.assertEqual(pg.pagination.items_field, "results")
        self.assertEqual(pg.pagination.page_size, 100)
        self.assertEqual(pg.rate_limit_remaining_header, "")


# -- body_url pagination (force-code trigger d, now generic) ----------------

class TestPosthogPagination(_PosthogBase):
    @responses.activate
    def test_collect_stitches_two_pages_via_json_body_next(self):
        """collect() follows the JSON-body `next` URL for page 2, returns all items in order."""
        responses.add(responses.GET, PERSONS_URL, json=_PERSONS_PAGE_1, status=200)
        responses.add(
            responses.GET, f"{PERSONS_URL}?limit=100&offset=1",
            json=_PERSONS_PAGE_2, status=200,
        )

        m = api.load_manifests()["posthog"]
        result = api.client(m, token_key="posthog").collect(
            PERSONS_PATH, query={"limit": 100, "search": "example.com"},
        )

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual([it["id"] for it in result["items"]], [1, 2])
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_bearer_credential_on_every_request_including_continuation(self):
        """Bearer token must ride the initial request AND the JSON-body next-URL follow."""
        responses.add(responses.GET, PERSONS_URL, json=_PERSONS_PAGE_1, status=200)
        responses.add(
            responses.GET, f"{PERSONS_URL}?limit=100&offset=1",
            json=_PERSONS_PAGE_2, status=200,
        )

        m = api.load_manifests()["posthog"]
        api.client(m, token_key="posthog").collect(PERSONS_PATH, query={"limit": 100})

        self.assertEqual(len(responses.calls), 2)
        for call in responses.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(auth.startswith("Bearer phx"),
                            f"expected Bearer phx… on every request, got: {auth!r}")

    @responses.activate
    def test_single_page_when_next_is_null(self):
        """When next=None, collect stops after one page."""
        single = dict(_PERSONS_PAGE_1, next=None)
        responses.add(responses.GET, PERSONS_URL, json=single, status=200)

        m = api.load_manifests()["posthog"]
        result = api.client(m, token_key="posthog").collect(PERSONS_PATH)

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_feature_flags_two_pages(self):
        responses.add(responses.GET, FLAGS_URL, json=_FLAGS_PAGE_1, status=200)
        responses.add(
            responses.GET, f"{FLAGS_URL}?limit=100&offset=1",
            json=_FLAGS_PAGE_2, status=200,
        )

        m = api.load_manifests()["posthog"]
        result = api.client(m, token_key="posthog").collect(FLAGS_PATH, query={"limit": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual([it["key"] for it in result["items"]], ["dark-mode", "new-onboarding"])


# -- field pre-selection ----------------------------------------------------

class TestPosthogPick(_PosthogBase):
    @responses.activate
    def test_pick_selects_support_fields_from_persons(self):
        single = dict(_PERSONS_PAGE_1, next=None)
        responses.add(responses.GET, PERSONS_URL, json=single, status=200)

        m = api.load_manifests()["posthog"]
        result = api.client(m, token_key="posthog").collect(PERSONS_PATH)
        picked = [api.pick(it, "id,distinct_ids,properties.email,properties.plan,created_at")
                  for it in result["items"]]
        self.assertEqual(picked[0]["id"], 1)
        self.assertEqual(picked[0]["properties.email"], "alice@example.com")
        self.assertEqual(picked[0]["properties.plan"], "pro")
        self.assertIn("alice@example.com", picked[0]["distinct_ids"])


# -- generic CLI ------------------------------------------------------------

class TestPosthogCLI(_PosthogBase):
    @responses.activate
    def test_cli_persons_paginate(self):
        """`get posthog … --paginate --pick` stitches pages, auth on both."""
        responses.add(responses.GET, PERSONS_URL, json=_PERSONS_PAGE_1, status=200)
        responses.add(
            responses.GET, f"{PERSONS_URL}?limit=100&offset=1",
            json=_PERSONS_PAGE_2, status=200,
        )

        rc = api._main([
            "get", "posthog", PERSONS_PATH, "--paginate",
            "--query", "limit=100", "--pick", "id,properties.email",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses.calls), 2)
        for call in responses.calls:
            self.assertTrue(call.request.headers.get("Authorization", "").startswith("Bearer phx"))

    @responses.activate
    def test_cli_single_item_get(self):
        """Single-item GET (no --paginate) hits the path directly with bearer auth."""
        person_url = f"{PERSONS_URL}/1/"
        responses.add(responses.GET, person_url, json=_PERSONS_PAGE_1["results"][0], status=200)

        rc = api._main(["get", "posthog", f"{PERSONS_PATH}/1/"])
        self.assertEqual(rc, 0)
        auth = responses.calls[0].request.headers.get("Authorization", "")
        self.assertTrue(auth.startswith("Bearer phx"))


# -- token-prefix hygiene ---------------------------------------------------

class TestPosthogHygiene(unittest.TestCase):
    """CI guard: no real PostHog personal API key prefix may land in the connector dir files.

    Scoped to the connector dir (only manifest.yaml remains) — this test file legitimately names
    the prefix it hunts, so scanning itself would be a false positive.
    """

    # PostHog personal API key prefix is "phx_". Split to avoid tripping our own guard.
    _TOKEN_PREFIXES = ("phx" "_",)

    def test_no_token_prefixes_in_posthog_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "posthog"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file() or path.suffix == ".pyc":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
