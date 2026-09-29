"""lib.outbound broker contract; no network."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import outbound  # noqa: E402


class OutboundTest(unittest.TestCase):
    @responses.activate
    def test_propose_carries_only_given_channels_and_no_send_field(self):
        responses.add(responses.POST, outbound.BASE_URL + "/propose", json={"batch": {"batch_id": "b1"}})
        outbound.propose([{"key": "p1", "phone": "0487123456"}], sms_body="Verhuisd")
        body = json.loads(responses.calls[0].request.body)
        self.assertEqual(body, {"recipients": [{"key": "p1", "phone": "0487123456"}], "sms_body": "Verhuisd"})
        self.assertNotIn("send", body)

    @responses.activate
    def test_cli_reads_files(self):
        responses.add(responses.POST, outbound.BASE_URL + "/propose", json={"batch": {"batch_id": "b1"}})
        with tempfile.TemporaryDirectory() as tmp:
            r = Path(tmp) / "r.json"
            r.write_text(json.dumps([{"key": "a", "email": "a@example.com"}]), encoding="utf-8")
            mail = Path(tmp) / "mail.md"
            mail.write_text("Beste,\n\nWe zijn verhuisd.\n", encoding="utf-8")
            code = outbound._main(["propose", "--recipients-file", str(r), "--email-subject", "Nieuw adres",
                                   "--email-body-file", str(mail), "--mailbox", "info"])
        self.assertEqual(code, 0)
        body = json.loads(responses.calls[0].request.body)
        self.assertEqual(body["email_subject"], "Nieuw adres")
        self.assertEqual(body["email_body_markdown"], "Beste,\n\nWe zijn verhuisd.")
        self.assertEqual(body["mailbox"], "info")
        self.assertNotIn("sms_body", body)

    @responses.activate
    def test_server_refusal_is_clear(self):
        responses.add(responses.POST, outbound.BASE_URL + "/propose", body="choose the sending mailbox (--mailbox): a, b", status=400)
        with self.assertRaises(outbound.OutboundError) as ctx:
            outbound.propose([{"key": "a", "email": "a@example.com"}], email_subject="s", email_body_markdown="b")
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn("--mailbox", str(ctx.exception))

    @responses.activate
    def test_absent_mount_is_clear(self):
        with self.assertRaises(outbound.OutboundUnavailable) as ctx:
            outbound.channels()
        self.assertEqual(str(ctx.exception), "Outbound messaging is not enabled for this run")

    @responses.activate
    def test_status_path(self):
        responses.add(responses.GET, outbound.BASE_URL + "/batches/b1", json={"batch_id": "b1", "status": "completed"})
        self.assertEqual(outbound.status("b1")["status"], "completed")


if __name__ == "__main__":
    unittest.main()
