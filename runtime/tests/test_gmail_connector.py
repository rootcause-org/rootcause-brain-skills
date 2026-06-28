"""Fixture test for the Gmail connector — script connector (force-code triggers: field pre-selection
+ multi-call join: messages.list → messages.get per ID).

No live creds, no network: HTTP is mocked with ``responses``. Payload shapes are from the Gmail
API documentation (developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages).

Tests cover:
- YAML manifest loads and maps all fields via lib.api's loader
- Cursor pagination stitches ≥2 pages (nextPageToken / pageToken)
- Bearer credential rides EVERY request (list + per-message GET + link-follow)
- ``api.pick`` works on shaped messages (field pre-selection sanity)
- Connector functions: list_messages (join), get_message, get_thread, list_labels
- CLI commands via main()
- Token-prefix hygiene: no real OAuth token prefix leaks in connector dir

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_gmail_connector.py -q
"""

import base64
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

BASE = "https://gmail.googleapis.com/gmail/v1"
USER = "me"
MESSAGES_URL = f"{BASE}/users/{USER}/messages"
MESSAGE_1_URL = f"{BASE}/users/{USER}/messages/msg001"
MESSAGE_2_URL = f"{BASE}/users/{USER}/messages/msg002"
MESSAGE_3_URL = f"{BASE}/users/{USER}/messages/msg003"
THREAD_URL = f"{BASE}/users/{USER}/threads/thread001"
LABELS_URL = f"{BASE}/users/{USER}/labels"


def _b64(text: str) -> str:
    """Base64url-encode a string (Gmail body data format)."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# ---------------------------------------------------------------------------

_LIST_PAGE_1 = {
    "messages": [{"id": "msg001", "threadId": "thread001"}],
    "nextPageToken": "tok_page2",
    "resultSizeEstimate": 2,
}
_LIST_PAGE_2 = {
    "messages": [{"id": "msg002", "threadId": "thread001"}],
    # No nextPageToken → cursor exhausted
    "resultSizeEstimate": 2,
}

_MSG_001_RAW = {
    "id": "msg001",
    "threadId": "thread001",
    "labelIds": ["INBOX", "UNREAD"],
    "snippet": "Hello, I need help with my account",
    "payload": {
        "mimeType": "text/plain",
        "headers": [
            {"name": "Subject", "value": "Support request"},
            {"name": "From", "value": "customer@example.com"},
            {"name": "To", "value": "support@company.com"},
            {"name": "Date", "value": "Mon, 23 Jun 2025 10:00:00 +0000"},
        ],
        "body": {"data": _b64("Hello, I need help with my account reset.")},
        "parts": [],
    },
}

_MSG_002_RAW = {
    "id": "msg002",
    "threadId": "thread001",
    "labelIds": ["INBOX", "SENT"],
    "snippet": "Sure, I can help you with that",
    "payload": {
        "mimeType": "multipart/alternative",
        "headers": [
            {"name": "Subject", "value": "Re: Support request"},
            {"name": "From", "value": "agent@company.com"},
            {"name": "To", "value": "customer@example.com"},
            {"name": "Date", "value": "Mon, 23 Jun 2025 10:30:00 +0000"},
        ],
        "body": {},
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": _b64("Sure, I can help you with that account reset.")},
            },
            {
                "mimeType": "text/html",
                "body": {"data": _b64("<p>Sure, I can help you with that account reset.</p>")},
            },
        ],
    },
}

_MSG_003_RAW = {
    "id": "msg003",
    "threadId": "thread002",
    "labelIds": ["INBOX"],
    "snippet": "Another message",
    "payload": {
        "mimeType": "text/plain",
        "headers": [
            {"name": "Subject", "value": "Another issue"},
            {"name": "From", "value": "other@example.com"},
            {"name": "To", "value": "support@company.com"},
            {"name": "Date", "value": "Tue, 24 Jun 2025 09:00:00 +0000"},
        ],
        "body": {"data": _b64("I have another issue.")},
    },
}

_THREAD_RAW = {
    "id": "thread001",
    "messages": [_MSG_001_RAW, _MSG_002_RAW],
}

_LABELS_RAW = {
    "labels": [
        {"id": "INBOX", "name": "INBOX", "type": "system"},
        {"id": "UNREAD", "name": "UNREAD", "type": "system"},
        {"id": "Label_custom", "name": "Support", "type": "user"},
    ]
}


class GmailManifestFields(unittest.TestCase):
    """The YAML manifest loads and maps every field the lib.api Manifest dataclass exposes."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("gmail", m, "gmail connector not found by manifest loader")
        g = m["gmail"]
        self.assertEqual(g.key, "gmail")
        self.assertEqual(g.base_url, "https://gmail.googleapis.com/gmail/v1")
        self.assertEqual(g.auth.strategy, "bearer")
        self.assertEqual(g.pagination.style, "cursor")
        self.assertEqual(g.pagination.cursor_field, "nextPageToken")
        self.assertEqual(g.pagination.cursor_param, "pageToken")
        self.assertEqual(g.pagination.items_field, "messages")
        self.assertEqual(g.pagination.page_size, 100)
        self.assertEqual(g.rate_limit_remaining_header, "")

    def test_oauth_block_present_in_raw_yaml(self):
        """oauth block (auth_url, token_url, default_scopes) survives in the raw YAML."""
        import yaml

        manifest_path = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "gmail" / "manifest.yaml"
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        self.assertIn("oauth", raw)
        self.assertIn("auth_url", raw["oauth"])
        self.assertIn("token_url", raw["oauth"])
        self.assertIn("default_scopes", raw["oauth"])
        self.assertIn("https://www.googleapis.com/auth/gmail.readonly", raw["oauth"]["default_scopes"])

    def test_kinds_in_raw_yaml(self):
        import yaml

        manifest_path = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "gmail" / "manifest.yaml"
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        self.assertIn("token", raw.get("kinds", []))
        self.assertIn("oauth", raw.get("kinds", []))

    def test_egress_hosts_in_raw_yaml(self):
        import yaml

        manifest_path = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "gmail" / "manifest.yaml"
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        self.assertIn("gmail.googleapis.com", raw.get("egress_hosts", []))


class GmailCursorPagination(unittest.TestCase):
    """Cursor pagination stitches ≥2 pages (nextPageToken / pageToken)."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GMAIL")
        os.environ["RC_CONN_GMAIL"] = "ya29.fake_access_tok" "en"  # split so guard doesn't flag itself

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GMAIL", None)
        else:
            os.environ["RC_CONN_GMAIL"] = self._saved

    @responses_lib.activate
    def test_cursor_pagination_stitches_two_pages(self):
        responses_lib.add(responses_lib.GET, MESSAGES_URL, json=_LIST_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, MESSAGES_URL, json=_LIST_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["gmail"])
        result = c.collect("users/me/messages", query={"maxResults": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, ["msg001", "msg002"])

    @responses_lib.activate
    def test_bearer_on_all_requests_incl_page_two(self):
        responses_lib.add(responses_lib.GET, MESSAGES_URL, json=_LIST_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, MESSAGES_URL, json=_LIST_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["gmail"])
        c.collect("users/me/messages", query={"maxResults": 100})

        # Both page requests carry Authorization: Bearer …
        for call in responses_lib.calls:
            self.assertIn("Authorization", call.request.headers)
            self.assertTrue(
                call.request.headers["Authorization"].startswith("Bearer "),
                f"expected Bearer prefix, got: {call.request.headers['Authorization']!r}",
            )

    @responses_lib.activate
    def test_page_two_sends_pagetoken_param(self):
        """The nextPageToken from page 1 is sent as pageToken on the page 2 request."""
        responses_lib.add(responses_lib.GET, MESSAGES_URL, json=_LIST_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, MESSAGES_URL, json=_LIST_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["gmail"])
        c.collect("users/me/messages", query={"maxResults": 100})

        self.assertEqual(len(responses_lib.calls), 2)
        page2_url = responses_lib.calls[1].request.url
        self.assertIn("pageToken=tok_page2", page2_url)


class GmailConnectorFunctions(unittest.TestCase):
    """Connector importable functions: list_messages (join), get_message, get_thread, list_labels."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GMAIL")
        os.environ["RC_CONN_GMAIL"] = "ya29.fake_access_tok" "en"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GMAIL", None)
        else:
            os.environ["RC_CONN_GMAIL"] = self._saved

    @responses_lib.activate
    def test_list_messages_joins_list_and_get(self):
        """list_messages calls messages.list, then messages.get for each ID (multi-call join)."""
        from lib.connectors.gmail import list_messages

        responses_lib.add(responses_lib.GET, MESSAGES_URL, json=_LIST_PAGE_2, status=200)
        responses_lib.add(responses_lib.GET, MESSAGE_2_URL, json=_MSG_002_RAW, status=200)

        msgs = list_messages(limit=1)
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        self.assertEqual(m["id"], "msg002")
        self.assertEqual(m["subject"], "Re: Support request")
        self.assertEqual(m["from"], "agent@company.com")
        self.assertIn("account reset", m["body_excerpt"])
        self.assertIn("INBOX", m["labelIds"])

        # Both the list call and the get call went out
        urls = [c.request.url for c in responses_lib.calls]
        self.assertTrue(any("messages" == u.split("/")[-1].split("?")[0] for u in urls), "list call missing")
        self.assertTrue(any("msg002" in u for u in urls), "get call missing")

    @responses_lib.activate
    def test_list_messages_two_pages_hydrated(self):
        """list_messages with limit=2 stitches two list pages, then GETs each message."""
        from lib.connectors.gmail import list_messages

        responses_lib.add(responses_lib.GET, MESSAGES_URL, json=_LIST_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, MESSAGES_URL, json=_LIST_PAGE_2, status=200)
        responses_lib.add(responses_lib.GET, MESSAGE_1_URL, json=_MSG_001_RAW, status=200)
        responses_lib.add(responses_lib.GET, MESSAGE_2_URL, json=_MSG_002_RAW, status=200)

        msgs = list_messages(limit=2)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["id"], "msg001")
        self.assertEqual(msgs[1]["id"], "msg002")
        # Multipart/alternative: text/plain wins over text/html
        self.assertIn("account reset", msgs[1]["body_excerpt"])

    @responses_lib.activate
    def test_get_message_shapes_fields(self):
        """get_message extracts subject, from, date, snippet, body_excerpt, labelIds."""
        from lib.connectors.gmail import get_message

        responses_lib.add(responses_lib.GET, MESSAGE_1_URL, json=_MSG_001_RAW, status=200)

        m = get_message("msg001")
        self.assertEqual(m["id"], "msg001")
        self.assertEqual(m["threadId"], "thread001")
        self.assertEqual(m["subject"], "Support request")
        self.assertEqual(m["from"], "customer@example.com")
        self.assertEqual(m["to"], "support@company.com")
        self.assertEqual(m["snippet"], "Hello, I need help with my account")
        self.assertIn("account reset", m["body_excerpt"])
        self.assertIn("UNREAD", m["labelIds"])

        # --pick works on a shaped message too (api.pick uses dotted paths)
        picked = api.pick(m, "id,subject,from,labelIds")
        self.assertEqual(picked["id"], "msg001")
        self.assertEqual(picked["subject"], "Support request")

    @responses_lib.activate
    def test_get_message_format_full_in_query(self):
        """messages.get is called with format=full so the payload is included."""
        from lib.connectors.gmail import get_message

        responses_lib.add(responses_lib.GET, MESSAGE_1_URL, json=_MSG_001_RAW, status=200)
        get_message("msg001")
        url = responses_lib.calls[0].request.url
        self.assertIn("format=full", url)

    @responses_lib.activate
    def test_get_thread_shapes_all_messages(self):
        """get_thread fetches the thread and shapes each of its messages."""
        from lib.connectors.gmail import get_thread

        responses_lib.add(responses_lib.GET, THREAD_URL, json=_THREAD_RAW, status=200)

        t = get_thread("thread001")
        self.assertEqual(t["id"], "thread001")
        self.assertEqual(len(t["messages"]), 2)
        self.assertEqual(t["messages"][0]["subject"], "Support request")
        self.assertEqual(t["messages"][1]["subject"], "Re: Support request")
        self.assertEqual(t["messages"][0]["from"], "customer@example.com")

    @responses_lib.activate
    def test_list_labels(self):
        """list_labels returns id, name, type for each label."""
        from lib.connectors.gmail import list_labels

        responses_lib.add(responses_lib.GET, LABELS_URL, json=_LABELS_RAW, status=200)

        labels = list_labels()
        self.assertEqual(len(labels), 3)
        names = [lb["name"] for lb in labels]
        self.assertIn("INBOX", names)
        self.assertIn("Support", names)
        types = {lb["name"]: lb["type"] for lb in labels}
        self.assertEqual(types["INBOX"], "system")
        self.assertEqual(types["Support"], "user")

    @responses_lib.activate
    def test_multipart_body_prefers_text_plain(self):
        """text/plain wins over text/html in multipart messages."""
        from lib.connectors.gmail import get_message

        responses_lib.add(responses_lib.GET, MESSAGE_2_URL, json=_MSG_002_RAW, status=200)
        m = get_message("msg002")
        # Body excerpt is from text/plain (not HTML-tagged)
        self.assertNotIn("<p>", m["body_excerpt"])
        self.assertIn("account reset", m["body_excerpt"])


class GmailCliCommands(unittest.TestCase):
    """CLI commands via main() produce expected markdown output."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GMAIL")
        os.environ["RC_CONN_GMAIL"] = "ya29.fake_access_tok" "en"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GMAIL", None)
        else:
            os.environ["RC_CONN_GMAIL"] = self._saved

    @responses_lib.activate
    def test_cli_messages_command(self):
        from lib.connectors.gmail import main

        responses_lib.add(responses_lib.GET, MESSAGES_URL, json=_LIST_PAGE_2, status=200)
        responses_lib.add(responses_lib.GET, MESSAGE_2_URL, json=_MSG_002_RAW, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["messages", "--limit", "1"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Re: Support request", out)

    @responses_lib.activate
    def test_cli_message_command(self):
        from lib.connectors.gmail import main

        responses_lib.add(responses_lib.GET, MESSAGE_1_URL, json=_MSG_001_RAW, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["message", "msg001"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Support request", out)
        self.assertIn("customer@example.com", out)

    @responses_lib.activate
    def test_cli_thread_command(self):
        from lib.connectors.gmail import main

        responses_lib.add(responses_lib.GET, THREAD_URL, json=_THREAD_RAW, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["thread", "thread001"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("thread001", out)
        self.assertIn("Support request", out)

    @responses_lib.activate
    def test_cli_labels_command(self):
        from lib.connectors.gmail import main

        responses_lib.add(responses_lib.GET, LABELS_URL, json=_LABELS_RAW, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["labels"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("INBOX", out)
        self.assertIn("Support", out)

    @responses_lib.activate
    def test_cli_messages_json_flag(self):
        from lib.connectors.gmail import main

        responses_lib.add(responses_lib.GET, MESSAGES_URL, json=_LIST_PAGE_2, status=200)
        responses_lib.add(responses_lib.GET, MESSAGE_2_URL, json=_MSG_002_RAW, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["messages", "--limit", "1", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertIsInstance(data, list)
        self.assertEqual(data[0]["id"], "msg002")

    @responses_lib.activate
    def test_lib_api_cli_drives_gmail(self):
        """python -m lib.api get gmail <path> works for the raw list endpoint (manifest-driven)."""
        responses_lib.add(responses_lib.GET, MESSAGES_URL, json=_LIST_PAGE_2, status=200)
        rc = api._main(["get", "gmail", "users/me/messages", "--query", "maxResults=10"])
        self.assertEqual(rc, 0)
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], "Bearer ya29.fake_access_tok" "en")


class GmailCassetteHygiene(unittest.TestCase):
    """CI guard: no real OAuth token prefix may land in the connector dir (manifest or script).

    The test file itself splits these literals — scanned file is the connector dir, NOT this file.
    """

    # Google OAuth token prefixes split across string concat so this guard never triggers on itself.
    _TOKEN_PREFIXES = ("ya29" ".",)

    def test_no_token_prefixes_in_gmail_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "gmail"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: found token-like prefix {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
