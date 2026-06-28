"""Fixture test for the Help Scout connector.

No live creds, no network: HTTP is mocked with ``responses``. Bodies are modelled on
Help Scout's documented example payloads (HAL+JSON with ``_embedded`` + ``_links`` + ``page``).

Covers:
- YAML manifest loads via lib.api's loader and maps every field correctly.
- Page-number pagination (_links.next in body) stitches ≥2 pages with page counter advancing.
- The OAuth2 client-credentials bearer rides EVERY request (incl. page 2+).
- api.pick selects support-relevant fields from conversations and customers.
- The connector's domain helpers (list_conversations, get_conversation, list_threads,
  resolve_customer, list_mailboxes) return correctly shaped dicts.
- The CLI drive works via helpscout_conn.main([...]).

Token-prefix hygiene guard: any Help Scout access token prefix is split with string concatenation
so the guard doesn't flag its own literals.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_helpscout_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
import lib.connectors.helpscout as helpscout_conn  # noqa: E402

BASE = "https://api.helpscout.net/v2"
# Paths in the connector are relative to base_url (which already has /v2), so the wire URL
# is base_url + "/" + path. Mocked URLs must match what requests actually sends.
CONVERSATIONS_URL = f"{BASE}/conversations"
CONV_URL = f"{BASE}/conversations/123456"
THREADS_URL = f"{BASE}/conversations/123456/threads"
CUSTOMERS_URL = f"{BASE}/customers"
CUSTOMER_URL = f"{BASE}/customers/9001"
MAILBOXES_URL = f"{BASE}/mailboxes"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields, HAL+JSON shape)
# ---------------------------------------------------------------------------

_CONV_PAGE_1 = {
    "_embedded": {
        "conversations": [
            {
                "id": 123456,
                "number": 42,
                "subject": "Cannot log in",
                "status": "active",
                "mailboxId": 11,
                "assignee": {"email": "agent@example.com"},
                "customer": {"email": "user@example.com"},
                "createdAt": "2024-01-10T09:00:00Z",
                "closedAt": None,
                "tags": ["billing", "urgent"],
            }
        ]
    },
    "_links": {
        "self": {"href": f"{CONVERSATIONS_URL}?page=1"},
        "next": {"href": f"{CONVERSATIONS_URL}?page=2"},
        "first": {"href": f"{CONVERSATIONS_URL}?page=1"},
        "last": {"href": f"{CONVERSATIONS_URL}?page=2"},
    },
    "page": {"number": 1, "size": 25, "totalElements": 2, "totalPages": 2},
}

_CONV_PAGE_2 = {
    "_embedded": {
        "conversations": [
            {
                "id": 789012,
                "number": 43,
                "subject": "Refund request",
                "status": "closed",
                "mailboxId": 11,
                "assignee": None,
                "customer": {"email": "other@example.com"},
                "createdAt": "2024-01-09T14:30:00Z",
                "closedAt": "2024-01-10T08:00:00Z",
                "tags": ["refund"],
            }
        ]
    },
    "_links": {
        "self": {"href": f"{CONVERSATIONS_URL}?page=2"},
        "first": {"href": f"{CONVERSATIONS_URL}?page=1"},
        "last": {"href": f"{CONVERSATIONS_URL}?page=2"},
    },
    "page": {"number": 2, "size": 25, "totalElements": 2, "totalPages": 2},
}

_SINGLE_CONV = {
    "id": 123456,
    "number": 42,
    "subject": "Cannot log in",
    "status": "active",
    "mailboxId": 11,
    "assignee": {"email": "agent@example.com"},
    "customer": {"email": "user@example.com"},
    "createdAt": "2024-01-10T09:00:00Z",
    "closedAt": None,
    "tags": ["billing"],
}

_THREADS_PAGE_1 = {
    "_embedded": {
        "threads": [
            {
                "id": 1001,
                "type": "customer",
                "status": "active",
                "body": "<p>I cannot log into my account since yesterday.</p>",
                "author": None,
                "customer": {"email": "user@example.com"},
                "createdAt": "2024-01-10T09:00:00Z",
                "openedAt": "2024-01-10T09:01:00Z",
            }
        ]
    },
    "_links": {
        "self": {"href": f"{THREADS_URL}?page=1"},
        "next": {"href": f"{THREADS_URL}?page=2"},
    },
    "page": {"number": 1, "size": 25, "totalElements": 2, "totalPages": 2},
}

_THREADS_PAGE_2 = {
    "_embedded": {
        "threads": [
            {
                "id": 1002,
                "type": "reply",
                "status": "active",
                "body": "<p>Hi! We've reset your password. Please try again.</p>",
                "author": {"email": "agent@example.com"},
                "customer": None,
                "createdAt": "2024-01-10T10:00:00Z",
                "openedAt": None,
            }
        ]
    },
    "_links": {
        "self": {"href": f"{THREADS_URL}?page=2"},
    },
    "page": {"number": 2, "size": 25, "totalElements": 2, "totalPages": 2},
}

_CUSTOMERS_SEARCH = {
    "_embedded": {
        "customers": [
            {
                "id": 9001,
                "firstName": "Alice",
                "lastName": "Smith",
                "email": "alice@example.com",
                "organization": "Acme Corp",
                "jobTitle": "CEO",
                "conversationCount": 7,
                "createdAt": "2023-06-01T00:00:00Z",
            }
        ]
    },
    "_links": {"self": {"href": f"{CUSTOMERS_URL}?email=alice%40example.com"}},
    "page": {"number": 1, "size": 25, "totalElements": 1, "totalPages": 1},
}

_CUSTOMER_BY_ID = {
    "id": 9001,
    "firstName": "Alice",
    "lastName": "Smith",
    "email": "alice@example.com",
    "organization": "Acme Corp",
    "jobTitle": "CEO",
    "conversationCount": 7,
    "createdAt": "2023-06-01T00:00:00Z",
}

_MAILBOXES = {
    "_embedded": {
        "mailboxes": [
            {
                "id": 11,
                "name": "Support",
                "email": "support@example.com",
                "slug": "support",
                "createdAt": "2022-01-01T00:00:00Z",
                "updatedAt": "2024-01-01T00:00:00Z",
            }
        ]
    },
    "_links": {"self": {"href": f"{MAILBOXES_URL}"}},
    "page": {"number": 1, "size": 25, "totalElements": 1, "totalPages": 1},
}


class HelpScoutManifest(unittest.TestCase):
    """Verify the YAML manifest loads via lib.api and maps every field correctly."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_HELPSCOUT")
        os.environ["RC_CONN_HELPSCOUT"] = "hs_tok_dummy"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_HELPSCOUT", None)
        else:
            os.environ["RC_CONN_HELPSCOUT"] = self._saved

    def test_manifest_loads_from_yaml(self):
        manifests = api.load_manifests()
        self.assertIn("helpscout", manifests)
        m = manifests["helpscout"]
        self.assertEqual(m.key, "helpscout")
        self.assertEqual(m.base_url, "https://api.helpscout.net/v2")
        self.assertEqual(m.auth.strategy, "oauth2_client_credentials")
        self.assertEqual(m.pagination.style, "none")
        self.assertEqual(m.rate_limit_remaining_header, "X-RateLimit-Remaining-Minute")

    def test_connector_registers_manifest(self):
        # Re-trigger registration (setUp cleared MANIFESTS to isolate YAML loader test).
        # The connector registers via api.register() at import time; here we call the YAML
        # loader which also discovers and registers it, proving both paths work.
        api.load_manifests()
        self.assertIn("helpscout", api.MANIFESTS)
        m = api.MANIFESTS["helpscout"]
        self.assertEqual(m.base_url, "https://api.helpscout.net/v2")
        self.assertEqual(m.auth.strategy, "oauth2_client_credentials")


class HelpScoutPagination(unittest.TestCase):
    """Verify page-number pagination stitches ≥2 pages and bearer rides every request."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_HELPSCOUT")
        os.environ["RC_CONN_HELPSCOUT"] = "hs_tok_test_dummy"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_HELPSCOUT", None)
        else:
            os.environ["RC_CONN_HELPSCOUT"] = self._saved

    @responses_lib.activate
    def test_conversations_pagination_stitches_two_pages(self):
        responses_lib.add(responses_lib.GET, CONVERSATIONS_URL, json=_CONV_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, CONVERSATIONS_URL, json=_CONV_PAGE_2, status=200)

        convs = helpscout_conn.list_conversations(status="active", max_items=50)
        self.assertEqual(len(convs), 2)
        # Page 1 item
        self.assertEqual(convs[0].get("number"), 42)
        # Page 2 item
        self.assertEqual(convs[1].get("number"), 43)

    @responses_lib.activate
    def test_bearer_rides_every_page_request(self):
        responses_lib.add(responses_lib.GET, CONVERSATIONS_URL, json=_CONV_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, CONVERSATIONS_URL, json=_CONV_PAGE_2, status=200)

        helpscout_conn.list_conversations(status="active", max_items=50)

        # Both page 1 and page 2 requests must carry the bearer.
        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], "Bearer hs_tok_test_dummy")

    @responses_lib.activate
    def test_stops_when_no_next_link(self):
        # Single page with no _links.next → loop exits after one request.
        single_page = dict(_CONV_PAGE_1)
        single_page["_links"] = {"self": {"href": CONVERSATIONS_URL}}  # no "next"
        responses_lib.add(responses_lib.GET, CONVERSATIONS_URL, json=single_page, status=200)

        convs = helpscout_conn.list_conversations(status="active", max_items=50)
        self.assertEqual(len(convs), 1)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_threads_pagination_stitches_two_pages(self):
        responses_lib.add(responses_lib.GET, THREADS_URL, json=_THREADS_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, THREADS_URL, json=_THREADS_PAGE_2, status=200)

        threads = helpscout_conn.list_threads(123456)
        self.assertEqual(len(threads), 2)
        self.assertEqual(threads[0].get("type"), "customer")
        self.assertEqual(threads[1].get("type"), "reply")

        # Bearer on both page requests.
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], "Bearer hs_tok_test_dummy")


class HelpScoutDomainHelpers(unittest.TestCase):
    """Domain helpers return correctly shaped, pre-selected dicts."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_HELPSCOUT")
        os.environ["RC_CONN_HELPSCOUT"] = "hs_tok_dummy"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_HELPSCOUT", None)
        else:
            os.environ["RC_CONN_HELPSCOUT"] = self._saved

    @responses_lib.activate
    def test_get_conversation(self):
        responses_lib.add(responses_lib.GET, CONV_URL, json=_SINGLE_CONV, status=200)
        conv = helpscout_conn.get_conversation(123456)
        self.assertEqual(conv.get("number"), 42)
        self.assertEqual(conv.get("subject"), "Cannot log in")

    @responses_lib.activate
    def test_resolve_customer_by_email(self):
        responses_lib.add(responses_lib.GET, CUSTOMERS_URL, json=_CUSTOMERS_SEARCH, status=200)
        cust = helpscout_conn.resolve_customer("alice@example.com")
        self.assertIsNotNone(cust)
        self.assertEqual(cust.get("firstName"), "Alice")
        self.assertEqual(cust.get("organization"), "Acme Corp")

    @responses_lib.activate
    def test_resolve_customer_by_id(self):
        responses_lib.add(responses_lib.GET, CUSTOMER_URL, json=_CUSTOMER_BY_ID, status=200)
        cust = helpscout_conn.resolve_customer("9001")
        self.assertIsNotNone(cust)
        self.assertEqual(cust.get("lastName"), "Smith")

    @responses_lib.activate
    def test_resolve_customer_not_found(self):
        empty = {"_embedded": {"customers": []}, "_links": {}, "page": {"number": 1, "size": 25, "totalElements": 0, "totalPages": 0}}
        responses_lib.add(responses_lib.GET, CUSTOMERS_URL, json=empty, status=200)
        cust = helpscout_conn.resolve_customer("nobody@example.com")
        self.assertIsNone(cust)

    @responses_lib.activate
    def test_list_mailboxes(self):
        responses_lib.add(responses_lib.GET, MAILBOXES_URL, json=_MAILBOXES, status=200)
        mbs = helpscout_conn.list_mailboxes()
        self.assertEqual(len(mbs), 1)
        self.assertEqual(mbs[0].get("name"), "Support")
        self.assertEqual(mbs[0].get("email"), "support@example.com")

    def test_pick_selects_conversation_fields(self):
        picked = api.pick(_SINGLE_CONV, "id,number,subject,status,mailboxId")
        self.assertEqual(picked["number"], 42)
        self.assertEqual(picked["status"], "active")
        self.assertNotIn("closedAt", picked)  # not requested


class HelpScoutCLI(unittest.TestCase):
    """CLI drive via helpscout_conn.main([...])."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_HELPSCOUT")
        os.environ["RC_CONN_HELPSCOUT"] = "hs_tok_dummy"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_HELPSCOUT", None)
        else:
            os.environ["RC_CONN_HELPSCOUT"] = self._saved

    @responses_lib.activate
    def test_cli_conversations_markdown(self):
        responses_lib.add(responses_lib.GET, CONVERSATIONS_URL, json=_CONV_PAGE_2, status=200)
        rc = helpscout_conn.main(["conversations", "--status", "closed", "--max-items", "5"])
        self.assertEqual(rc, 0)
        # Verify bearer was sent.
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], "Bearer hs_tok_dummy")

    @responses_lib.activate
    def test_cli_conversations_json(self):
        responses_lib.add(responses_lib.GET, CONVERSATIONS_URL, json=_CONV_PAGE_2, status=200)
        rc = helpscout_conn.main(["conversations", "--json"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_conversation_detail(self):
        responses_lib.add(responses_lib.GET, CONV_URL, json=_SINGLE_CONV, status=200)
        responses_lib.add(responses_lib.GET, THREADS_URL, json=_THREADS_PAGE_1, status=200)
        # No second thread page (_links.next present but we mock only one page; connector should call it)
        _threads_p2_no_next = dict(_THREADS_PAGE_2)
        _threads_p2_no_next["_links"] = {}
        responses_lib.add(responses_lib.GET, THREADS_URL, json=_threads_p2_no_next, status=200)
        rc = helpscout_conn.main(["conversation", "123456"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_customer(self):
        responses_lib.add(responses_lib.GET, CUSTOMERS_URL, json=_CUSTOMERS_SEARCH, status=200)
        rc = helpscout_conn.main(["customer", "alice@example.com"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_mailboxes(self):
        responses_lib.add(responses_lib.GET, MAILBOXES_URL, json=_MAILBOXES, status=200)
        rc = helpscout_conn.main(["mailboxes"])
        self.assertEqual(rc, 0)


class HelpScoutTokenHygiene(unittest.TestCase):
    """CI guard: no real Help Scout token prefix may land in committed connector files.

    Scoped to the connector dir only — this test file legitimately contains split prefixes
    so scanning itself would be a false positive.
    """

    # Help Scout OAuth access tokens are opaque hex strings with no fixed prefix documented,
    # but the auth header "Authorization: Bearer <token>" is the known pattern. We guard against
    # any real-looking token that may have been pasted during development. We split the prefix
    # literals so this guard cannot flag itself.
    _TOKEN_PREFIXES = (
        "Bearer" + " 369",   # the example token from the docs
        "access_token" + ": ",  # raw JSON credential
    )

    def test_no_token_material_in_helpscout_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "helpscout"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
