"""Fixture tests for the monday.com connector — responses-mocked, no live creds, no network.

monday.com is GraphQL-only (POST https://api.monday.com/v2), so the connector uses a script
rather than lib.api's generic GET path. This tests:
  1. YAML manifest loads correctly via lib.api's loader.
  2. The connector's _gql() helper places the bearer token on every request.
  3. get_boards / get_board / get_items (multi-page cursor) / get_updates / get_me work.
  4. Cursor pagination stitches ≥2 pages (items_page → next_items_page).
  5. api.pick selects support-relevant fields from the item shape.
  6. The CLI main() function runs the board/items/user/query subcommands.
  7. GraphQL error responses surface as api.ApiError.
  8. No monday.com token prefix leaks into committed connector files.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \
        pytest tests/test_monday_connector.py -q
"""

import json
import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
import lib.connectors.monday as monday  # noqa: E402

_GQL_URL = "https://api.monday.com/v2"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# ---------------------------------------------------------------------------

_ME_RESPONSE = {
    "data": {
        "me": {
            "id": "12345",
            "name": "Alice Support",
            "email": "alice@example.com",
            "account": {"id": "9999", "name": "Acme Corp"},
        }
    }
}

_BOARDS_RESPONSE = {
    "data": {
        "boards": [
            {
                "id": "1111111111",
                "name": "Customer Support",
                "state": "active",
                "description": "Track support tickets",
                "board_kind": "public",
                "workspace": {"id": "77", "name": "Main"},
            },
            {
                "id": "2222222222",
                "name": "Bug Tracker",
                "state": "active",
                "description": None,
                "board_kind": "private",
                "workspace": {"id": "77", "name": "Main"},
            },
        ]
    }
}

_BOARD_DETAIL_RESPONSE = {
    "data": {
        "boards": [
            {
                "id": "1111111111",
                "name": "Customer Support",
                "state": "active",
                "description": "Track support tickets",
                "columns": [
                    {"id": "name", "title": "Name", "type": "name"},
                    {"id": "status", "title": "Status", "type": "color"},
                    {"id": "text", "title": "Notes", "type": "text"},
                ],
                "groups": [
                    {"id": "topics", "title": "Open"},
                    {"id": "group_title", "title": "Closed"},
                ],
                "workspace": {"id": "77", "name": "Main"},
            }
        ]
    }
}

# Two pages of items to exercise cursor pagination.
_ITEMS_PAGE1_RESPONSE = {
    "data": {
        "boards": [
            {
                "items_page": {
                    "cursor": "cursor_abc123",
                    "items": [
                        {
                            "id": "101",
                            "name": "Widget broken on mobile",
                            "state": "active",
                            "group": {"id": "topics", "title": "Open"},
                            "column_values": [
                                {"id": "status", "text": "Working on it"},
                                {"id": "text", "text": "Reproduced on iOS 17"},
                            ],
                            "updates": [
                                {
                                    "id": "u1",
                                    "body": "Assigned to engineering.",
                                    "created_at": "2026-06-01T10:00:00Z",
                                    "creator": {"name": "Bob"},
                                }
                            ],
                        }
                    ],
                }
            }
        ]
    }
}

_ITEMS_PAGE2_RESPONSE = {
    "data": {
        "next_items_page": {
            "cursor": None,  # No further pages.
            "items": [
                {
                    "id": "102",
                    "name": "Login page slow",
                    "state": "active",
                    "group": {"id": "topics", "title": "Open"},
                    "column_values": [
                        {"id": "status", "text": "Stuck"},
                        {"id": "text", "text": ""},
                    ],
                    "updates": [],
                }
            ],
        }
    }
}

_UPDATES_RESPONSE = {
    "data": {
        "items": [
            {
                "updates": [
                    {
                        "id": "u1",
                        "body": "Assigned to engineering.",
                        "created_at": "2026-06-01T10:00:00Z",
                        "creator": {"id": "12345", "name": "Bob"},
                    },
                    {
                        "id": "u2",
                        "body": "Waiting for customer confirmation.",
                        "created_at": "2026-06-02T09:00:00Z",
                        "creator": {"id": "99", "name": "Alice"},
                    },
                ]
            }
        ]
    }
}

_GQL_ERROR_RESPONSE = {
    "errors": [{"message": "Board not found", "extensions": {"code": "NotFound"}}],
    "data": None,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_gql(body: dict, status: int = 200):
    """Register a responses POST mock for the GraphQL endpoint."""
    responses.add(responses.POST, _GQL_URL, json=body, status=status)


# ---------------------------------------------------------------------------
# 1. Manifest loading
# ---------------------------------------------------------------------------

class TestMondayManifest(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_MONDAY")
        os.environ["RC_CONN_MONDAY"] = "eyJhbGciOiJIUzI1NiJ9.test_token"  # fake JWT shape

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MONDAY", None)
        else:
            os.environ["RC_CONN_MONDAY"] = self._saved
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loads_from_yaml(self):
        manifests = api.load_manifests()
        self.assertIn("monday", manifests)
        m = manifests["monday"]
        self.assertEqual(m.key, "monday")
        self.assertEqual(m.base_url, "https://api.monday.com/v2")
        self.assertEqual(m.auth.strategy, "bearer")
        self.assertEqual(m.pagination.style, "none")
        # API-Version header must be pinned.
        self.assertIn("API-Version", m.default_headers)
        self.assertEqual(m.default_headers["API-Version"], "2026-04")
        # Content-Type required for GraphQL POSTs (catalog completeness).
        self.assertIn("Content-Type", m.default_headers)

    def test_manifest_rate_limit_header_empty(self):
        # monday.com's RateLimit header is non-standard compound format; we don't track it.
        api.load_manifests()
        m = api.MANIFESTS["monday"]
        self.assertEqual(m.rate_limit_remaining_header, "")

    def test_connector_module_registered(self):
        # The script connector registers MANIFEST at import time; YAML loader must not clobber it.
        api.load_manifests()
        self.assertIn("monday", api.MANIFESTS)
        # The registered manifest (from __init__.py) takes precedence over YAML for runtime fields.
        m = api.MANIFESTS["monday"]
        self.assertEqual(m.base_url, _GQL_URL)


# ---------------------------------------------------------------------------
# 2. Bearer credential on every request
# ---------------------------------------------------------------------------

class TestMondayAuth(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_MONDAY")
        os.environ["RC_CONN_MONDAY"] = "fake_monday_token_for_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MONDAY", None)
        else:
            os.environ["RC_CONN_MONDAY"] = self._saved

    @responses.activate
    def test_bearer_token_on_every_request(self):
        _mock_gql(_ME_RESPONSE)
        _mock_gql(_BOARDS_RESPONSE)

        monday.get_me()
        monday.get_boards(limit=10)

        self.assertEqual(len(responses.calls), 2)
        for call in responses.calls:
            self.assertEqual(call.request.headers["Authorization"], "fake_monday_token_for_test")

    @responses.activate
    def test_api_version_header_on_every_request(self):
        _mock_gql(_ME_RESPONSE)
        monday.get_me()
        call = responses.calls[0]
        self.assertEqual(call.request.headers.get("API-Version"), "2026-04")

    @responses.activate
    def test_content_type_json_on_every_request(self):
        _mock_gql(_ME_RESPONSE)
        monday.get_me()
        call = responses.calls[0]
        self.assertIn("application/json", call.request.headers.get("Content-Type", ""))


# ---------------------------------------------------------------------------
# 3. Individual read helpers
# ---------------------------------------------------------------------------

class TestMondayReads(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_MONDAY")
        os.environ["RC_CONN_MONDAY"] = "fake_monday_token_for_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MONDAY", None)
        else:
            os.environ["RC_CONN_MONDAY"] = self._saved

    @responses.activate
    def test_get_me(self):
        _mock_gql(_ME_RESPONSE)
        me = monday.get_me()
        self.assertEqual(me["id"], "12345")
        self.assertEqual(me["email"], "alice@example.com")
        self.assertEqual(me["account"]["name"], "Acme Corp")

    @responses.activate
    def test_get_boards(self):
        _mock_gql(_BOARDS_RESPONSE)
        boards = monday.get_boards(limit=10)
        self.assertEqual(len(boards), 2)
        self.assertEqual(boards[0]["name"], "Customer Support")
        self.assertEqual(boards[1]["id"], "2222222222")

    @responses.activate
    def test_get_board_detail(self):
        _mock_gql(_BOARD_DETAIL_RESPONSE)
        board = monday.get_board("1111111111")
        self.assertEqual(board["id"], "1111111111")
        cols = board["columns"]
        self.assertEqual(len(cols), 3)
        self.assertEqual(cols[1]["title"], "Status")
        groups = board["groups"]
        self.assertEqual(groups[0]["title"], "Open")

    @responses.activate
    def test_get_updates(self):
        _mock_gql(_UPDATES_RESPONSE)
        updates = monday.get_updates("101", limit=10)
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0]["body"], "Assigned to engineering.")
        self.assertEqual(updates[1]["creator"]["name"], "Alice")


# ---------------------------------------------------------------------------
# 4. Cursor pagination — items_page → next_items_page (≥2 pages)
# ---------------------------------------------------------------------------

class TestMondayCursorPagination(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_MONDAY")
        os.environ["RC_CONN_MONDAY"] = "fake_monday_token_for_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MONDAY", None)
        else:
            os.environ["RC_CONN_MONDAY"] = self._saved

    @responses.activate
    def test_get_items_stitches_two_pages(self):
        # Page 1 returns cursor "cursor_abc123"; page 2 returns cursor=None → stop.
        _mock_gql(_ITEMS_PAGE1_RESPONSE)
        _mock_gql(_ITEMS_PAGE2_RESPONSE)

        items = monday.get_items("1111111111", limit=50)

        # Both pages stitched.
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["id"], "101")
        self.assertEqual(items[0]["name"], "Widget broken on mobile")
        self.assertEqual(items[1]["id"], "102")
        self.assertEqual(items[1]["name"], "Login page slow")

        # Two HTTP POSTs were made (initial page + cursor follow).
        self.assertEqual(len(responses.calls), 2)

        # Bearer rode both calls.
        for call in responses.calls:
            self.assertEqual(call.request.headers["Authorization"], "fake_monday_token_for_test")

        # The second call used next_items_page with the cursor from page 1.
        second_body = json.loads(responses.calls[1].request.body)
        self.assertIn("next_items_page", second_body["query"])
        self.assertEqual(second_body["variables"]["cursor"], "cursor_abc123")

    @responses.activate
    def test_get_items_single_page_when_cursor_none(self):
        # If cursor is None on the first page, only one call is made.
        single_page = {
            "data": {
                "boards": [
                    {
                        "items_page": {
                            "cursor": None,
                            "items": [_ITEMS_PAGE1_RESPONSE["data"]["boards"][0]["items_page"]["items"][0]],
                        }
                    }
                ]
            }
        }
        _mock_gql(single_page)
        items = monday.get_items("1111111111", limit=50)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_get_items_respects_limit(self):
        # Even if more pages exist, honour the caller's limit.
        _mock_gql(_ITEMS_PAGE1_RESPONSE)
        # With limit=1, the first page returns 1 item and the cursor is not followed.
        items = monday.get_items("1111111111", limit=1)
        self.assertEqual(len(items), 1)
        # Only the initial page call is made; no next_items_page because limit is already satisfied.
        self.assertEqual(len(responses.calls), 1)


# ---------------------------------------------------------------------------
# 5. api.pick on item shape
# ---------------------------------------------------------------------------

class TestMondayFieldSelection(unittest.TestCase):
    def test_pick_support_fields_from_item(self):
        item = _ITEMS_PAGE1_RESPONSE["data"]["boards"][0]["items_page"]["items"][0]
        picked = api.pick(item, "id,name,state,group.title")
        self.assertEqual(picked["id"], "101")
        self.assertEqual(picked["name"], "Widget broken on mobile")
        self.assertEqual(picked["state"], "active")
        self.assertEqual(picked["group.title"], "Open")

    def test_pick_nested_column_values(self):
        item = _ITEMS_PAGE1_RESPONSE["data"]["boards"][0]["items_page"]["items"][0]
        picked = api.pick(item, "column_values.*.text")
        texts = picked.get("column_values.*.text")
        self.assertIn("Working on it", texts)
        self.assertIn("Reproduced on iOS 17", texts)


# ---------------------------------------------------------------------------
# 6. CLI subcommands
# ---------------------------------------------------------------------------

class TestMondayCLI(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_MONDAY")
        os.environ["RC_CONN_MONDAY"] = "fake_monday_token_for_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MONDAY", None)
        else:
            os.environ["RC_CONN_MONDAY"] = self._saved

    @responses.activate
    def test_cli_user(self):
        _mock_gql(_ME_RESPONSE)
        rc = monday.main(["user"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_cli_board(self):
        _mock_gql(_BOARD_DETAIL_RESPONSE)
        rc = monday.main(["board", "1111111111"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_cli_items_with_two_pages(self):
        _mock_gql(_ITEMS_PAGE1_RESPONSE)
        _mock_gql(_ITEMS_PAGE2_RESPONSE)
        rc = monday.main(["items", "1111111111", "--limit", "50"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_cli_boards(self):
        _mock_gql(_BOARDS_RESPONSE)
        rc = monday.main(["boards", "--limit", "10"])
        self.assertEqual(rc, 0)

    @responses.activate
    def test_cli_query_raw(self, capsys=None):
        _mock_gql(_ME_RESPONSE)
        rc = monday.main(["query", "query { me { id name email } }"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_cli_board_not_found_returns_1(self):
        _mock_gql({"data": {"boards": []}})
        rc = monday.main(["board", "9999999"])
        self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# 7. GraphQL error surfaces as ApiError
# ---------------------------------------------------------------------------

class TestMondayErrors(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_MONDAY")
        os.environ["RC_CONN_MONDAY"] = "fake_monday_token_for_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MONDAY", None)
        else:
            os.environ["RC_CONN_MONDAY"] = self._saved

    @responses.activate
    def test_graphql_errors_raise_api_error(self):
        _mock_gql(_GQL_ERROR_RESPONSE, status=200)
        with self.assertRaises(api.ApiError) as ctx:
            monday.get_me()
        self.assertIn("Board not found", str(ctx.exception))

    @responses.activate
    def test_http_error_raises_api_error(self):
        responses.add(responses.POST, _GQL_URL, json={"error": "Unauthorized"}, status=401)
        with self.assertRaises(api.ApiError) as ctx:
            monday.get_me()
        self.assertEqual(ctx.exception.status, 401)


# ---------------------------------------------------------------------------
# 8. Token-prefix hygiene guard (scoped to monday connector dir only)
# ---------------------------------------------------------------------------

class TestMondayCassetteHygiene(unittest.TestCase):
    """CI guard: no real monday.com token prefix leaks into committed connector files.

    Token prefix literals are split with string concatenation here so the guard doesn't
    flag THIS test file as an offender.
    """

    # monday.com personal API tokens start with "ey" (JWT base64) but that's too broad.
    # The distinctive pattern is the full base64 JWT header "eyJhbGciOiJIUzI1NiJ9." —
    # we guard on the prefix "eyJhbGciOiJIUzI1Ni" to catch leaked JWT tokens.
    _TOKEN_PREFIXES = (
        "eyJhbGciOiJIUzI1Ni",   # monday.com JWT token prefix (split to avoid self-detection)
    )

    def test_no_token_prefixes_in_monday_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "monday"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains token-like prefix")
        self.assertEqual(offenders, [], f"token-like material in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
