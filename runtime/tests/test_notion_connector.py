"""Fixture tests for the Notion connector (script connector — POST-backed search + query-db).

No live creds, no network: HTTP is mocked with ``responses``. Bodies are Notion's documented
example payloads (developers.notion.com/reference/pagination + /reference/page-object), trimmed to
support-relevant fields.

Tests cover:
  - YAML manifest loads via lib.api's loader and maps every field correctly.
  - Cursor pagination stitches ≥2 pages on both POST endpoints (search + query-db).
  - Bearer credential rides every POST request (auth strategy consistent with manifest).
  - Notion-Version header is present on every request.
  - ``api.pick`` selects support fields from GET-based pages.
  - CLI drives the script connector (search and query-db subcommands).
  - Token-hygiene guard: no real Notion token prefix leaks into the connector dir.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_notion_connector.py -q
"""

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import notion as notion_conn  # noqa: E402

API_BASE = "https://api.notion.com/v1"
SEARCH_URL = f"{API_BASE}/search"
DB_ID = "a7c5e2d9-1234-4abc-8def-000000000001"
DB_QUERY_URL = f"{API_BASE}/databases/{DB_ID}/query"

# ---------------------------------------------------------------------------
# Documented example page objects (trimmed to support-relevant fields)
# From: https://developers.notion.com/reference/page-object
# ---------------------------------------------------------------------------

_PAGE_1 = {
    "object": "page",
    "id": "a7c5e2d9-1234-4abc-8def-000000000002",
    "url": "https://www.notion.so/Meeting-Notes-a7c5e2d912344abc8def000000000002",
    "created_time": "2024-01-01T00:00:00.000Z",
    "last_edited_time": "2024-06-15T09:30:00.000Z",
    "parent": {"type": "workspace", "workspace": True},
    "properties": {
        "Name": {
            "id": "title",
            "type": "title",
            "title": [{"type": "text", "text": {"content": "Meeting Notes"}, "plain_text": "Meeting Notes"}],
        },
        "Status": {
            "id": "status",
            "type": "select",
            "select": {"name": "In Progress", "color": "blue"},
        },
        "Due": {
            "id": "due",
            "type": "date",
            "date": {"start": "2024-07-01", "end": None},
        },
    },
}

_PAGE_2 = {
    "object": "page",
    "id": "b8d6f3e0-5678-4bcd-9ef0-000000000003",
    "url": "https://www.notion.so/Onboarding-Checklist-b8d6f3e056784bcd9ef0000000000003",
    "created_time": "2024-02-10T00:00:00.000Z",
    "last_edited_time": "2024-06-20T14:00:00.000Z",
    "parent": {"type": "workspace", "workspace": True},
    "properties": {
        "Name": {
            "id": "title",
            "type": "title",
            "title": [{"type": "text", "text": {"content": "Onboarding Checklist"}, "plain_text": "Onboarding Checklist"}],
        },
        "Status": {
            "id": "status",
            "type": "select",
            "select": {"name": "Done", "color": "green"},
        },
        "Notes": {
            "id": "notes",
            "type": "rich_text",
            "rich_text": [{"plain_text": "All steps completed."}],
        },
    },
}

# Two-page search response: page 1 has has_more=True + next_cursor; page 2 is the last page.
_SEARCH_PAGE_1 = {
    "object": "list",
    "results": [_PAGE_1],
    "has_more": True,
    "next_cursor": "cursor-token-abc123",
    "type": "page_or_database",
    "page_or_database": {},
}

_SEARCH_PAGE_2 = {
    "object": "list",
    "results": [_PAGE_2],
    "has_more": False,
    "next_cursor": None,
    "type": "page_or_database",
    "page_or_database": {},
}

# Two-page database query response.
_DB_QUERY_PAGE_1 = {
    "object": "list",
    "results": [_PAGE_1],
    "has_more": True,
    "next_cursor": "db-cursor-xyz789",
    "type": "page",
    "page": {},
}

_DB_QUERY_PAGE_2 = {
    "object": "list",
    "results": [_PAGE_2],
    "has_more": False,
    "next_cursor": None,
    "type": "page",
    "page": {},
}


class NotionManifest(unittest.TestCase):
    """The YAML manifest loads correctly and maps every lib.api field."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_NOTION")
        os.environ["RC_CONN_NOTION"] = "secret_" + "test_token_placeholder"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NOTION", None)
        else:
            os.environ["RC_CONN_NOTION"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("notion", m)
        n = m["notion"]
        self.assertEqual(n.base_url, "https://api.notion.com/v1")
        self.assertEqual(n.auth.strategy, "bearer")
        # Notion-Version required header must be declared
        self.assertIn("Notion-Version", n.default_headers)
        self.assertEqual(n.default_headers["Notion-Version"], "2022-06-28")

    def test_manifest_pagination_fields(self):
        m = api.load_manifests()
        n = m["notion"]
        pg = n.pagination
        self.assertEqual(pg.style, "cursor")
        self.assertEqual(pg.cursor_param, "start_cursor")
        self.assertEqual(pg.cursor_field, "next_cursor")
        self.assertEqual(pg.has_more_field, "has_more")
        self.assertEqual(pg.items_field, "results")
        self.assertEqual(pg.page_size, 100)

    def test_manifest_rate_limit_no_remaining_header(self):
        m = api.load_manifests()
        # Notion signals rate limits with 429 + Retry-After only, no remaining count header.
        self.assertEqual(m["notion"].rate_limit_remaining_header, "")

    def test_connector_registers_same_key(self):
        # The script connector's MANIFEST constant must declare the same key as the YAML row.
        # Note: setUp clears MANIFESTS so we re-register via load_manifests(); the YAML loader
        # must not clobber an explicit register() (idempotency guard tested in test_api.py).
        api.load_manifests()
        self.assertIn("notion", api.MANIFESTS)
        self.assertEqual(notion_conn.MANIFEST.key, "notion")
        self.assertEqual(notion_conn.MANIFEST.auth.strategy, "bearer")


class NotionSearchPagination(unittest.TestCase):
    """POST /v1/search stitches ≥2 pages and places the bearer + Notion-Version on every request."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_NOTION")
        os.environ["RC_CONN_NOTION"] = "secret_" + "notion_test_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NOTION", None)
        else:
            os.environ["RC_CONN_NOTION"] = self._saved

    @responses_lib.activate
    def test_search_paginates_two_pages_and_applies_field_preselection(self):
        # Page 1: has_more=True, next_cursor set.  Page 2: last page.
        responses_lib.add(
            responses_lib.POST, SEARCH_URL,
            json=_SEARCH_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.POST, SEARCH_URL,
            json=_SEARCH_PAGE_2, status=200,
        )

        results = notion_conn.search("onboarding")

        self.assertEqual(len(responses_lib.calls), 2, "should have fetched exactly 2 pages")
        self.assertEqual(len(results), 2, "both pages stitched into one list")

        # First result: field pre-selection applied — compact_page shape
        r1 = results[0]
        self.assertEqual(r1["id"], _PAGE_1["id"])
        self.assertEqual(r1["title"], "Meeting Notes")
        self.assertEqual(r1["properties"]["Status"], "In Progress")
        self.assertEqual(r1["properties"]["Due"], "2024-07-01")
        self.assertIn("url", r1)
        self.assertIn("last_edited_time", r1)

        r2 = results[1]
        self.assertEqual(r2["title"], "Onboarding Checklist")
        self.assertEqual(r2["properties"]["Notes"], "All steps completed.")

    @responses_lib.activate
    def test_search_bearer_on_every_request(self):
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_1, status=200)
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_2, status=200)

        notion_conn.search("test")

        for call in responses_lib.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(
                auth.startswith("Bearer "),
                f"Expected Bearer auth on every POST, got: {auth!r}",
            )

    @responses_lib.activate
    def test_search_notion_version_header_on_every_request(self):
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_1, status=200)
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_2, status=200)

        notion_conn.search("test")

        for call in responses_lib.calls:
            version = call.request.headers.get("Notion-Version", "")
            self.assertEqual(
                version, "2022-06-28",
                f"Notion-Version header missing or wrong on POST: {version!r}",
            )

    @responses_lib.activate
    def test_search_cursor_sent_on_page_two(self):
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_1, status=200)
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_2, status=200)

        notion_conn.search("cursor test")

        # Second request body must carry the start_cursor from page 1's next_cursor.
        call2_body = json.loads(responses_lib.calls[1].request.body)
        self.assertEqual(call2_body.get("start_cursor"), "cursor-token-abc123")

    @responses_lib.activate
    def test_search_filter_type_sent_in_body(self):
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_2, status=200)

        notion_conn.search("pages only", filter_type="page")

        call_body = json.loads(responses_lib.calls[0].request.body)
        self.assertIn("filter", call_body)
        self.assertEqual(call_body["filter"]["value"], "page")
        self.assertEqual(call_body["filter"]["property"], "object")


class NotionQueryDatabase(unittest.TestCase):
    """POST /v1/databases/{id}/query stitches pages and pre-selects fields."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_NOTION")
        os.environ["RC_CONN_NOTION"] = "secret_" + "notion_test_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NOTION", None)
        else:
            os.environ["RC_CONN_NOTION"] = self._saved

    @responses_lib.activate
    def test_query_db_paginates_two_pages(self):
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_1, status=200)
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_2, status=200)

        results = notion_conn.query_database(DB_ID)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "Meeting Notes")
        self.assertEqual(results[1]["title"], "Onboarding Checklist")

    @responses_lib.activate
    def test_query_db_bearer_on_every_request(self):
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_1, status=200)
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_2, status=200)

        notion_conn.query_database(DB_ID)

        for call in responses_lib.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(auth.startswith("Bearer "), f"Missing Bearer: {auth!r}")

    @responses_lib.activate
    def test_query_db_cursor_sent_on_page_two(self):
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_1, status=200)
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_2, status=200)

        notion_conn.query_database(DB_ID)

        call2_body = json.loads(responses_lib.calls[1].request.body)
        self.assertEqual(call2_body.get("start_cursor"), "db-cursor-xyz789")


class NotionPickViaLibApi(unittest.TestCase):
    """lib.api's GET client + pick() work on Notion GET endpoints (pages, databases, blocks)."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_NOTION")
        os.environ["RC_CONN_NOTION"] = "secret_" + "notion_pick_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NOTION", None)
        else:
            os.environ["RC_CONN_NOTION"] = self._saved

    @responses_lib.activate
    def test_pick_selects_support_fields_from_get_page(self):
        page_url = f"{API_BASE}/pages/{_PAGE_1['id']}"
        responses_lib.add(responses_lib.GET, page_url, json=_PAGE_1, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["notion"])
        body = c.get(f"pages/{_PAGE_1['id']}")

        # Verify Notion-Version rode along on the GET too.
        self.assertEqual(
            responses_lib.calls[0].request.headers.get("Notion-Version"),
            "2022-06-28",
        )
        # pick() extracts dotted paths from the raw body
        picked = api.pick(body, "id,url,last_edited_time")
        self.assertEqual(picked["id"], _PAGE_1["id"])
        self.assertEqual(picked["url"], _PAGE_1["url"])
        self.assertIn("last_edited_time", picked)


class NotionCLIDrive(unittest.TestCase):
    """CLI subcommands for both search and query-db drive the script end-to-end."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_NOTION")
        os.environ["RC_CONN_NOTION"] = "secret_" + "notion_cli_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NOTION", None)
        else:
            os.environ["RC_CONN_NOTION"] = self._saved

    @responses_lib.activate
    def test_cli_search_prints_markdown(self):
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_2, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = notion_conn.main(["search", "onboarding"])

        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("# Notion search", output)
        self.assertIn("Onboarding Checklist", output)

    @responses_lib.activate
    def test_cli_search_with_filter_flag(self):
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_2, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = notion_conn.main(["search", "docs", "--filter", "page"])

        self.assertEqual(rc, 0)
        call_body = json.loads(responses_lib.calls[0].request.body)
        self.assertEqual(call_body["filter"]["value"], "page")

    @responses_lib.activate
    def test_cli_query_db_prints_markdown(self):
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_2, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = notion_conn.main(["query-db", DB_ID])

        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn(f"Notion database: {DB_ID}", output)
        self.assertIn("Onboarding Checklist", output)

    @responses_lib.activate
    def test_cli_lib_api_get_notion(self):
        """The manifest-only generic CLI also drives Notion GET endpoints."""
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        page_url = f"{API_BASE}/pages/{_PAGE_1['id']}"
        responses_lib.add(responses_lib.GET, page_url, json=_PAGE_1, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = api._main([
                "get", "notion", f"pages/{_PAGE_1['id']}",
                "--pick", "id,url",
            ])

        self.assertEqual(rc, 0)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["id"], _PAGE_1["id"])


class NotionTokenHygiene(unittest.TestCase):
    """CI guard: no real Notion integration token prefix may land in the connector directory.

    Scopes to the connector dir only — this test file legitimately names the prefixes it hunts,
    so scanning itself would produce a false positive (split the literals with concatenation).
    """

    # Notion integration tokens are prefixed "secret_"; OAuth access tokens use "ntn_".
    # Both are guarded here.
    _TOKEN_PREFIXES = ("secret" + "_", "ntn" + "_")

    def test_no_token_prefixes_in_notion_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "notion"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains {pref!r}")
        self.assertEqual(offenders, [], f"token-like material in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
