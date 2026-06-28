"""Fixture test for the manifest-ONLY ClickUp integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. Bodies mirror ClickUp's documented
example payloads, trimmed to support-relevant fields.

ClickUp task lists page with a 0-based PAGE NUMBER (page=0,1,2,…), expressed by lib.api's `page`
style with page_start=0: the paginator increments the page number (NOT an item-count offset) and
stops on a short/empty page. The real manifest uses page_size=100; to exercise multi-page stitching
with small fixtures these tests drive a page_size-1 copy of the real manifest, while asserting the
real manifest fields and driving the real manifest for single-GET + CLI auth checks.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_clickup_connector.py -q
"""

import dataclasses
import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://api.clickup.com/api/v2"
LIST_ID = "12345678"
TEAM_ID = "9999001"
TASK_ID = "abc123"
TASKS_URL = f"{BASE}/list/{LIST_ID}/task"

# Credential rides VERBATIM (no "Bearer" prefix). Split so the hygiene guard can't flag this file.
_CRED = "pk" "_test_token_fixture_abc123"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# ---------------------------------------------------------------------------

_TASK_1 = {
    "id": "abc123",
    "name": "Login page broken after deploy",
    "status": {"status": "in progress"},
    "assignees": [{"username": "alice"}, {"username": "bob"}],
    "due_date": "1700000000000",
    "url": "https://app.clickup.com/t/abc123",
    "list": {"name": "Sprint 42"},
    "space": {"id": "space_001"},
    "priority": {"priority": "high"},
    "description": "Users cannot log in after the 2.3.1 deploy.",
    "creator": {"username": "carol"},
    "date_created": "1699000000000",
    "date_updated": "1700000000000",
}

_TASK_2 = {
    "id": "def456",
    "name": "Payment webhook not firing",
    "status": {"status": "open"},
    "assignees": [{"username": "dave"}],
    "due_date": None,
    "url": "https://app.clickup.com/t/def456",
    "list": {"name": "Sprint 42"},
    "space": {"id": "space_001"},
    "priority": {"priority": "urgent"},
    "description": "Stripe webhook missing for plan upgrades.",
    "creator": {"username": "carol"},
    "date_created": "1699100000000",
    "date_updated": "1699900000000",
}


class _ClickupBase(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_CLICKUP")
        os.environ["RC_CONN_CLICKUP"] = _CRED
        api.load_manifests()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_CLICKUP", None)
        else:
            os.environ["RC_CONN_CLICKUP"] = self._saved


# ---------------------------------------------------------------------------
# 1. Manifest loading
# ---------------------------------------------------------------------------

class TestClickupManifest(_ClickupBase):
    def test_yaml_loads_and_maps_every_field(self):
        self.assertIn("clickup", api.MANIFESTS)
        m = api.MANIFESTS["clickup"]
        self.assertEqual(m.key, "clickup")
        self.assertEqual(m.base_url, "https://api.clickup.com/api/v2")
        self.assertEqual(m.auth.strategy, "api_key_header")
        self.assertEqual(m.auth.name, "Authorization")
        self.assertEqual(m.pagination.style, "page")
        self.assertEqual(m.pagination.page_param, "page")
        self.assertEqual(m.pagination.page_start, 0)  # ClickUp is 0-based
        self.assertEqual(m.pagination.items_field, "tasks")
        self.assertEqual(m.pagination.page_size, 100)
        self.assertEqual(m.rate_limit_remaining_header, "X-RateLimit-Remaining")


# ---------------------------------------------------------------------------
# 2. page-number pagination (0-based) — stitches ≥2 pages, stops on a short page
# ---------------------------------------------------------------------------

class TestClickupPagination(_ClickupBase):
    def _client_small(self) -> api.Client:
        """Real manifest with page_size overridden to 1 so small fixtures exercise multi-page."""
        m = api.MANIFESTS["clickup"]
        m_small = dataclasses.replace(
            m, pagination=dataclasses.replace(m.pagination, page_size=1)
        )
        return api.client(m_small, token_key="clickup")

    @responses_lib.activate
    def test_two_pages_stitched_stops_on_short_page(self):
        """page=0 (full) then page=1 (short) → stop; both pages stitched in order."""
        responses_lib.add(responses_lib.GET, TASKS_URL, json={"tasks": [_TASK_1]}, status=200)
        responses_lib.add(responses_lib.GET, TASKS_URL, json={"tasks": [_TASK_2]}, status=200)
        responses_lib.add(responses_lib.GET, TASKS_URL, json={"tasks": []}, status=200)

        result = self._client_small().collect(f"list/{LIST_ID}/task")

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, ["abc123", "def456"])  # in order

        # page param is 0 on the first request, 1 on the second (0-based, increment by 1).
        self.assertEqual(len(responses_lib.calls), 3)
        self.assertIn("page=0", responses_lib.calls[0].request.url)
        self.assertIn("page=1", responses_lib.calls[1].request.url)
        self.assertIn("page=2", responses_lib.calls[2].request.url)

    @responses_lib.activate
    def test_credential_rides_every_page_verbatim_no_bearer(self):
        """The raw token (NO 'Bearer' prefix) rides every request including continuation pages."""
        responses_lib.add(responses_lib.GET, TASKS_URL, json={"tasks": [_TASK_1]}, status=200)
        responses_lib.add(responses_lib.GET, TASKS_URL, json={"tasks": []}, status=200)

        self._client_small().collect(f"list/{LIST_ID}/task")

        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            auth = call.request.headers["Authorization"]
            self.assertEqual(auth, _CRED)               # verbatim
            self.assertFalse(auth.startswith("Bearer "))  # no Bearer prefix

    @responses_lib.activate
    def test_empty_first_page_returns_zero_items(self):
        """An empty first page returns immediately without a second request."""
        responses_lib.add(responses_lib.GET, TASKS_URL, json={"tasks": []}, status=200)

        result = self._client_small().collect(f"list/{LIST_ID}/task")

        self.assertFalse(result["incomplete"])
        self.assertEqual(result["items"], [])
        self.assertEqual(len(responses_lib.calls), 1)
        self.assertIn("page=0", responses_lib.calls[0].request.url)


# ---------------------------------------------------------------------------
# 3. Single-GET + CLI drive on the REAL manifest (auth verbatim)
# ---------------------------------------------------------------------------

class TestClickupSingleGetAndCLI(_ClickupBase):
    @responses_lib.activate
    def test_single_get_places_token_verbatim(self):
        """api_key_header sets Authorization: <raw_token> — no 'Bearer ' prefix."""
        url = f"{BASE}/task/{TASK_ID}"
        responses_lib.add(responses_lib.GET, url, json=_TASK_1, status=200,
                          headers={"X-RateLimit-Remaining": "99"})

        c = api.client(api.MANIFESTS["clickup"], token_key="clickup")
        c.get(f"task/{TASK_ID}")

        self.assertEqual(len(responses_lib.calls), 1)
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], _CRED)

    @responses_lib.activate
    def test_cli_get_single_task(self):
        """`python -m lib.api get clickup task/{id}` round-trips through the manifest + auth."""
        url = f"{BASE}/task/{TASK_ID}"
        responses_lib.add(responses_lib.GET, url, json=_TASK_1, status=200)

        rc = api._main(["get", "clickup", f"task/{TASK_ID}", "--pick", "id,name,status.status"])
        self.assertEqual(rc, 0)
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], _CRED)

    @responses_lib.activate
    def test_cli_paginate_tasks_zero_based(self):
        """CLI --paginate auto-pages a task list; manifest page_size=100, one short page → stop."""
        responses_lib.add(responses_lib.GET, TASKS_URL, json={"tasks": [_TASK_1]}, status=200)

        rc = api._main([
            "get", "clickup", f"list/{LIST_ID}/task", "--paginate",
            "--pick", "tasks.*.id,tasks.*.name",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)  # 1 item < 100 → single page
        self.assertIn("page=0", responses_lib.calls[0].request.url)
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], _CRED)


# ---------------------------------------------------------------------------
# 4. api.pick on ClickUp fields
# ---------------------------------------------------------------------------

class TestClickupPick(_ClickupBase):
    def test_pick_selects_support_fields_from_task(self):
        picked = api.pick(
            _TASK_1,
            "id,name,status.status,assignees.*.username,due_date,url,list.name,description,creator.username",
        )
        self.assertEqual(picked["id"], "abc123")
        self.assertEqual(picked["name"], "Login page broken after deploy")
        self.assertEqual(picked["status.status"], "in progress")
        self.assertEqual(picked["assignees.*.username"], ["alice", "bob"])
        self.assertEqual(picked["url"], "https://app.clickup.com/t/abc123")
        self.assertEqual(picked["list.name"], "Sprint 42")
        self.assertEqual(picked["description"], "Users cannot log in after the 2.3.1 deploy.")
        self.assertEqual(picked["creator.username"], "carol")
        # Raw vendor keys are not surfaced.
        self.assertNotIn("status", picked)
        self.assertNotIn("assignees", picked)


# ---------------------------------------------------------------------------
# 5. Token-prefix hygiene (scoped to connector dir — only manifest.yaml remains)
# ---------------------------------------------------------------------------

class TestClickupTokenHygiene(unittest.TestCase):
    """CI guard: no real ClickUp token value (pk_<digit>) may land in the connector files.

    Scoped to the connector dir, NOT this test file (which legitimately names the prefix, split
    across concatenation). A digit after the prefix distinguishes a real token (pk_1234…) from a
    documentation mention ("pk_…").
    """

    def test_no_token_values_in_clickup_files(self):
        import re
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "clickup"
        token_re = re.compile(r"pk_\d")  # pk_ immediately followed by a digit
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix in (".pyc",) or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if token_re.search(text):
                offenders.append(f"{path.name}: contains pk_<digit>")
        self.assertEqual(offenders, [], f"real token value found in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
