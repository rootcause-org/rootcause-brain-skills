"""lib.mail broker contract; no network."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import mail  # noqa: E402


class MailTest(unittest.TestCase):
    @responses.activate
    def test_search_is_exact_structured_request(self):
        responses.add(responses.POST, mail.BASE_URL + "/search", json={"threads": [{"ref": "support:t1"}]})
        self.assertEqual(mail.search("person@example.com", since_days=30, limit=7), [{"ref": "support:t1"}])
        self.assertEqual(json.loads(responses.calls[0].request.body), {"with": "person@example.com", "since_days": 30, "limit": 7})

    @responses.activate
    def test_reply_draft_has_no_send_field(self):
        responses.add(responses.POST, mail.BASE_URL + "/draft", json={"status": "created"})
        mail.draft(reply="support:t1", body_markdown="Hello", key="followup")
        body = json.loads(responses.calls[0].request.body)
        self.assertEqual(body["reply"], "support:t1")
        self.assertNotIn("send", body)
        self.assertNotIn("mode", body)

    @responses.activate
    def test_server_refusal_is_clear(self):
        responses.add(responses.GET, mail.BASE_URL + "/thread", body="thread ref mailbox is not eligible", status=404)
        with self.assertRaises(mail.MailError) as ctx:
            mail.thread("other:t1")
        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertIn("not eligible", str(ctx.exception))

    @responses.activate
    def test_absent_mount_is_clear(self):
        with self.assertRaises(mail.MailUnavailable) as ctx:
            mail.mailboxes()
        self.assertEqual(str(ctx.exception), "Mailbox access is not enabled for this run")

    @responses.activate
    def test_cli_reads_body_file(self):
        responses.add(responses.POST, mail.BASE_URL + "/draft", json={"status": "created"})
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("Hello there", encoding="utf-8")
            self.assertEqual(mail._main(["draft", "--to", "a@example.com", "--subject", "Hi", "--body-file", str(body)]), 0)
        self.assertEqual(json.loads(responses.calls[0].request.body)["body_markdown"], "Hello there")


if __name__ == "__main__":
    unittest.main()
