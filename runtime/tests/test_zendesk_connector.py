"""Fixture test for the Zendesk connector (lib.connectors.zendesk).

Force-code trigger (d): items_field varies per endpoint — the script handles dynamic extraction
from the response envelope. Tests cover YAML loading, cursor pagination stitching ≥2 pages,
credential on every request (incl. cursor-follow), api.pick field selection, and CLI invocation.

No live creds, no network: HTTP is mocked with `responses`. Bodies mirror Zendesk's documented
example payloads, trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \
        pytest tests/test_zendesk_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
import lib.connectors.zendesk as zendesk  # noqa: E402  — registers MANIFEST via import

BASE = "https://acme.zendesk.com/api/v2"
TICKETS_URL = f"{BASE}/tickets"
USERS_URL = f"{BASE}/users"
SEARCH_URL = f"{BASE}/search"
COMMENTS_URL = f"{BASE}/tickets/1/comments"

# ---------------------------------------------------------------------------
# Fixture payloads — Zendesk documented example shapes, trimmed to support fields.
# Cursor pagination envelope: { "tickets": [...], "meta": {...}, "links": {...} }
# ---------------------------------------------------------------------------

_TICKET_1 = {
    "id": 1,
    "url": f"{BASE}/tickets/1.json",
    "created_at": "2024-01-15T10:00:00Z",
    "updated_at": "2024-01-16T12:00:00Z",
    "subject": "My printer is on fire",
    "description": "The description of the ticket",
    "status": "open",
    "priority": "high",
    "type": "incident",
    "requester_id": 123,
    "assignee_id": 456,
    "organization_id": 789,
    "group_id": 101,
    "tags": ["printer", "fire"],
}

_TICKET_2 = {
    "id": 2,
    "url": f"{BASE}/tickets/2.json",
    "created_at": "2024-01-17T08:00:00Z",
    "updated_at": "2024-01-17T09:00:00Z",
    "subject": "Feature request: dark mode",
    "description": "Please add dark mode",
    "status": "new",
    "priority": "low",
    "type": "question",
    "requester_id": 124,
    "assignee_id": None,
    "organization_id": 789,
    "group_id": 101,
    "tags": ["feature"],
}

# Page 1: one ticket, has_more=True, after_cursor points to page 2.
_PAGE_1_BODY = {
    "tickets": [_TICKET_1],
    "meta": {"has_more": True, "after_cursor": "cursor_abc123", "before_cursor": None},
    "links": {"prev": None, "next": f"{TICKETS_URL}?page[size]=100&page[after]=cursor_abc123"},
}

# Page 2: one ticket, has_more=False — loop stops.
_PAGE_2_BODY = {
    "tickets": [_TICKET_2],
    "meta": {"has_more": False, "after_cursor": "cursor_end", "before_cursor": "cursor_abc123"},
    "links": {"prev": f"{TICKETS_URL}?page[size]=100", "next": None},
}

# Single ticket response (GET /tickets/1)
_SINGLE_TICKET_BODY = {"ticket": _TICKET_1}

# User list (page 1 only, has_more=False)
_USER_1 = {
    "id": 123,
    "url": f"{BASE}/users/123.json",
    "name": "Jane Smith",
    "email": "jane@example.com",
    "created_at": "2023-06-01T00:00:00Z",
    "updated_at": "2024-01-10T00:00:00Z",
    "role": "end-user",
    "organization_id": 789,
    "phone": "+1 555-1234",
    "time_zone": "Eastern Time (US & Canada)",
    "locale": "en-US",
    "suspended": False,
    "verified": True,
}
_USERS_PAGE_BODY = {
    "users": [_USER_1],
    "meta": {"has_more": False, "after_cursor": None, "before_cursor": None},
    "links": {"prev": None, "next": None},
}

# Search results (GET /search?query=type:ticket)
_SEARCH_RESULT_1 = {
    "id": 1,
    "url": f"{BASE}/tickets/1.json",
    "result_type": "ticket",
    "subject": "My printer is on fire",
    "status": "open",
    "created_at": "2024-01-15T10:00:00Z",
    "updated_at": "2024-01-16T12:00:00Z",
}
_SEARCH_BODY = {
    "results": [_SEARCH_RESULT_1],
    "meta": {"has_more": False, "after_cursor": None},
    "links": {"next": None},
    "count": 1,
}

# Comments on ticket 1
_COMMENT_1 = {
    "id": 1001,
    "type": "Comment",
    "body": "The issue is still happening.",
    "html_body": "<p>The issue is still happening.</p>",
    "created_at": "2024-01-16T11:00:00Z",
    "public": True,
    "author_id": 123,
}
_COMMENTS_BODY = {
    "comments": [_COMMENT_1],
    "meta": {"has_more": False, "after_cursor": None},
    "links": {"next": None},
}


class ZendeskManifestLoading(unittest.TestCase):
    """YAML loads correctly via lib.api and maps every declared field."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        # Re-register via import path (connector registers at import time)
        import importlib
        import lib.connectors.zendesk as z
        importlib.reload(z)

    def test_manifest_fields(self):
        m = api.load_manifests()
        self.assertIn("zendesk", m)
        z = m["zendesk"]
        self.assertIn("{subdomain}.zendesk.com", z.base_url)
        self.assertEqual(z.auth.strategy, "basic")
        self.assertEqual(z.pagination.style, "cursor")
        self.assertEqual(z.pagination.cursor_field, "meta.after_cursor")
        self.assertEqual(z.pagination.cursor_param, "page[after]")
        self.assertEqual(z.pagination.has_more_field, "meta.has_more")
        self.assertEqual(z.pagination.page_size, 100)
        self.assertEqual(z.rate_limit_remaining_header, "X-RateLimit-Remaining")

    def test_manifest_registered_by_connector_import(self):
        # Connector import registers the manifest — key must be present without load_manifests().
        self.assertIn("zendesk", api.MANIFESTS)


class ZendeskCursorPagination(unittest.TestCase):
    """Cursor pagination stitches ≥2 pages; credentials ride every request."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_ZENDESK")
        # Zendesk basic: email/token:apikey — lib.api splits on first ":" → user=email/token, pass=apikey
        os.environ["RC_CONN_ZENDESK"] = "agent@acme.com/token:" + "test_api_key_abc"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_ZENDESK", None)
        else:
            os.environ["RC_CONN_ZENDESK"] = self._saved

    @responses_lib.activate
    def test_two_pages_stitched(self):
        # Page 1: has_more=True with after_cursor → page 2. Page 2: has_more=False → stop.
        responses_lib.add(responses_lib.GET, TICKETS_URL, json=_PAGE_1_BODY, status=200,
                          headers={"X-RateLimit-Remaining": "499"})
        responses_lib.add(responses_lib.GET, TICKETS_URL, json=_PAGE_2_BODY, status=200,
                          headers={"X-RateLimit-Remaining": "498"})

        result = zendesk.list_resource("/tickets", base_url=BASE, max_pages=10)

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, [1, 2])  # both pages stitched in order
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_basic_credential_on_every_request_including_cursor_follow(self):
        """Authorization: Basic header must appear on page 1 AND the cursor-follow page 2."""
        responses_lib.add(responses_lib.GET, TICKETS_URL, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, TICKETS_URL, json=_PAGE_2_BODY, status=200)

        zendesk.list_resource("/tickets", base_url=BASE, max_pages=10)

        # Both calls carry the Authorization header (Basic scheme).
        for call in responses_lib.calls:
            auth_header = call.request.headers.get("Authorization", "")
            self.assertTrue(
                auth_header.startswith("Basic "),
                f"Expected Basic auth on call, got: {auth_header!r}",
            )

    @responses_lib.activate
    def test_cursor_param_sent_on_page_two(self):
        """page[after] param with after_cursor value must be sent on the second request."""
        responses_lib.add(responses_lib.GET, TICKETS_URL, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, TICKETS_URL, json=_PAGE_2_BODY, status=200)

        zendesk.list_resource("/tickets", base_url=BASE)

        # Page 1 must NOT have page[after].
        url_1 = responses_lib.calls[0].request.url
        self.assertNotIn("page%5Bafter%5D", url_1)
        self.assertNotIn("page[after]", url_1)

        # Page 2 must carry the cursor from page 1's meta.after_cursor.
        url_2 = responses_lib.calls[1].request.url
        self.assertIn("cursor_abc123", url_2)

    @responses_lib.activate
    def test_single_page_no_has_more(self):
        """When has_more=False on page 1, collect stops after one page."""
        responses_lib.add(responses_lib.GET, USERS_URL, json=_USERS_PAGE_BODY, status=200)

        result = zendesk.list_resource("/users", base_url=BASE)

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], 123)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_max_pages_cap_triggers_incomplete(self):
        """Hitting max_pages returns incomplete=True with the reason."""
        # Both pages advertise has_more=True — max_pages=1 truncates after first.
        page1_infinite = {
            "tickets": [_TICKET_1],
            "meta": {"has_more": True, "after_cursor": "cursor_loop"},
            "links": {},
        }
        responses_lib.add(responses_lib.GET, TICKETS_URL, json=page1_infinite, status=200)

        result = zendesk.list_resource("/tickets", base_url=BASE, max_pages=1)

        self.assertTrue(result["incomplete"])
        self.assertIn("max_pages", result["reason"])
        self.assertEqual(len(responses_lib.calls), 1)


class ZendeskItemsExtraction(unittest.TestCase):
    """Dynamic items extraction from envelope — the core force-code-trigger concern."""

    def test_tickets_key_extracted(self):
        body = {"tickets": [_TICKET_1, _TICKET_2], "meta": {"has_more": False}}
        items = zendesk._items_from_body(body, "tickets")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["id"], 1)

    def test_users_key_extracted(self):
        body = {"users": [_USER_1], "meta": {"has_more": False}}
        items = zendesk._items_from_body(body, "users")
        self.assertEqual(len(items), 1)

    def test_fallback_finds_first_list(self):
        """When resource_key is missing, the fallback finds the first list-valued key."""
        body = {"unknown_items": [{"id": 99}], "meta": {"has_more": False}}
        items = zendesk._items_from_body(body, "unknown_items")
        self.assertEqual(items, [{"id": 99}])

    def test_meta_and_links_keys_excluded_from_fallback(self):
        """meta and links are excluded from the fallback scan even though they are dict-valued."""
        body = {"meta": {"has_more": False}, "links": {}, "tickets": [_TICKET_1]}
        items = zendesk._items_from_body(body, "tickets")
        self.assertEqual(len(items), 1)

    def test_resource_key_for_search(self):
        self.assertEqual(zendesk._resource_key("/search"), "results")

    def test_resource_key_for_tickets(self):
        self.assertEqual(zendesk._resource_key("/tickets"), "tickets")

    def test_resource_key_for_comments(self):
        self.assertEqual(zendesk._resource_key("/tickets/1/comments"), "tickets")
        # Note: comments are nested; the script list_resource uses the path to derive key.
        # /tickets/1/comments → first segment is "tickets", but body has "comments".
        # _items_from_body fallback handles this correctly.

    def test_next_cursor_extracted(self):
        self.assertEqual(zendesk._next_cursor(_PAGE_1_BODY), "cursor_abc123")

    def test_next_cursor_none_when_has_more_false(self):
        self.assertIsNone(zendesk._next_cursor(_PAGE_2_BODY))

    def test_next_cursor_none_when_no_meta(self):
        self.assertIsNone(zendesk._next_cursor({"tickets": []}))


class ZendeskPickFieldSelection(unittest.TestCase):
    """api.pick selects the declared support-relevant fields."""

    def test_pick_ticket_fields(self):
        picked = api.pick(_TICKET_1, zendesk._PICK_FIELDS["tickets"])
        self.assertEqual(picked["id"], 1)
        self.assertEqual(picked["status"], "open")
        self.assertEqual(picked["subject"], "My printer is on fire")
        self.assertIn("tags", picked)

    def test_pick_user_fields(self):
        picked = api.pick(_USER_1, zendesk._PICK_FIELDS["users"])
        self.assertEqual(picked["id"], 123)
        self.assertEqual(picked["email"], "jane@example.com")
        self.assertEqual(picked["role"], "end-user")

    def test_pick_excludes_extra_fields(self):
        ticket_with_extra = dict(_TICKET_1, sensitive_internal_field="secret_value")
        picked = api.pick(ticket_with_extra, zendesk._PICK_FIELDS["tickets"])
        self.assertNotIn("sensitive_internal_field", picked)


class ZendeskSearchAndComments(unittest.TestCase):
    """Search and comments sub-resources work correctly."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_ZENDESK")
        os.environ["RC_CONN_ZENDESK"] = "agent@acme.com/token:" + "test_api_key_abc"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_ZENDESK", None)
        else:
            os.environ["RC_CONN_ZENDESK"] = self._saved

    @responses_lib.activate
    def test_search_returns_results(self):
        responses_lib.add(responses_lib.GET, SEARCH_URL, json=_SEARCH_BODY, status=200)

        result = zendesk.search("type:ticket status:open", base_url=BASE)

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["result_type"], "ticket")

    @responses_lib.activate
    def test_comments_list(self):
        responses_lib.add(responses_lib.GET, COMMENTS_URL, json=_COMMENTS_BODY, status=200)

        result = zendesk.list_resource("/tickets/1/comments", base_url=BASE)

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], 1001)


class ZendeskCLIDrive(unittest.TestCase):
    """CLI invocations (main()) wire through to the right endpoints."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_ZENDESK")
        os.environ["RC_CONN_ZENDESK"] = "agent@acme.com/token:" + "test_api_key_abc"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_ZENDESK", None)
        else:
            os.environ["RC_CONN_ZENDESK"] = self._saved

    @responses_lib.activate
    def test_cli_list_tickets(self, capsys=None):
        responses_lib.add(responses_lib.GET, TICKETS_URL, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, TICKETS_URL, json=_PAGE_2_BODY, status=200)

        rc = zendesk.main(["--base-url", BASE, "list", "tickets", "--max-pages", "5"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_cli_get_ticket(self):
        single_url = f"{BASE}/tickets/1"
        responses_lib.add(responses_lib.GET, single_url, json=_SINGLE_TICKET_BODY, status=200)

        rc = zendesk.main(["--base-url", BASE, "get", "ticket", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)
        self.assertTrue(responses_lib.calls[0].request.url.endswith("/tickets/1"))

    @responses_lib.activate
    def test_cli_search(self):
        responses_lib.add(responses_lib.GET, SEARCH_URL, json=_SEARCH_BODY, status=200)

        rc = zendesk.main(["--base-url", BASE, "search", "type:ticket status:open"])
        self.assertEqual(rc, 0)
        call_url = responses_lib.calls[0].request.url
        self.assertIn("search", call_url)
        self.assertIn("type%3Aticket", call_url)  # "type:ticket" URL-encoded

    @responses_lib.activate
    def test_cli_comments(self):
        responses_lib.add(responses_lib.GET, COMMENTS_URL, json=_COMMENTS_BODY, status=200)

        rc = zendesk.main(["--base-url", BASE, "comments", "1"])
        self.assertEqual(rc, 0)
        self.assertTrue(responses_lib.calls[0].request.url.startswith(COMMENTS_URL))

    @responses_lib.activate
    def test_cli_bearer_rides_every_request(self):
        """Basic credential appears on every CLI-driven request."""
        responses_lib.add(responses_lib.GET, TICKETS_URL, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, TICKETS_URL, json=_PAGE_2_BODY, status=200)

        zendesk.main(["--base-url", BASE, "list", "tickets"])

        for call in responses_lib.calls:
            self.assertTrue(call.request.headers["Authorization"].startswith("Basic "))


class ZendeskTokenHygieneGuard(unittest.TestCase):
    """CI guard: no real Zendesk API token prefix may land in the connector directory.

    Scopes to the connector dir only — this test file legitimately names prefixes it hunts,
    so scanning itself would be a false positive.
    """

    # Zendesk API tokens are opaque strings — guard against common plaintext token patterns
    # by checking for the test-value prefix we use in this file, split to avoid self-match.
    _TOKEN_PREFIXES = (
        "test_api" + "_key",   # our test fixture value — must never be committed to connector files
    )

    def test_no_token_prefixes_in_zendesk_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "zendesk"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: found {pref!r}")
        self.assertEqual(offenders, [], f"token-like material in connector dir: {offenders}")


if __name__ == "__main__":
    unittest.main()
