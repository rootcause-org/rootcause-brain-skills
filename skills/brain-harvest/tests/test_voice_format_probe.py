"""Extraction contract for voice_format_probe.py: font vote, nested-div signature, image reuse."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "voice_format_probe.py"
SPEC = importlib.util.spec_from_file_location("voice_format_probe", SCRIPT)
assert SPEC and SPEC.loader
vfp = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = vfp
SPEC.loader.exec_module(vfp)

SENT = (Path(__file__).resolve().parent / "fixtures" / "voice_format_sent.html").read_text("utf-8")
PLAIN = "<div dir=\"ltr\">Bedankt!<br clear=\"all\"></div>"


class ExtractionTest(unittest.TestCase):
    def test_signature_innerhtml_stops_at_the_matching_close(self):
        sig = vfp.signature_html(SENT)
        self.assertIn("Sportieve groeten", sig)
        self.assertIn("mail-sig", sig)
        self.assertNotIn("gmail_signature", sig)
        self.assertNotIn("De inschrijving", sig)

    def test_proposal_from_a_consistent_mailbox(self):
        p = vfp.propose([SENT] * 5)
        self.assertEqual(p["draft_font_css"], "font-family:verdana,sans-serif;font-size:small")
        self.assertEqual(p["signature_share"], 1.0)
        self.assertEqual(p["image_urls"], ["https://ci3.googleusercontent.com/mail-sig/AIorK4xEXAMPLElogo"])
        self.assertIn("Sportieve groeten", p["signature_text"])
        self.assertIn("[image]", p["signature_text"])

    def test_minority_font_and_signature_yield_empty_proposals(self):
        p = vfp.propose([SENT] + [PLAIN] * 4)
        self.assertEqual(p["draft_font_css"], "")
        self.assertEqual(p["signature_html"], "")
        self.assertEqual(p["signature_share"], 0.2)

    def test_whitespace_variants_of_one_signature_are_the_same_vote(self):
        spaced = SENT.replace("><", ">\n   <")
        p = vfp.propose([SENT, spaced, SENT])
        self.assertEqual(p["signature_variants"], 1)

    def test_checks_flag_an_oversized_signature(self):
        p = vfp.propose([SENT] * 3)
        p["signature_bytes"] = vfp.MAX_SIGNATURE_BYTES + 1
        vfp.run_checks(p, network=False)
        self.assertIn(("size", "fail"), [(c["check"], c["status"]) for c in p["checks"]])
        self.assertTrue(all(c["status"] == "skipped" for c in p["checks"] if c["check"] == "image"))


if __name__ == "__main__":
    unittest.main()
