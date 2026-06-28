"""Fixture tests for the Intercom script connector (``lib.connectors.intercom``).

No live creds, no network — HTTP is mocked with ``responses``. Fixture bodies are shaped from
Intercom's documented example payloads (developers.intercom.com), trimmed to support-relevant
fields. Tests cover:

 - YAML manifest loads correctly and maps every declared field
 - Cursor pagination stitches ≥2 pages via ``pages.next.starting_after``
 - Bearer credential rides every request including page-2 calls
 - Dynamic items extraction works (items key matches resource type name)
 - ``api.pick`` selects the support-relevant fields
 - CLI drives the connector (list + get subcommands)
 - Token-prefix hygiene guard (no real Intercom token prefixes in the connector directory)

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_intercom_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
import lib.connectors.intercom as intercom  # noqa: E402

BASE = "https://api.intercom.io"

# ---------------------------------------------------------------------------
# Documented example payloads (Intercom REST API docs), trimmed to support-relevant fields.
# ---------------------------------------------------------------------------

_CONV_1 = {
    "type": "conversation",
    "id": "1",
    "created_at": 1717000000,
    "updated_at": 1717001000,
    "state": "open",
    "read": False,
    "waiting_since": 1717000500,
    "source": {
        "type": "email",
        "subject": "Cannot log in",
        "body": "<p>I cannot log in to my account.</p>",
    },
    "contacts": {"contacts": [{"id": "contact_abc"}]},
    "assignee": {"id": "admin_1", "name": "Alice", "email": "alice@example.com"},
    "tags": {"tags": [{"name": "billing"}]},
}

_CONV_2 = {
    "type": "conversation",
    "id": "2",
    "created_at": 1717002000,
    "updated_at": 1717003000,
    "state": "closed",
    "read": True,
    "waiting_since": None,
    "source": {"type": "chat", "subject": "", "body": "<p>Upgrade question</p>"},
    "contacts": {"contacts": [{"id": "contact_xyz"}]},
    "assignee": None,
    "tags": {"tags": []},
}

# Page 1 of conversations — cursor in pages.next.starting_after; page 2 has no next.
_CONV_PAGE_1 = {
    "type": "conversation.list",
    "pages": {
        "type": "pages",
        "page": 1,
        "per_page": 1,
        "total_pages": 2,
        "next": {"page": 2, "starting_after": "cursor_abc123"},
    },
    "total_count": 2,
    "conversations": [_CONV_1],
}
_CONV_PAGE_2 = {
    "type": "conversation.list",
    "pages": {
        "type": "pages",
        "page": 2,
        "per_page": 1,
        "total_pages": 2,
        # no "next" key — signals last page
    },
    "total_count": 2,
    "conversations": [_CONV_2],
}

_CONTACT_1 = {
    "type": "contact",
    "id": "contact_abc",
    "external_id": "ext_1",
    "email": "user@example.com",
    "phone": "+15551234567",
    "name": "Jane Doe",
    "role": "user",
    "created_at": 1700000000,
    "last_seen_at": 1717000000,
    "last_replied_at": 1717000500,
    "unsubscribed_from_emails": False,
    "companies": {
        "companies": [
            {"id": "comp_1", "name": "Acme Corp", "company_id": "acme"}
        ]
    },
}

_CONTACT_PAGE_1 = {
    "type": "list",
    "pages": {
        "type": "pages",
        "page": 1,
        "per_page": 1,
        "total_pages": 1,
        # no next → single page
    },
    "total_count": 1,
    "contacts": [_CONTACT_1],
}


# ---------------------------------------------------------------------------
# Test: manifest loading
# ---------------------------------------------------------------------------


class IntercomManifestTest(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("intercom", m)
        ic = m["intercom"]
        self.assertEqual(ic.base_url, "https://api.intercom.io")
        self.assertEqual(ic.auth.strategy, "bearer")
        # Pagination is cursor style driven by pages.next.starting_after
        self.assertEqual(ic.pagination.style, "cursor")
        self.assertEqual(ic.pagination.cursor_param, "starting_after")
        self.assertEqual(ic.pagination.cursor_field, "pages.next.starting_after")
        # items_field is "" — items extraction is dynamic (script's responsibility)
        self.assertEqual(ic.pagination.items_field, "")
        # Rate limit header
        self.assertEqual(ic.rate_limit_remaining_header, "X-RateLimit-Remaining")
        # Required default headers
        self.assertIn("Intercom-Version", ic.default_headers)
        self.assertEqual(ic.default_headers["Intercom-Version"], "2.11")
        self.assertIn("Accept", ic.default_headers)

    def test_connector_registration(self):
        """Importing the connector registers the manifest; YAML loader defers to it."""
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        # Import triggers register() in __init__.py
        import importlib
        import lib.connectors.intercom as ic_mod
        importlib.reload(ic_mod)
        self.assertIn("intercom", api.MANIFESTS)


# ---------------------------------------------------------------------------
# Test: pagination — 2 pages of conversations, cursor stitching
# ---------------------------------------------------------------------------


class IntercomPaginationTest(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_INTERCOM")
        os.environ["RC_CONN_INTERCOM"] = "tok_" + "intercom_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_INTERCOM", None)
        else:
            os.environ["RC_CONN_INTERCOM"] = self._saved

    @responses_lib.activate
    def test_cursor_pagination_stitches_two_pages(self):
        """list_resource follows pages.next.starting_after and stitches both pages."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_1, status=200,
            headers={"X-RateLimit-Remaining": "499"},
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_2, status=200,
            headers={"X-RateLimit-Remaining": "498"},
        )

        result = intercom.list_resource("/conversations", page_size=1)

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["id"], "1")
        self.assertEqual(result["items"][1]["id"], "2")

    @responses_lib.activate
    def test_bearer_credential_on_every_request(self):
        """Bearer token appears in Authorization header on page 1 AND page 2."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_2, status=200,
        )

        intercom.list_resource("/conversations", page_size=1)

        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(
                auth.startswith("Bearer "),
                f"Expected Bearer auth, got: {auth!r}",
            )
            self.assertIn("intercom_test", auth)

    @responses_lib.activate
    def test_intercom_version_header_on_every_request(self):
        """Intercom-Version header is sent on all calls (required by Intercom)."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_2, status=200,
        )

        intercom.list_resource("/conversations", page_size=1)

        for call in responses_lib.calls:
            self.assertEqual(call.request.headers.get("Intercom-Version"), "2.11")

    @responses_lib.activate
    def test_cursor_sent_on_page_2(self):
        """Page 2 request carries starting_after=cursor_abc123 as query param."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_2, status=200,
        )

        intercom.list_resource("/conversations", page_size=1)

        self.assertEqual(len(responses_lib.calls), 2)
        # Page 2 must carry the cursor from page 1's pages.next.starting_after
        page2_url = responses_lib.calls[1].request.url
        self.assertIn("starting_after=cursor_abc123", page2_url)

    @responses_lib.activate
    def test_single_page_no_next_stops(self):
        """Single-page response (no pages.next) → exactly 1 call, incomplete=False."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/contacts",
            json=_CONTACT_PAGE_1, status=200,
        )

        result = intercom.list_resource("/contacts")

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(responses_lib.calls), 1)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["email"], "user@example.com")

    @responses_lib.activate
    def test_max_pages_cap_sets_incomplete(self):
        """Reaching max_pages before exhausting the cursor sets incomplete=True."""
        # Return page 1 twice (would continue if cap allowed)
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_1, status=200,
        )

        result = intercom.list_resource("/conversations", page_size=1, max_pages=1)

        self.assertTrue(result["incomplete"])
        self.assertIn("max_pages", result["reason"])
        self.assertEqual(len(result["items"]), 1)


# ---------------------------------------------------------------------------
# Test: field extraction — dynamic items key resolution
# ---------------------------------------------------------------------------


class IntercomItemsExtractionTest(unittest.TestCase):
    def test_items_from_body_conversations(self):
        body = _CONV_PAGE_1
        items = intercom._items_from_body(body, "conversations")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "1")

    def test_items_from_body_contacts(self):
        body = _CONTACT_PAGE_1
        items = intercom._items_from_body(body, "contacts")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["email"], "user@example.com")

    def test_items_from_body_fallback_to_first_list_key(self):
        """Unknown resource type falls back to the first list-valued key in the envelope."""
        body = {
            "type": "list",
            "pages": {"type": "pages"},
            "total_count": 1,
            "widgets": [{"id": "w1"}],
        }
        items = intercom._items_from_body(body, "widgets")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "w1")

    def test_next_cursor_present(self):
        cursor = intercom._next_cursor(_CONV_PAGE_1)
        self.assertEqual(cursor, "cursor_abc123")

    def test_next_cursor_absent(self):
        cursor = intercom._next_cursor(_CONV_PAGE_2)
        self.assertIsNone(cursor)

    def test_next_cursor_no_next_key(self):
        body = {"pages": {"type": "pages", "page": 1}}
        self.assertIsNone(intercom._next_cursor(body))


# ---------------------------------------------------------------------------
# Test: pick field pre-selection
# ---------------------------------------------------------------------------


class IntercomPickTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_INTERCOM")
        os.environ["RC_CONN_INTERCOM"] = "tok_" + "intercom_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_INTERCOM", None)
        else:
            os.environ["RC_CONN_INTERCOM"] = self._saved

    @responses_lib.activate
    def test_pick_conversation_fields(self):
        """list_resource returns items; pick selects the declared support fields."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_2, status=200,  # single page, no next
        )

        result = intercom.list_resource("/conversations", page_size=50)
        items = result["items"]
        self.assertEqual(len(items), 1)
        picked = api.pick(items[0], intercom._PICK_FIELDS["conversations"])

        self.assertIn("id", picked)
        self.assertIn("state", picked)
        self.assertIn("source.subject", picked)
        self.assertEqual(picked["state"], "closed")

    def test_pick_contact_fields(self):
        picked = api.pick(_CONTACT_1, intercom._PICK_FIELDS["contacts"])
        self.assertEqual(picked["email"], "user@example.com")
        self.assertEqual(picked["name"], "Jane Doe")
        self.assertIn("companies.companies.*.name", picked)
        self.assertEqual(picked["companies.companies.*.name"], ["Acme Corp"])


# ---------------------------------------------------------------------------
# Test: get_resource (single item GET)
# ---------------------------------------------------------------------------


class IntercomGetResourceTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_INTERCOM")
        os.environ["RC_CONN_INTERCOM"] = "tok_" + "intercom_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_INTERCOM", None)
        else:
            os.environ["RC_CONN_INTERCOM"] = self._saved

    @responses_lib.activate
    def test_get_conversation_by_id(self):
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations/1",
            json=_CONV_1, status=200,
        )

        body = intercom.get_resource("/conversations/1")

        self.assertEqual(body["id"], "1")
        self.assertEqual(body["state"], "open")
        self.assertEqual(len(responses_lib.calls), 1)
        auth = responses_lib.calls[0].request.headers["Authorization"]
        self.assertTrue(auth.startswith("Bearer "))


# ---------------------------------------------------------------------------
# Test: CLI
# ---------------------------------------------------------------------------


class IntercomCLITest(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_INTERCOM")
        os.environ["RC_CONN_INTERCOM"] = "tok_" + "intercom_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_INTERCOM", None)
        else:
            os.environ["RC_CONN_INTERCOM"] = self._saved

    @responses_lib.activate
    def test_cli_list_conversations(self, capsys=None):
        """CLI 'list conversations' fetches and prints JSON."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_2, status=200,
        )

        rc = intercom.main(["list", "conversations"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)
        self.assertEqual(
            responses_lib.calls[0].request.headers.get("Intercom-Version"), "2.11"
        )

    @responses_lib.activate
    def test_cli_list_with_query_param(self):
        """CLI --query flags are forwarded to the request."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_2, status=200,
        )

        rc = intercom.main(["list", "conversations", "--query", "state=open"])
        self.assertEqual(rc, 0)
        url = responses_lib.calls[0].request.url
        self.assertIn("state=open", url)

    @responses_lib.activate
    def test_cli_get_conversation(self):
        """CLI 'get conversation <id>' fetches a single conversation."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations/1",
            json=_CONV_1, status=200,
        )

        rc = intercom.main(["get", "conversation", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_cli_get_contact(self):
        """CLI 'get contact <id>' resolves the plural path correctly."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/contacts/contact_abc",
            json=_CONTACT_1, status=200,
        )

        rc = intercom.main(["get", "contact", "contact_abc"])
        self.assertEqual(rc, 0)
        called_url = responses_lib.calls[0].request.url
        self.assertIn("/contacts/contact_abc", called_url)

    @responses_lib.activate
    def test_cli_list_two_pages_via_main(self):
        """CLI paginates through 2 pages and returns both in the JSON output."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_2, status=200,
        )

        rc = intercom.main(["list", "conversations", "--page-size", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)


# ---------------------------------------------------------------------------
# Test: lib.api manifest-level CLI drives intercom (for manifest completeness check)
# ---------------------------------------------------------------------------


class IntercomApiCliTest(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_INTERCOM")
        os.environ["RC_CONN_INTERCOM"] = "tok_" + "intercom_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_INTERCOM", None)
        else:
            os.environ["RC_CONN_INTERCOM"] = self._saved

    @responses_lib.activate
    def test_api_cli_get_single_page(self):
        """python -m lib.api get intercom /conversations works for a single-page GET."""
        # Single conversation object (not a list envelope) — simulating raw GET
        responses_lib.add(
            responses_lib.GET, f"{BASE}/conversations",
            json=_CONV_PAGE_2, status=200,
        )

        rc = api._main(["get", "intercom", "/conversations"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)
        self.assertEqual(
            responses_lib.calls[0].request.headers.get("Authorization"),
            "Bearer tok_intercom_test",
        )


# ---------------------------------------------------------------------------
# Test: token-prefix hygiene guard
# ---------------------------------------------------------------------------


class IntercomHygieneTest(unittest.TestCase):
    """CI guard: no real Intercom token prefix may land in the connector directory.

    Scopes to the connector dir (manifest + script), NOT this test file — the test legitimately
    names the prefixes to scan for, so scanning itself would be a false positive.

    Intercom access tokens are opaque long strings without a public prefix, so the check guards
    against accidentally committing a real-looking token pattern or placeholder like
    ``dG9r...`` (base64 preamble) or literal ``Bearer`` value in the source files.
    """

    # Intercom tokens have no published prefix, but guard against common placeholder leaks.
    # Split concatenation so this guard itself isn't flagged by the very patterns it seeks.
    _FORBIDDEN_PATTERNS = (
        "Bearer " + "dG9r",      # base64 preamble of "tok" — a real token start
        "RC_CONN_INTERCOM=" + "dG",   # inline assignment with a value
    )

    def test_no_token_patterns_in_intercom_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "intercom"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pat in self._FORBIDDEN_PATTERNS:
                if pat in text:
                    offenders.append(f"{path.name}: {pat!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
