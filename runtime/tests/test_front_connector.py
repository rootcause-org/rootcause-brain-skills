"""Fixture test for the manifest-ONLY Front integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies mirror Front's
documented example payloads (dev.frontapp.com/reference), trimmed to support-relevant fields.
Front paginates with a cursor: `_pagination.next` in the response body carries the next-page URL
(or null when exhausted), and the cursor value is sent as the `page_token` query param.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \
        pytest tests/test_front_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://api2.frontapp.com"
CONVERSATIONS = f"{BASE}/conversations"
MESSAGES = f"{BASE}/conversations/cnv_abc123/messages"

# ---------------------------------------------------------------------------
# Documented example payloads — shapes mirror Front API reference, trimmed to
# the fields most useful for support grounding.
# ---------------------------------------------------------------------------

# Page 1: two conversations; _pagination.next points at page 2 with an opaque page_token.
_CONV_PAGE_1 = {
    "_pagination": {
        "next": f"{CONVERSATIONS}?page_token=tok_page2&limit=100",
    },
    "_results": [
        {
            "id": "cnv_abc123",
            "subject": "Cannot log in",
            "status": "open",
            "assignee": {"email": "alice@example.com"},
            "tags": [{"name": "bug"}, {"name": "urgent"}],
        },
    ],
}

# Page 2: one conversation; _pagination.next is null → loop terminates.
_CONV_PAGE_2 = {
    "_pagination": {"next": None},
    "_results": [
        {
            "id": "cnv_def456",
            "subject": "Billing question",
            "status": "archived",
            "assignee": {"email": "bob@example.com"},
            "tags": [{"name": "billing"}],
        },
    ],
}

# Single-page response for messages in a conversation.
_MESSAGES_PAGE = {
    "_pagination": {"next": None},
    "_results": [
        {
            "id": "msg_111",
            "type": "email",
            "author": {"email": "customer@example.com"},
            "text": "I can't log in to the dashboard.",
            "created_at": 1700000000,
        },
        {
            "id": "msg_222",
            "type": "email",
            "author": {"email": "alice@example.com"},
            "text": "We are looking into it.",
            "created_at": 1700001000,
        },
    ],
}

# Page-2 URL that the cursor-follow request will hit (from _pagination.next above).
_PAGE_2_URL = f"{CONVERSATIONS}?page_token=tok_page2&limit=100"


class FrontManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates 'front' (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_FRONT")
        # Split the prefix so the token-hygiene guard in this file doesn't flag itself.
        os.environ["RC_CONN_FRONT"] = "Bearer" "_front_test_token_abc"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_FRONT", None)
        else:
            os.environ["RC_CONN_FRONT"] = self._saved

    # ------------------------------------------------------------------
    # 1. YAML manifest loads and maps every relevant field.
    # ------------------------------------------------------------------

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("front", m)
        f = m["front"]
        self.assertEqual(f.key, "front")
        self.assertEqual(f.base_url, "https://api2.frontapp.com")
        self.assertEqual(f.auth.strategy, "bearer")
        self.assertEqual(f.pagination.style, "cursor")
        self.assertEqual(f.pagination.cursor_field, "_pagination.next")
        self.assertEqual(f.pagination.cursor_param, "page_token")
        self.assertEqual(f.pagination.items_field, "_results")
        self.assertEqual(f.pagination.has_more_field, "")   # absent: loop until cursor is empty
        self.assertEqual(f.pagination.page_size, 100)
        self.assertEqual(f.rate_limit_remaining_header, "x-ratelimit-remaining")

    # ------------------------------------------------------------------
    # 2. Cursor pagination stitches ≥ 2 pages; credential rides every call.
    # ------------------------------------------------------------------

    @responses.activate
    def test_cursor_pagination_stitches_pages(self):
        # Page 1: /conversations → _pagination.next carries the page-2 URL.
        responses.add(
            responses.GET, CONVERSATIONS,
            json=_CONV_PAGE_1, status=200,
            headers={"x-ratelimit-remaining": "199"},
        )
        # Page 2: the lib.api cursor loop sends page_token as a query param.
        # We match on the base URL; the cursor param is added by the framework.
        responses.add(
            responses.GET, CONVERSATIONS,
            json=_CONV_PAGE_2, status=200,
            headers={"x-ratelimit-remaining": "198"},
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["front"])
        result = c.collect("conversations", query={"limit": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, ["cnv_abc123", "cnv_def456"])  # both pages stitched

    @responses.activate
    def test_bearer_credential_rides_every_request(self):
        responses.add(responses.GET, CONVERSATIONS, json=_CONV_PAGE_1, status=200)
        responses.add(responses.GET, CONVERSATIONS, json=_CONV_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["front"])
        c.collect("conversations", query={"limit": 100})

        # Both calls (page 1 and the cursor follow) must carry the Authorization header.
        for call in responses.calls:
            self.assertIn(
                "Bearer" "_front_test_token_abc",
                call.request.headers.get("Authorization", ""),
            )

    # ------------------------------------------------------------------
    # 3. api.pick selects support-relevant fields.
    # ------------------------------------------------------------------

    @responses.activate
    def test_pick_selects_support_fields(self):
        responses.add(responses.GET, CONVERSATIONS, json=_CONV_PAGE_1, status=200)
        responses.add(responses.GET, CONVERSATIONS, json=_CONV_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["front"])
        result = c.collect("conversations", query={"limit": 100})

        picked = [api.pick(it, "id,subject,status,assignee.email,tags.*.name") for it in result["items"]]
        self.assertEqual(picked[0]["id"], "cnv_abc123")
        self.assertEqual(picked[0]["status"], "open")
        self.assertEqual(picked[0]["assignee.email"], "alice@example.com")
        self.assertEqual(picked[0]["tags.*.name"], ["bug", "urgent"])
        self.assertEqual(picked[1]["id"], "cnv_def456")
        self.assertEqual(picked[1]["tags.*.name"], ["billing"])

    # ------------------------------------------------------------------
    # 4. Single-page endpoint (messages) works with pagination style=cursor,
    #    terminating when _pagination.next is null.
    # ------------------------------------------------------------------

    @responses.activate
    def test_single_page_messages(self):
        responses.add(responses.GET, MESSAGES, json=_MESSAGES_PAGE, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["front"])
        result = c.collect("conversations/cnv_abc123/messages")

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 2)
        picked = [api.pick(it, "id,type,author.email,text") for it in result["items"]]
        self.assertEqual(picked[0]["author.email"], "customer@example.com")
        self.assertEqual(picked[1]["author.email"], "alice@example.com")

    # ------------------------------------------------------------------
    # 5. CLI drive works through api._main.
    # ------------------------------------------------------------------

    @responses.activate
    def test_cli_drives_front_with_bearer_and_paginate(self):
        responses.add(responses.GET, CONVERSATIONS, json=_CONV_PAGE_1, status=200)
        responses.add(responses.GET, CONVERSATIONS, json=_CONV_PAGE_2, status=200)

        rc = api._main([
            "get", "front", "conversations",
            "--query", "limit=100",
            "--paginate",
            "--pick", "id,status",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(responses.calls[0].request.url.startswith(CONVERSATIONS))
        self.assertIn(
            "Bearer" "_front_test_token_abc",
            responses.calls[0].request.headers.get("Authorization", ""),
        )
        self.assertEqual(len(responses.calls), 2)


# ---------------------------------------------------------------------------
# Token-prefix hygiene guard — scoped to the connector dir only.
# The test legitimately NAMES the prefixes it hunts, so it scans only the
# connector directory to avoid a false-positive on itself.
# ---------------------------------------------------------------------------


class FrontCassetteHygiene(unittest.TestCase):
    """CI guard: no real Front token prefix may land in the committed connector files."""

    # Front API tokens are JWTs (Bearer eyJ...) or opaque strings with no published prefix.
    # Guard against common secret leaks that could slip in via copy-paste.
    # The split below prevents the hygiene guard from flagging itself.
    _TOKEN_PREFIXES = (
        "eyJ" "bG9nIjoiZ",   # base64 JWT header fragment (never a real token here)
        "Bearer" " eyJ",     # full JWT bearer prefix as it would appear in a secret file
    )

    def test_no_token_prefixes_in_front_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "front"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains suspicious token fragment")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
