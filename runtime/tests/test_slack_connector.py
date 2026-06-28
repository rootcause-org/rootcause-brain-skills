"""Fixture test for the manifest-ONLY Slack integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. Bodies mirror Slack's documented
example payloads (https://docs.slack.dev), trimmed to support-relevant fields. Slack paginates
with `response_metadata.next_cursor` cursor tokens, so the two mocked pages exercise the real
`cursor` pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_slack_connector.py -q
"""

import json
import os
import sys
import unittest
from pathlib import Path

import responses as rsps_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://slack.com/api"
CONVERSATIONS_LIST = f"{BASE}/conversations.list"
USERS_INFO = f"{BASE}/users.info"
CONVERSATIONS_HISTORY = f"{BASE}/conversations.history"

# --- Documented example payloads (trimmed to support-relevant fields) ---

_CHANNELS_PAGE_1 = {
    "ok": True,
    "channels": [
        {"id": "C012AB3CD", "name": "general", "topic": {"value": "Company-wide announcements"}},
        {"id": "C012AB3CE", "name": "support", "topic": {"value": "Customer support queue"}},
    ],
    "response_metadata": {"next_cursor": "dGVhbS1uYXZpZ2F0aW9uLWNoYW5uZWxzOjE2MDYxNTA3"},
}

_CHANNELS_PAGE_2 = {
    "ok": True,
    "channels": [
        {"id": "C012AB3CF", "name": "random", "topic": {"value": ""}},
    ],
    # Empty next_cursor signals end of pages.
    "response_metadata": {"next_cursor": ""},
}

_USER_INFO = {
    "ok": True,
    "user": {
        "id": "U012AB3CDE",
        "real_name": "Spengler",
        "is_bot": False,
        "profile": {
            "email": "spengler@ghostbusters.example.com",
            "display_name": "spengler",
        },
    },
}

_MESSAGES_PAGE_1 = {
    "ok": True,
    "messages": [
        {
            "ts": "1512085950.000216",
            "user": "U012AB3CDE",
            "text": "Payment failed for invoice INV-001",
        },
    ],
    "has_more": True,
    "response_metadata": {"next_cursor": "bmV4dF90czoxNTEyMDg1OTUwMDAwMjE2"},
}

_MESSAGES_PAGE_2 = {
    "ok": True,
    "messages": [
        {
            "ts": "1512085900.000200",
            "user": "U012AB3CDF",
            "text": "Retrying charge now",
        },
    ],
    "has_more": False,
    "response_metadata": {"next_cursor": ""},
}


class SlackManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates 'slack'.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_SLACK")
        # Split prefix literal so the token-prefix hygiene guard doesn't flag this test file.
        os.environ["RC_CONN_SLACK"] = "xoxb" "-test-slack-token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_SLACK", None)
        else:
            os.environ["RC_CONN_SLACK"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("slack", m)
        s = m["slack"]
        self.assertEqual(s.base_url, "https://slack.com/api")
        self.assertEqual(s.auth.strategy, "bearer")
        self.assertEqual(s.pagination.style, "cursor")
        self.assertEqual(s.pagination.cursor_param, "cursor")
        self.assertEqual(s.pagination.cursor_field, "response_metadata.next_cursor")
        self.assertEqual(s.pagination.has_more_field, "")
        self.assertEqual(s.pagination.page_size, 200)
        self.assertEqual(s.rate_limit_remaining_header, "")

    @rsps_lib.activate
    def test_cursor_pagination_stitches_two_pages(self):
        """cursor loop: page 1 carries a next_cursor → page 2 fetched; empty cursor stops loop."""
        rsps_lib.add(rsps_lib.GET, CONVERSATIONS_LIST, json=_CHANNELS_PAGE_1, status=200)
        rsps_lib.add(rsps_lib.GET, CONVERSATIONS_LIST, json=_CHANNELS_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["slack"])
        # items_field is "" so the page body is used as the list; but channels are under 'channels'
        # key, so we collect raw pages and check the body directly.
        pages = list(c.paginate(
            "conversations.list",
            query={"types": "public_channel", "limit": 200},
        ))
        self.assertEqual(len(pages), 2)
        # Page 1 returned a next token; page 2 has empty cursor → stopped.
        self.assertIsNotNone(pages[0].next)
        self.assertIsNone(pages[1].next)
        # Channel names accessible through body.
        names_p1 = [ch["name"] for ch in pages[0].body["channels"]]
        names_p2 = [ch["name"] for ch in pages[1].body["channels"]]
        self.assertEqual(names_p1, ["general", "support"])
        self.assertEqual(names_p2, ["random"])

    @rsps_lib.activate
    def test_bearer_credential_on_every_request_incl_paginated(self):
        """Bearer token must ride on every request, including cursor-followed pages."""
        rsps_lib.add(rsps_lib.GET, CONVERSATIONS_LIST, json=_CHANNELS_PAGE_1, status=200)
        rsps_lib.add(rsps_lib.GET, CONVERSATIONS_LIST, json=_CHANNELS_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["slack"])
        list(c.paginate("conversations.list"))

        expected = "Bearer " + "xoxb" + "-test-slack-token"
        for call in rsps_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], expected)

    @rsps_lib.activate
    def test_single_page_get_and_pick(self):
        """Single non-paginated GET for users.info + field pre-selection via api.pick."""
        rsps_lib.add(rsps_lib.GET, USERS_INFO, json=_USER_INFO, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["slack"])
        body = c.get("users.info", query={"user": "U012AB3CDE"})
        self.assertTrue(body["ok"])
        picked = api.pick(body, "user.real_name,user.profile.email,user.is_bot")
        self.assertEqual(picked["user.real_name"], "Spengler")
        self.assertEqual(picked["user.profile.email"], "spengler@ghostbusters.example.com")
        self.assertEqual(picked["user.is_bot"], False)

    @rsps_lib.activate
    def test_message_history_cursor_pagination(self):
        """conversations.history pages via response_metadata.next_cursor, items in 'messages'."""
        rsps_lib.add(rsps_lib.GET, CONVERSATIONS_HISTORY, json=_MESSAGES_PAGE_1, status=200)
        rsps_lib.add(rsps_lib.GET, CONVERSATIONS_HISTORY, json=_MESSAGES_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["slack"])
        pages = list(c.paginate(
            "conversations.history",
            query={"channel": "C012AB3CD", "limit": 200},
        ))
        self.assertEqual(len(pages), 2)
        texts = [pages[0].body["messages"][0]["text"], pages[1].body["messages"][0]["text"]]
        self.assertIn("Payment failed", texts[0])
        self.assertIn("Retrying", texts[1])

    @rsps_lib.activate
    def test_cli_drives_slack_with_bearer(self):
        """CLI `python -m lib.api get slack conversations.list` resolves the manifest and fires."""
        rsps_lib.add(rsps_lib.GET, CONVERSATIONS_LIST, json=_CHANNELS_PAGE_1, status=200)
        rsps_lib.add(rsps_lib.GET, CONVERSATIONS_LIST, json=_CHANNELS_PAGE_2, status=200)

        rc = api._main([
            "get", "slack", "conversations.list",
            "--query", "types=public_channel",
            "--query", "limit=200",
            "--paginate",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(rsps_lib.calls[0].request.url.startswith(CONVERSATIONS_LIST))
        expected_auth = "Bearer " + "xoxb" + "-test-slack-token"
        self.assertEqual(rsps_lib.calls[0].request.headers["Authorization"], expected_auth)
        self.assertEqual(len(rsps_lib.calls), 2)

    @rsps_lib.activate
    def test_cli_pick_selects_fields(self):
        """CLI --pick extracts nested paths from the response body."""
        rsps_lib.add(rsps_lib.GET, USERS_INFO, json=_USER_INFO, status=200)

        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = api._main([
                "get", "slack", "users.info",
                "--query", "user=U012AB3CDE",
                "--pick", "user.real_name,user.is_bot",
            ])
        self.assertEqual(rc, 0)
        output = json.loads(buf.getvalue())
        self.assertEqual(output["user.real_name"], "Spengler")
        self.assertEqual(output["user.is_bot"], False)


class SlackCassetteHygiene(unittest.TestCase):
    """CI guard: no real Slack token prefix may land in the committed manifest/fixtures.

    Scopes to the connector dir only — this test file legitimately names the prefixes it hunts
    for, so scanning itself would be a false positive.
    """

    # Slack token prefixes (split with concatenation so this guard doesn't flag itself).
    _TOKEN_PREFIXES = (
        "xoxb" "-",   # bot token
        "xoxp" "-",   # user token
        "xoxa" "-",   # app-level token
        "xoxs" "-",   # workspace token (legacy)
        "xoxr" "-",   # refresh token
    )

    def test_no_token_prefixes_in_slack_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "slack"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains {pref!r}")
        self.assertEqual(offenders, [], f"token-like material in connector dir: {offenders}")


if __name__ == "__main__":
    unittest.main()
