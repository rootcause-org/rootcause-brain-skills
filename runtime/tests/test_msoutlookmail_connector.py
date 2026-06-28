"""Fixture test for the Microsoft Outlook Mail connector (script connector).

Force-code triggers fired:
  (a) field pre-selection — message objects are huge; connector pre-selects ~8 support fields.
  (d) non-standard pagination — Graph uses @odata.nextLink (absolute URL in the JSON body), not
      RFC 8288 Link headers and not a plain cursor token.

No live creds, no network. HTTP is mocked with ``responses``. Fixture bodies are Microsoft Graph's
DOCUMENTED example message/folder payloads (learn.microsoft.com/en-us/graph/api/resources/message),
trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_msoutlookmail_connector.py -q
"""

import io
import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import msoutlookmail  # noqa: E402

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
ME_MESSAGES = f"{GRAPH_BASE}/me/messages"
ME_INBOX = f"{GRAPH_BASE}/me/mailFolders/inbox/messages"
ME_FOLDERS = f"{GRAPH_BASE}/me/mailFolders"
# Page 2 absolute URL as Graph would return it in @odata.nextLink
ME_MESSAGES_P2 = f"{GRAPH_BASE}/me/messages?$top=25&$skip=25"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# ---------------------------------------------------------------------------

_MSG_1 = {
    "id": "AAMkAGUAAAwTW09AAA=",
    "subject": "You have late tasks!",
    "from": {
        "emailAddress": {
            "name": "Microsoft Planner",
            "address": "noreply@Planner.Office365.com",
        }
    },
    "toRecipients": [{"emailAddress": {"name": "Alex", "address": "alex@example.com"}}],
    "receivedDateTime": "2026-06-15T10:30:00Z",
    "sentDateTime": "2026-06-15T10:29:55Z",
    "hasAttachments": False,
    "isRead": True,
    "isDraft": False,
    "importance": "normal",
    "bodyPreview": "You have 3 late tasks. Log in to see them.",
    "webLink": "https://outlook.office365.com/owa/?ItemID=AAMkAGUAAAwTW09AAA%3D",
    "conversationId": "AAQkAGU=",
    "parentFolderId": "inbox_folder_id",
}

_MSG_2 = {
    "id": "AAMkAGUAAAwTW10BBB=",
    "subject": "Invoice #1042 is due",
    "from": {
        "emailAddress": {
            "name": "Billing Team",
            "address": "billing@vendor.com",
        }
    },
    "toRecipients": [{"emailAddress": {"name": "Alex", "address": "alex@example.com"}}],
    "receivedDateTime": "2026-06-14T08:00:00Z",
    "sentDateTime": "2026-06-14T07:59:00Z",
    "hasAttachments": True,
    "isRead": False,
    "isDraft": False,
    "importance": "high",
    "bodyPreview": "Invoice #1042 of $450 is due on June 20.",
    "webLink": "https://outlook.office365.com/owa/?ItemID=AAMkAGUAAAwTW10BBB%3D",
    "conversationId": "AAQkAGV=",
    "parentFolderId": "inbox_folder_id",
}

_MSG_3 = {
    "id": "AAMkAGUAAAwTW11CCC=",
    "subject": "Onboarding welcome",
    "from": {"emailAddress": {"name": "HR Team", "address": "hr@company.com"}},
    "receivedDateTime": "2026-06-10T09:00:00Z",
    "sentDateTime": "2026-06-10T08:59:00Z",
    "hasAttachments": False,
    "isRead": True,
    "isDraft": False,
    "importance": "normal",
    "bodyPreview": "Welcome to the team! Your onboarding starts Monday.",
    "webLink": "https://outlook.office365.com/owa/?ItemID=AAMkAGUAAAwTW11CCC%3D",
    "conversationId": "AAQkAGW=",
    "parentFolderId": "inbox_folder_id",
}

_PAGE_1_BODY = {
    "@odata.context": f"{GRAPH_BASE}/$metadata#users('bb8775a4')/messages",
    "value": [_MSG_1, _MSG_2],
    "@odata.nextLink": ME_MESSAGES_P2,
}

_PAGE_2_BODY = {
    "@odata.context": f"{GRAPH_BASE}/$metadata#users('bb8775a4')/messages",
    "value": [_MSG_3],
    # No @odata.nextLink → pagination stops
}

_FOLDERS_BODY = {
    "@odata.context": f"{GRAPH_BASE}/$metadata#users('bb8775a4')/mailFolders",
    "value": [
        {
            "id": "inbox_folder_id",
            "displayName": "Inbox",
            "totalItemCount": 42,
            "unreadItemCount": 5,
            "isHidden": False,
            "parentFolderId": "root_folder_id",
        },
        {
            "id": "sent_folder_id",
            "displayName": "Sent Items",
            "totalItemCount": 120,
            "unreadItemCount": 0,
            "isHidden": False,
            "parentFolderId": "root_folder_id",
        },
    ],
    # No @odata.nextLink
}

_SEARCH_BODY = {
    "@odata.context": f"{GRAPH_BASE}/$metadata#users('bb8775a4')/messages",
    "value": [_MSG_3],
}


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class MsOutlookMailManifest(unittest.TestCase):
    """The YAML manifest loads correctly and maps every declared field."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_MSOUTLOOKMAIL")
        os.environ["RC_CONN_MSOUTLOOKMAIL"] = "EwA_test_bearer_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSOUTLOOKMAIL", None)
        else:
            os.environ["RC_CONN_MSOUTLOOKMAIL"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        """load_manifests() discovers the msoutlookmail connector and maps all runtime fields."""
        m = api.load_manifests()
        self.assertIn("msoutlookmail", m)
        mani = m["msoutlookmail"]
        self.assertEqual(mani.base_url, "https://graph.microsoft.com/v1.0")
        self.assertEqual(mani.auth.strategy, "bearer")
        self.assertEqual(mani.pagination.style, "none")
        self.assertEqual(mani.pagination.items_field, "value")
        self.assertEqual(mani.pagination.page_size, 50)
        self.assertEqual(mani.rate_limit_remaining_header, "")

    def test_connector_module_manifest_matches_yaml(self):
        """The connector's registered MANIFEST matches what the YAML loader would produce."""
        api.load_manifests()
        yaml_mani = api.MANIFESTS["msoutlookmail"]
        # The connector's register() call wins over YAML (register() discards the YAML-loaded key).
        # Both must agree on the runtime-critical fields.
        self.assertEqual(msoutlookmail.MANIFEST.base_url, yaml_mani.base_url)
        self.assertEqual(msoutlookmail.MANIFEST.auth.strategy, yaml_mani.auth.strategy)
        self.assertEqual(msoutlookmail.MANIFEST.pagination.style, yaml_mani.pagination.style)


class MsOutlookMailPagination(unittest.TestCase):
    """The _collect_odata() loop follows @odata.nextLink (absolute URL) across pages."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_MSOUTLOOKMAIL")
        os.environ["RC_CONN_MSOUTLOOKMAIL"] = "EwA_test_bearer_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSOUTLOOKMAIL", None)
        else:
            os.environ["RC_CONN_MSOUTLOOKMAIL"] = self._saved

    @responses.activate
    def test_collect_odata_stitches_two_pages(self):
        """@odata.nextLink on page 1 causes a follow to page 2; page 2 has no nextLink → stops."""
        # Page 1: returns two messages + nextLink pointing to page 2
        responses.add(responses.GET, ME_MESSAGES, json=_PAGE_1_BODY, status=200)
        # Page 2: absolute URL; returns one message, no nextLink
        responses.add(responses.GET, ME_MESSAGES_P2, json=_PAGE_2_BODY, status=200)

        items = msoutlookmail._collect_odata("me/messages", query={"$top": 25})

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["id"], "AAMkAGUAAAwTW09AAA=")
        self.assertEqual(items[1]["id"], "AAMkAGUAAAwTW10BBB=")
        self.assertEqual(items[2]["id"], "AAMkAGUAAAwTW11CCC=")
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_bearer_credential_on_both_pages(self):
        """Bearer token rides both the initial GET and the nextLink follow request."""
        responses.add(responses.GET, ME_MESSAGES, json=_PAGE_1_BODY, status=200)
        responses.add(responses.GET, ME_MESSAGES_P2, json=_PAGE_2_BODY, status=200)

        msoutlookmail._collect_odata("me/messages", query={"$top": 25})

        # Both calls must carry the injected bearer token.
        for call in responses.calls:
            self.assertEqual(
                call.request.headers.get("Authorization"),
                "Bearer EwA_test_bearer_token",
            )

    @responses.activate
    def test_max_items_caps_result(self):
        """max_items truncates results even when the server would return more via nextLink."""
        responses.add(responses.GET, ME_MESSAGES, json=_PAGE_1_BODY, status=200)
        # Page 2 would be fetched only if max_items allows more than 2 items.
        responses.add(responses.GET, ME_MESSAGES_P2, json=_PAGE_2_BODY, status=200)

        items = msoutlookmail._collect_odata("me/messages", query={"$top": 25}, max_items=2)

        self.assertEqual(len(items), 2)
        # Page 2 should NOT be fetched (max_items satisfied after page 1).
        self.assertEqual(len(responses.calls), 1)


class MsOutlookMailPickSelection(unittest.TestCase):
    """Field pre-selection: list_messages returns only the support-relevant subset."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_MSOUTLOOKMAIL")
        os.environ["RC_CONN_MSOUTLOOKMAIL"] = "EwA_test_bearer_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSOUTLOOKMAIL", None)
        else:
            os.environ["RC_CONN_MSOUTLOOKMAIL"] = self._saved

    @responses.activate
    def test_list_messages_returns_picked_fields(self):
        """list_messages() applies api.pick so only support-relevant fields reach the caller."""
        responses.add(responses.GET, ME_MESSAGES, json={"value": [_MSG_1, _MSG_2]}, status=200)

        msgs = msoutlookmail.list_messages()

        self.assertEqual(len(msgs), 2)
        m = msgs[0]
        # Support fields must be present.
        self.assertEqual(m["subject"], "You have late tasks!")
        self.assertEqual(m["from.emailAddress.address"], "noreply@Planner.Office365.com")
        self.assertEqual(m["from.emailAddress.name"], "Microsoft Planner")
        self.assertIn("receivedDateTime", m)
        self.assertIn("hasAttachments", m)
        self.assertIn("isRead", m)
        self.assertIn("bodyPreview", m)
        self.assertIn("webLink", m)

        # Raw noise (toRecipients raw object, @odata.etag, etc.) must NOT be present.
        self.assertNotIn("toRecipients", m)
        self.assertNotIn("@odata.etag", m)

    @responses.activate
    def test_high_importance_message_fields(self):
        """High-importance, unread message with attachment fields are correctly picked."""
        responses.add(responses.GET, ME_MESSAGES, json={"value": [_MSG_2]}, status=200)

        msgs = msoutlookmail.list_messages()
        m = msgs[0]

        self.assertEqual(m["importance"], "high")
        self.assertFalse(m["isRead"])
        self.assertTrue(m["hasAttachments"])

    @responses.activate
    def test_list_messages_folder_path(self):
        """Specifying a folder routes the request to mailFolders/{folder}/messages."""
        responses.add(responses.GET, ME_INBOX, json={"value": [_MSG_1]}, status=200)

        msgs = msoutlookmail.list_messages(folder="inbox")

        self.assertEqual(len(msgs), 1)
        self.assertTrue(responses.calls[0].request.url.startswith(ME_INBOX))

    @responses.activate
    def test_search_messages(self):
        """search_messages() issues $search and returns picked fields."""
        responses.add(responses.GET, ME_MESSAGES, json=_SEARCH_BODY, status=200)

        msgs = msoutlookmail.search_messages("onboarding")

        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["subject"], "Onboarding welcome")
        # Verify $search param was sent.
        url = responses.calls[0].request.url
        self.assertIn("%24search", url)  # URL-encoded $search

    @responses.activate
    def test_list_folders(self):
        """list_folders() returns picked folder fields."""
        responses.add(responses.GET, ME_FOLDERS, json=_FOLDERS_BODY, status=200)

        fldrs = msoutlookmail.list_folders()

        self.assertEqual(len(fldrs), 2)
        inbox = fldrs[0]
        self.assertEqual(inbox["displayName"], "Inbox")
        self.assertEqual(inbox["totalItemCount"], 42)
        self.assertEqual(inbox["unreadItemCount"], 5)

    @responses.activate
    def test_api_pick_on_collected_items(self):
        """api.pick selects dotted paths from a full message object as used in pick pre-selection."""
        picked = api.pick(
            _MSG_2,
            "subject,from.emailAddress.address,hasAttachments,importance,bodyPreview",
        )
        self.assertEqual(picked["subject"], "Invoice #1042 is due")
        self.assertEqual(picked["from.emailAddress.address"], "billing@vendor.com")
        self.assertTrue(picked["hasAttachments"])
        self.assertEqual(picked["importance"], "high")


class MsOutlookMailMarkdown(unittest.TestCase):
    """Markdown renderers produce the expected concise output."""

    def test_messages_to_markdown(self):
        """messages_to_markdown renders subject, sender, received, preview."""
        picked = [api.pick(m, msoutlookmail._MSG_PICK) for m in [_MSG_1, _MSG_2]]
        md = msoutlookmail.messages_to_markdown(picked)

        self.assertIn("You have late tasks!", md)
        self.assertIn("Invoice #1042 is due", md)
        self.assertIn("Microsoft Planner", md)
        self.assertIn("[UNREAD]", md)     # MSG_2 isRead=False
        self.assertIn("[HIGH]", md)       # MSG_2 importance=high
        self.assertIn("2026-06-15 10:30", md)

    def test_messages_to_markdown_empty(self):
        self.assertIn("(no messages)", msoutlookmail.messages_to_markdown([]))

    def test_folders_to_markdown(self):
        """folders_to_markdown renders display name, counts, and id."""
        fldrs = [
            {"displayName": "Inbox", "totalItemCount": 42, "unreadItemCount": 5,
             "isHidden": False, "id": "inbox_folder_id"},
        ]
        md = msoutlookmail.folders_to_markdown(fldrs)
        self.assertIn("Inbox", md)
        self.assertIn("total=42", md)
        self.assertIn("unread=5", md)

    def test_folders_to_markdown_empty(self):
        self.assertIn("(none found)", msoutlookmail.folders_to_markdown([]))


class MsOutlookMailCLI(unittest.TestCase):
    """CLI drives the connector end-to-end via the main() entry point."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_MSOUTLOOKMAIL")
        os.environ["RC_CONN_MSOUTLOOKMAIL"] = "EwA_test_bearer_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSOUTLOOKMAIL", None)
        else:
            os.environ["RC_CONN_MSOUTLOOKMAIL"] = self._saved

    @responses.activate
    def test_cli_messages_command(self):
        """CLI `messages` sub-command fetches and renders markdown."""
        responses.add(responses.GET, ME_MESSAGES, json={"value": [_MSG_1]}, status=200)

        import io
        import sys
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            rc = msoutlookmail.main(["messages", "--top", "10"])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("Outlook Messages", output)
        self.assertIn("You have late tasks!", output)
        # Bearer must have ridden the request.
        self.assertEqual(
            responses.calls[0].request.headers["Authorization"],
            "Bearer EwA_test_bearer_token",
        )

    @responses.activate
    def test_cli_search_command(self):
        """CLI `search` sub-command routes to $search and renders markdown."""
        responses.add(responses.GET, ME_MESSAGES, json=_SEARCH_BODY, status=200)

        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            rc = msoutlookmail.main(["search", "onboarding"])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("Search: onboarding", output)
        self.assertIn("Onboarding welcome", output)

    @responses.activate
    def test_cli_folders_command(self):
        """CLI `folders` sub-command fetches and renders folder list."""
        responses.add(responses.GET, ME_FOLDERS, json=_FOLDERS_BODY, status=200)

        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            rc = msoutlookmail.main(["folders"])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("Inbox", output)
        self.assertIn("Sent Items", output)

    @responses.activate
    def test_cli_messages_with_user(self):
        """CLI `messages --user UPN` routes to /users/{UPN}/messages."""
        user_msgs_url = f"{GRAPH_BASE}/users/alex@example.com/messages"
        responses.add(responses.GET, user_msgs_url, json={"value": [_MSG_1]}, status=200)

        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            rc = msoutlookmail.main(["--user", "alex@example.com", "messages"])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        self.assertTrue(responses.calls[0].request.url.startswith(user_msgs_url))

    @responses.activate
    def test_cli_generic_api_get_drives_connector(self):
        """The generic `python -m lib.api get msoutlookmail` CLI works for single-page reads."""
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/mailFolders",
            json=_FOLDERS_BODY,
            status=200,
        )

        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            rc = api._main([
                "get", "msoutlookmail", "me/mailFolders",
                "--pick", "value.*.displayName",
            ])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("Inbox", output)


class MsOutlookMailCassetteHygiene(unittest.TestCase):
    """CI guard: no real Outlook/Graph token prefix may land in committed connector files.

    Scopes to the connector dir (manifest + __init__.py + __main__.py), NOT this test file —
    the test legitimately names the prefixes it hunts for, so scanning itself would be a
    false positive.
    """

    # Microsoft identity platform token prefixes. Split with string concatenation so this guard
    # doesn't flag itself. Common prefixes: "EwA" (Exchange/Graph delegated), "ey" (JWT header —
    # access tokens are JWTs). We check only the unambiguous multi-char prefixes.
    _TOKEN_PREFIXES = ("EwA" "o", "MSAL" "Bearer")  # realistic examples only; test fixtures use "EwA_test"

    def test_no_real_token_prefixes_in_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "msoutlookmail"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
