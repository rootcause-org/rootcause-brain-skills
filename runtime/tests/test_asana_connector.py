"""Fixture test for the manifest-ONLY Asana integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies mirror Asana's
documented example payloads (developers.asana.com/docs/pagination), trimmed to support-relevant
fields. Asana paginates with opaque cursor tokens: `next_page.offset` in the response body, sent
back as the `offset` query param; `next_page` being null/absent signals exhaustion.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_asana_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://app.asana.com/api/1.0"
PROJECT_TASKS_URL = f"{BASE}/projects/1337/tasks"
TASK_URL = f"{BASE}/tasks/123456"
STORIES_URL = f"{BASE}/tasks/123456/stories"

# Two pages of tasks. Shapes mirror Asana's documented compact task records; only
# support-relevant fields are kept. Page 1 includes next_page pointing to page 2.
_TASKS_PAGE_1 = {
    "data": [
        {
            "gid": "123456",
            "name": "Fix login bug",
            "completed": False,
            "assignee": {"gid": "7890", "name": "Alice"},
            "due_on": "2026-07-15",
        },
    ],
    "next_page": {
        "offset": "yJ0eXAiOiJKV1QiLCJhbGciOiJIRzI1NiJ9",
        "path": "/projects/1337/tasks?limit=100&offset=yJ0eXAiOiJKV1QiLCJhbGciOiJIRzI1NiJ9",
        "uri": f"{PROJECT_TASKS_URL}?limit=100&offset=yJ0eXAiOiJKV1QiLCJhbGciOiJIRzI1NiJ9",
    },
}
_TASKS_PAGE_2 = {
    "data": [
        {
            "gid": "789012",
            "name": "Update documentation",
            "completed": True,
            "assignee": {"gid": "7890", "name": "Alice"},
            "due_on": "2026-06-30",
        },
    ],
    "next_page": None,  # null ⇒ last page
}

# Single task detail response (Asana wraps single resources in `data` too).
_TASK_DETAIL = {
    "data": {
        "gid": "123456",
        "name": "Fix login bug",
        "notes": "Users report login fails on mobile app version 3.2",
        "completed": False,
        "due_on": "2026-07-15",
        "assignee": {"gid": "7890", "name": "Alice"},
        "projects": [{"gid": "1337", "name": "Support Q3"}],
    }
}

# One page of stories (comments/activity) for a task.
_STORIES_PAGE = {
    "data": [
        {
            "gid": "55001",
            "type": "comment",
            "text": "Reproduced on Android 14. Assigning to mobile team.",
            "created_by": {"gid": "7890", "name": "Alice"},
            "created_at": "2026-06-28T10:30:00.000Z",
        },
        {
            "gid": "55002",
            "type": "system",
            "text": "Alice assigned to Bob",
            "created_by": {"gid": "7890", "name": "Alice"},
            "created_at": "2026-06-28T10:31:00.000Z",
        },
    ],
    "next_page": None,
}

# Split the PAT prefix so the token-hygiene guard (which scans connector dir files) doesn't
# flag this test file itself as a leak — the guard is scoped to the connector dir, not here.
_FAKE_TOKEN = "Bearer " + "0/" + "fake_asana_personal_access_token"
_RAW_TOKEN = "0/" + "fake_asana_personal_access_token"


class AsanaManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `asana` (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_ASANA")
        os.environ["RC_CONN_ASANA"] = _RAW_TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_ASANA", None)
        else:
            os.environ["RC_CONN_ASANA"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        """YAML loader picks up asana and maps every lib.api-relevant field correctly."""
        m = api.load_manifests()
        self.assertIn("asana", m)
        a = m["asana"]
        self.assertEqual(a.base_url, "https://app.asana.com/api/1.0")
        self.assertEqual(a.auth.strategy, "bearer")
        # Cursor pagination fields match Asana's documented envelope.
        self.assertEqual(a.pagination.style, "cursor")
        self.assertEqual(a.pagination.cursor_field, "next_page.offset")
        self.assertEqual(a.pagination.cursor_param, "offset")
        self.assertEqual(a.pagination.has_more_field, "next_page")
        self.assertEqual(a.pagination.items_field, "data")
        self.assertEqual(a.pagination.page_size, 100)
        # No remaining-count header on Asana.
        self.assertEqual(a.rate_limit_remaining_header, "")

    @responses_lib.activate
    def test_cursor_pagination_stitches_two_pages(self):
        """Asana cursor pagination: page 1 has next_page.offset; page 2 has next_page=null → stop."""
        responses_lib.add(
            responses_lib.GET, PROJECT_TASKS_URL,
            json=_TASKS_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.GET, PROJECT_TASKS_URL,
            json=_TASKS_PAGE_2, status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["asana"])
        result = c.collect("projects/1337/tasks", query={"limit": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        names = [it["name"] for it in result["items"]]
        self.assertEqual(names, ["Fix login bug", "Update documentation"])

    @responses_lib.activate
    def test_bearer_credential_on_every_request_including_cursor_follow(self):
        """Bearer token must appear on page 1 AND the cursor-follow page 2 request."""
        responses_lib.add(
            responses_lib.GET, PROJECT_TASKS_URL,
            json=_TASKS_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.GET, PROJECT_TASKS_URL,
            json=_TASKS_PAGE_2, status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["asana"])
        c.collect("projects/1337/tasks", query={"limit": 100})

        for call in responses_lib.calls:
            self.assertEqual(
                call.request.headers["Authorization"],
                _FAKE_TOKEN,
                f"Missing bearer on request to {call.request.url}",
            )

    @responses_lib.activate
    def test_cursor_token_sent_as_offset_param_on_page_2(self):
        """The opaque cursor from next_page.offset is forwarded as ?offset=… on the next request."""
        responses_lib.add(
            responses_lib.GET, PROJECT_TASKS_URL,
            json=_TASKS_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.GET, PROJECT_TASKS_URL,
            json=_TASKS_PAGE_2, status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["asana"])
        c.collect("projects/1337/tasks", query={"limit": 100})

        # Second call must have the offset cursor in the URL query string.
        page2_url = responses_lib.calls[1].request.url
        self.assertIn("offset=yJ0eXAiOiJKV1QiLCJhbGciOiJIRzI1NiJ9", page2_url)

    @responses_lib.activate
    def test_single_page_get_task_detail(self):
        """Single-resource GET (task detail) — no pagination, bearer present, full body returned."""
        responses_lib.add(
            responses_lib.GET, TASK_URL,
            json=_TASK_DETAIL, status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["asana"])
        body = c.get("tasks/123456")

        self.assertEqual(body["data"]["gid"], "123456")
        self.assertEqual(body["data"]["name"], "Fix login bug")
        auth = responses_lib.calls[0].request.headers["Authorization"]
        self.assertEqual(auth, _FAKE_TOKEN)

    @responses_lib.activate
    def test_pick_selects_support_relevant_fields(self):
        """api.pick extracts support-relevant task fields correctly."""
        responses_lib.add(
            responses_lib.GET, PROJECT_TASKS_URL,
            json=_TASKS_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.GET, PROJECT_TASKS_URL,
            json=_TASKS_PAGE_2, status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["asana"])
        result = c.collect("projects/1337/tasks", query={"limit": 100})
        picked = [api.pick(it, "gid,name,completed,assignee.name,due_on") for it in result["items"]]

        self.assertEqual(picked[0]["gid"], "123456")
        self.assertEqual(picked[0]["name"], "Fix login bug")
        self.assertFalse(picked[0]["completed"])
        self.assertEqual(picked[0]["assignee.name"], "Alice")
        self.assertEqual(picked[0]["due_on"], "2026-07-15")
        # Second item (page 2)
        self.assertTrue(picked[1]["completed"])

    @responses_lib.activate
    def test_stories_pagination_single_page(self):
        """Stories endpoint: single page (next_page=None), bearer token present."""
        responses_lib.add(
            responses_lib.GET, STORIES_URL,
            json=_STORIES_PAGE, status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["asana"])
        result = c.collect("tasks/123456/stories", query={"limit": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["type"], "comment")
        self.assertEqual(result["items"][0]["created_by"]["name"], "Alice")
        auth = responses_lib.calls[0].request.headers["Authorization"]
        self.assertEqual(auth, _FAKE_TOKEN)

    @responses_lib.activate
    def test_cli_drives_asana_with_bearer_and_paginate(self):
        """CLI path: `python -m lib.api get asana … --paginate` resolves manifest + paginates."""
        responses_lib.add(
            responses_lib.GET, PROJECT_TASKS_URL,
            json=_TASKS_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.GET, PROJECT_TASKS_URL,
            json=_TASKS_PAGE_2, status=200,
        )

        rc = api._main([
            "get", "asana", "projects/1337/tasks",
            "--query", "limit=100",
            "--paginate",
            "--pick", "gid,name",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(responses_lib.calls[0].request.url.startswith(PROJECT_TASKS_URL))
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], _FAKE_TOKEN)
        self.assertEqual(len(responses_lib.calls), 2)


class AsanaCassetteHygiene(unittest.TestCase):
    """CI guard: no real Asana PAT prefix may land in the committed connector dir.

    Scoped to the connector dir (manifest + any future cassette), NOT this test file —
    the test legitimately mentions the prefix strings it hunts for (split to avoid
    self-detection), so scanning itself would be a false positive.
    """

    # Asana PAT format: 0/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx (32 hex chars after the slash).
    # Split the prefix literal so the guard doesn't flag itself.
    _TOKEN_PREFIXES = ("0" "/",)

    def test_no_token_prefixes_in_asana_connector_dir(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "asana"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present in connector dir: {offenders}")


if __name__ == "__main__":
    unittest.main()
