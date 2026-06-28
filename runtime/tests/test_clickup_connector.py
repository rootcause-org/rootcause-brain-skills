"""Tests for the ClickUp connector (script connector, force-code trigger: page-number pagination).

No live creds, no network. HTTP is mocked with `responses`. Bodies mirror ClickUp's documented
example payloads trimmed to support-relevant fields.

Key assertions:
- The YAML manifest loads via lib.api's loader and maps every field correctly.
- The auth credential rides every request as Authorization: <token> (no "Bearer" prefix).
- The page-number pagination loop stitches ≥2 pages and stops on a short page.
- api.pick selects the declared support fields correctly.
- The script CLI (main()) renders markdown and exits 0 for tasks/task/comments/spaces.
- Token-prefix hygiene: no pk_-prefixed literal leaks into the connector directory.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_clickup_connector.py -q
"""

import json
import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors.clickup import (  # noqa: E402
    MANIFEST,
    _collect_tasks,
    get_comments,
    get_spaces,
    get_task,
    get_tasks,
    main,
)

BASE = "https://api.clickup.com/api/v2"
LIST_ID = "12345678"
TEAM_ID = "9999001"
TASK_ID = "abc123"

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

# Page 1: full 100 items simulated with 1 item + page_size override in tests;
# for simplicity we mock page 1 returning PAGE_SIZE items and page 2 returning <PAGE_SIZE.
# Two distinct pages to exercise the pagination loop.
_PAGE_1_BODY = {"tasks": [_TASK_1] * 100}   # 100 items → more pages expected
_PAGE_2_BODY = {"tasks": [_TASK_2]}          # 1 item  → last page (short)

_COMMENTS_BODY = {
    "comments": [
        {
            "id": "cm001",
            "comment_text": "Reproduced on staging. The session cookie is not being set.",
            "user": {"username": "alice"},
            "date": "1700001000000",
            "resolved": False,
        },
        {
            "id": "cm002",
            "comment_text": "Hotfix deployed. Monitoring.",
            "user": {"username": "bob"},
            "date": "1700002000000",
            "resolved": True,
        },
    ]
}

_SPACES_BODY = {
    "spaces": [
        {
            "id": "space_001",
            "name": "Engineering",
            "statuses": [{"status": "open"}, {"status": "in progress"}, {"status": "done"}],
            "features": {"due_dates": {"enabled": True}},
        },
        {
            "id": "space_002",
            "name": "Support",
            "statuses": [{"status": "open"}, {"status": "closed"}],
            "features": {"due_dates": {"enabled": False}},
        },
    ]
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task_url(scope_path: str) -> str:
    return f"{BASE}/{scope_path}/task"


def _set_token(token: str) -> None:
    os.environ["RC_CONN_CLICKUP"] = token


def _clear_token() -> None:
    os.environ.pop("RC_CONN_CLICKUP", None)


# ---------------------------------------------------------------------------
# Test: manifest loading
# ---------------------------------------------------------------------------

class TestClickupManifestLoad(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_yaml_loads_and_maps_key_fields(self):
        manifests = api.load_manifests()
        self.assertIn("clickup", manifests)
        m = manifests["clickup"]
        self.assertEqual(m.key, "clickup")
        self.assertEqual(m.base_url, "https://api.clickup.com/api/v2")
        self.assertEqual(m.auth.strategy, "api_key_header")
        self.assertEqual(m.auth.name, "Authorization")
        self.assertEqual(m.pagination.style, "none")
        self.assertEqual(m.rate_limit_remaining_header, "X-RateLimit-Remaining")

    def test_manifest_module_constant_matches_yaml(self):
        # The module-level MANIFEST constant (registered at import via api.register()) must
        # agree with the YAML on the fields that drive HTTP calls.
        self.assertEqual(MANIFEST.key, "clickup")
        self.assertEqual(MANIFEST.base_url, "https://api.clickup.com/api/v2")
        self.assertEqual(MANIFEST.auth.strategy, "api_key_header")
        self.assertEqual(MANIFEST.auth.name, "Authorization")


# ---------------------------------------------------------------------------
# Test: auth strategy — credential rides every request verbatim (no Bearer prefix)
# ---------------------------------------------------------------------------

class TestClickupAuth(unittest.TestCase):
    def setUp(self):
        _set_token("tok_clickup_test_credential")

    def tearDown(self):
        _clear_token()

    @responses_lib.activate
    def test_api_key_header_places_token_verbatim(self):
        """api_key_header sets Authorization: <raw_token> — no 'Bearer ' prefix."""
        url = f"{BASE}/task/{TASK_ID}"
        responses_lib.add(responses_lib.GET, url, json=_TASK_1, status=200,
                          headers={"X-RateLimit-Remaining": "99"})

        c = api.client(MANIFEST)
        c.get(f"task/{TASK_ID}")

        self.assertEqual(len(responses_lib.calls), 1)
        auth_header = responses_lib.calls[0].request.headers["Authorization"]
        # Credential rides verbatim — no "Bearer " prefix
        self.assertEqual(auth_header, "tok_clickup_test_credential")
        self.assertFalse(auth_header.startswith("Bearer "))

    @responses_lib.activate
    def test_credential_rides_every_page_request(self):
        """Auth header is present on page 0 AND page 1 (both requests)."""
        url = _task_url(f"list/{LIST_ID}")
        responses_lib.add(responses_lib.GET, url, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, url, json=_PAGE_2_BODY, status=200)

        # _collect_tasks takes the full path (with /task) — get_tasks adds it internally
        result = _collect_tasks(f"list/{LIST_ID}/task")
        self.assertFalse(result["incomplete"])

        for call in responses_lib.calls:
            self.assertEqual(
                call.request.headers["Authorization"],
                "tok_clickup_test_credential",
            )


# ---------------------------------------------------------------------------
# Test: page-number pagination (the force-code trigger)
# ---------------------------------------------------------------------------

class TestClickupPagination(unittest.TestCase):
    def setUp(self):
        _set_token("tok_clickup_page_test")

    def tearDown(self):
        _clear_token()

    @responses_lib.activate
    def test_two_pages_stitched_stops_on_short_page(self):
        """The page loop fetches page=0 (100 items) then page=1 (1 item) and stops."""
        url = _task_url(f"list/{LIST_ID}")
        responses_lib.add(responses_lib.GET, url, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, url, json=_PAGE_2_BODY, status=200)

        # _collect_tasks takes the FULL path including /task
        result = _collect_tasks(f"list/{LIST_ID}/task")

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 101)   # 100 + 1

        # Page numbers: first call page=0, second call page=1
        self.assertEqual(len(responses_lib.calls), 2)
        self.assertIn("page=0", responses_lib.calls[0].request.url)
        self.assertIn("page=1", responses_lib.calls[1].request.url)

    @responses_lib.activate
    def test_max_pages_cap_sets_incomplete(self):
        """When every page is full, max_pages stops the loop and sets incomplete=True."""
        url = _task_url(f"list/{LIST_ID}")
        # Both pages full (100 items each) → loop would continue but max_pages=2 cuts it.
        responses_lib.add(responses_lib.GET, url, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, url, json=_PAGE_1_BODY, status=200)

        result = _collect_tasks(f"list/{LIST_ID}/task", max_pages=2)

        self.assertTrue(result["incomplete"])
        self.assertIn("max_pages=2", result["reason"])
        self.assertEqual(len(result["items"]), 200)

    @responses_lib.activate
    def test_empty_first_page_returns_zero_items(self):
        """An empty first page (no tasks) returns immediately without a second request."""
        url = _task_url(f"list/{LIST_ID}")
        responses_lib.add(responses_lib.GET, url, json={"tasks": []}, status=200)

        result = _collect_tasks(f"list/{LIST_ID}/task")

        self.assertFalse(result["incomplete"])
        self.assertEqual(result["items"], [])
        self.assertEqual(len(responses_lib.calls), 1)


# ---------------------------------------------------------------------------
# Test: field pre-selection with api.pick
# ---------------------------------------------------------------------------

class TestClickupPick(unittest.TestCase):
    def setUp(self):
        _set_token("tok_clickup_pick_test")

    def tearDown(self):
        _clear_token()

    @responses_lib.activate
    def test_get_tasks_preselects_support_fields(self):
        """get_tasks() returns items with only the pre-selected support fields."""
        url = _task_url(f"list/{LIST_ID}")
        responses_lib.add(responses_lib.GET, url, json={"tasks": [_TASK_1]}, status=200)

        result = get_tasks(f"list/{LIST_ID}")

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        # Fields that should be present (pre-selected)
        self.assertEqual(item["id"], "abc123")
        self.assertEqual(item["name"], "Login page broken after deploy")
        self.assertEqual(item["status.status"], "in progress")
        self.assertEqual(item["assignees.*.username"], ["alice", "bob"])
        self.assertEqual(item["url"], "https://app.clickup.com/t/abc123")
        self.assertEqual(item["list.name"], "Sprint 42")
        # Raw vendor fields that the pick should NOT expose as keys
        self.assertNotIn("status", item)
        self.assertNotIn("assignees", item)

    @responses_lib.activate
    def test_get_task_single_includes_description(self):
        """get_task() fetches a single task and includes description + creator."""
        url = f"{BASE}/task/{TASK_ID}"
        responses_lib.add(responses_lib.GET, url, json=_TASK_1, status=200)

        task = get_task(TASK_ID)

        self.assertEqual(task["id"], "abc123")
        self.assertEqual(task["description"], "Users cannot log in after the 2.3.1 deploy.")
        self.assertEqual(task["creator.username"], "carol")

    @responses_lib.activate
    def test_get_comments_preselects_fields(self):
        """get_comments() returns comments with text, user, date, resolved pre-selected."""
        url = f"{BASE}/task/{TASK_ID}/comment"
        responses_lib.add(responses_lib.GET, url, json=_COMMENTS_BODY, status=200)

        comments = get_comments(TASK_ID)

        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[0]["comment_text"],
                         "Reproduced on staging. The session cookie is not being set.")
        self.assertEqual(comments[0]["user.username"], "alice")
        self.assertEqual(comments[0]["resolved"], False)
        self.assertEqual(comments[1]["resolved"], True)

    @responses_lib.activate
    def test_get_spaces_preselects_fields(self):
        """get_spaces() returns spaces with id, name, and feature flags."""
        url = f"{BASE}/team/{TEAM_ID}/space"
        responses_lib.add(responses_lib.GET, url, json=_SPACES_BODY, status=200)

        spaces = get_spaces(TEAM_ID)

        self.assertEqual(len(spaces), 2)
        self.assertEqual(spaces[0]["id"], "space_001")
        self.assertEqual(spaces[0]["name"], "Engineering")


# ---------------------------------------------------------------------------
# Test: CLI (main()) — markdown output + exit code 0
# ---------------------------------------------------------------------------

class TestClickupCLI(unittest.TestCase):
    def setUp(self):
        _set_token("tok_clickup_cli_test")

    def tearDown(self):
        _clear_token()

    @responses_lib.activate
    def test_cli_tasks_markdown(self):
        url = _task_url(f"list/{LIST_ID}")
        responses_lib.add(responses_lib.GET, url, json={"tasks": [_TASK_1]}, status=200)
        import io
        import sys as _sys
        captured = io.StringIO()
        old_stdout = _sys.stdout
        _sys.stdout = captured
        try:
            rc = main(["tasks", f"list/{LIST_ID}"])
        finally:
            _sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        output = captured.getvalue()
        self.assertIn("ClickUp tasks", output)
        self.assertIn("Login page broken after deploy", output)
        self.assertIn("in progress", output)

    @responses_lib.activate
    def test_cli_task_markdown(self):
        url = f"{BASE}/task/{TASK_ID}"
        responses_lib.add(responses_lib.GET, url, json=_TASK_1, status=200)
        import io
        import sys as _sys
        captured = io.StringIO()
        old_stdout = _sys.stdout
        _sys.stdout = captured
        try:
            rc = main(["task", TASK_ID])
        finally:
            _sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        output = captured.getvalue()
        self.assertIn("Login page broken after deploy", output)
        self.assertIn("in progress", output)

    @responses_lib.activate
    def test_cli_comments_markdown(self):
        url = f"{BASE}/task/{TASK_ID}/comment"
        responses_lib.add(responses_lib.GET, url, json=_COMMENTS_BODY, status=200)
        import io
        import sys as _sys
        captured = io.StringIO()
        old_stdout = _sys.stdout
        _sys.stdout = captured
        try:
            rc = main(["comments", TASK_ID])
        finally:
            _sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        output = captured.getvalue()
        self.assertIn("Comments", output)
        self.assertIn("alice", output)
        self.assertIn("session cookie", output)

    @responses_lib.activate
    def test_cli_spaces_markdown(self):
        url = f"{BASE}/team/{TEAM_ID}/space"
        responses_lib.add(responses_lib.GET, url, json=_SPACES_BODY, status=200)
        import io
        import sys as _sys
        captured = io.StringIO()
        old_stdout = _sys.stdout
        _sys.stdout = captured
        try:
            rc = main(["spaces", TEAM_ID])
        finally:
            _sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        output = captured.getvalue()
        self.assertIn("Engineering", output)
        self.assertIn("Support", output)

    @responses_lib.activate
    def test_cli_tasks_json_flag(self):
        url = _task_url(f"list/{LIST_ID}")
        responses_lib.add(responses_lib.GET, url, json={"tasks": [_TASK_1]}, status=200)
        import io
        import sys as _sys
        captured = io.StringIO()
        old_stdout = _sys.stdout
        _sys.stdout = captured
        try:
            rc = main(["tasks", f"list/{LIST_ID}", "--json"])
        finally:
            _sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        parsed = json.loads(captured.getvalue())
        self.assertIn("items", parsed)
        self.assertFalse(parsed["incomplete"])


# ---------------------------------------------------------------------------
# Test: lib.api CLI drive (python -m lib.api get clickup …)
# ---------------------------------------------------------------------------

class TestClickupApiCLIDrive(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        _set_token("tok_clickup_api_cli")

    def tearDown(self):
        _clear_token()

    @responses_lib.activate
    def test_api_main_get_single_task(self):
        """python -m lib.api get clickup task/{id} works for single-page reads."""
        url = f"{BASE}/task/{TASK_ID}"
        responses_lib.add(responses_lib.GET, url, json=_TASK_1, status=200)
        rc = api._main(["get", "clickup", f"task/{TASK_ID}", "--pick", "id,name,status.status"])
        self.assertEqual(rc, 0)
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], "tok_clickup_api_cli")


# ---------------------------------------------------------------------------
# Test: token-prefix hygiene guard (scoped to connector dir only)
# ---------------------------------------------------------------------------

class TestClickupTokenHygiene(unittest.TestCase):
    """CI guard: no committed ClickUp *actual token value* (pk_<digits>) may land in connector files.

    The guard scans only lib/connectors/clickup/ — NOT this test file (which legitimately
    names the prefix split across concatenation). We check for a digit after the prefix to
    distinguish a real token value (pk_1234…) from documentation mentions ("pk_…" in comments).
    Compiled .pyc files and __pycache__ are excluded (they're not committed).
    """
    # ClickUp personal tokens begin with "pk_" followed by digits — detect the digit-bearing form.
    # Split across concatenation so this literal doesn't self-flag.
    _TOKEN_PATTERN_PARTS = ("pk" "_", "0123456789")

    def test_no_token_values_in_clickup_files(self):
        import re
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "clickup"
        # Real token pattern: pk_ followed immediately by a digit (not a doc mention like pk_…)
        token_re = re.compile(r"pk_\d")
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            # Skip compiled bytecode — not committed, not a leakage risk
            if path.suffix in (".pyc",) or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if token_re.search(text):
                offenders.append(f"{path.name}: contains pk_<digit>")
        self.assertEqual(offenders, [], f"real token value found in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
