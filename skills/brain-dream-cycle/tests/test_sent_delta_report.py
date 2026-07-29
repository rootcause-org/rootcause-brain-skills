from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sent_delta_report.py"
SPEC = importlib.util.spec_from_file_location("sent_delta_report", SCRIPT)
assert SPEC and SPEC.loader
sdr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sdr  # @dataclass resolves annotations through sys.modules
SPEC.loader.exec_module(sdr)


def text_of(html_fragment: str) -> str:
    return re.sub(r"<[^>]+>", "", html_fragment)


class SplitQuotesTest(unittest.TestCase):
    def test_dutch_attribution_and_quote_block_go_to_the_trailer(self):
        body = (
            "Uw afspraak is geannuleerd.\n\nVriendelijke groeten,\nTessa\n\n"
            "Op di 14 jul 2026 om 18:28 schreef Afifa Lefhal <\n"
            "afifa@example.be>:\n\n> Beste,\n> Graag annuleren.\n"
        )
        new, quoted = sdr.split_quotes(body)
        self.assertIn("Vriendelijke groeten", new)
        self.assertNotIn("schreef", new)
        self.assertTrue(quoted.startswith("Op di 14 jul 2026"))
        self.assertIn("> Graag annuleren.", quoted)

    def test_english_and_forward_markers(self):
        for marker in ("On Tue, 14 Jul 2026 at 18:28 Ann <a@b.com> wrote:",
                       "-----Original Message-----"):
            new, quoted = sdr.split_quotes(f"Hi there.\n\n{marker}\n> old\n")
            self.assertEqual(new, "Hi there.")
            self.assertTrue(quoted.startswith(marker))

    def test_body_without_quotes_is_untouched(self):
        new, quoted = sdr.split_quotes("Just a reply.\n\nRegards,\nTessa")
        self.assertEqual(new, "Just a reply.\n\nRegards,\nTessa")
        self.assertEqual(quoted, "")


class WordDiffTest(unittest.TestCase):
    def test_edit_inside_a_sentence_stays_word_level(self):
        row, kept, removed, added = sdr.word_diff(
            "We can schedule you on Tuesday 8 September at 11u20.",
            "We can schedule you on Monday 12 July at 11u20.",
        )
        self.assertGreater(kept, removed + added)  # a line diff would call this a full rewrite
        left = "".join(t for op, t in row.left if op == "del")
        right = "".join(t for op, t in row.right if op == "ins")
        self.assertIn("Tuesday", left)
        self.assertIn("Monday", right)
        self.assertNotIn("schedule", left + right)

    def test_adjacent_changed_words_are_one_span(self):
        row, _, _, _ = sdr.word_diff("Bedankt voor uw bericht.", "Dank u voor het bericht.")
        html = sdr.spans(row.inline)
        self.assertLessEqual(html.count("<del>"), 2)
        self.assertLessEqual(html.count("<ins>"), 2)

    def test_inline_view_interleaves_removal_before_replacement(self):
        row, _, _, _ = sdr.word_diff("Beste Liesbeth,", "Beste Klara,")
        self.assertEqual(text_of(sdr.spans(row.inline)), "Beste LiesbethKlara,")

    def test_trailing_whitespace_stays_outside_the_marker(self):
        row, _, _, _ = sdr.word_diff("een twee drie", "een drie")
        self.assertIn("<del>twee</del> ", sdr.spans(row.left))


class BuildDiffTest(unittest.TestCase):
    def test_rewritten_paragraph_stays_paired_and_new_one_does_not(self):
        diff = sdr.build_diff(
            "Beste Bono,\n\nIk heb uw afspraak verplaatst naar vrijdag.",
            "Beste Bono,\n\nIk heb uw afspraak verplaatst naar maandag.\n\nTot dan!",
        )
        kinds = [row.kind for row in diff.rows]
        self.assertEqual(kinds, ["equal", "changed", "insert"])
        self.assertGreater(diff.similarity, 0.75)

    def test_unrelated_bodies_are_reported_as_replaced(self):
        diff = sdr.build_diff("Het vroegste moment is dinsdag 8 september om 11u20.",
                              "Wij hebben u proberen te bellen maar krijgen voicemail.")
        self.assertEqual(diff.shape, "replaced")
        self.assertEqual([row.kind for row in diff.rows], ["delete", "insert"])

    def test_quoted_history_is_excluded_from_the_counts_but_kept(self):
        sent = "Dank u.\n\nOp di 14 jul 2026 om 18:28 schreef Ann <a@b.be>:\n\n> " + "woord " * 200
        diff = sdr.build_diff("Dank u.", sent)
        self.assertEqual(diff.shape, "identical")
        self.assertIn("woord", diff.quoted)

    def test_rewrapped_paragraph_is_not_reported_as_changed(self):
        diff = sdr.build_diff(
            "Bedankt voor je bericht. Ik begrijp dat je op 2 september langskomt.",
            "Bedankt voor je bericht. Ik begrijp dat je op 2 september\nlangskomt.",
        )
        self.assertEqual([row.kind for row in diff.rows], ["equal"])
        self.assertEqual(diff.shape, "identical")

    def test_identical_bodies_have_no_markers(self):
        diff = sdr.build_diff("Zelfde tekst.", "Zelfde tekst.")
        self.assertEqual(diff.shape, "identical")
        self.assertEqual(diff.similarity, 1.0)


class RenderTest(unittest.TestCase):
    def test_report_escapes_customer_html_and_keeps_drill_command(self):
        item = {
            "id": "d1",
            "sender": "Praktijk <info@example.be>",
            "sent_at": "2026-07-15T06:23:41Z",
            "related_run_id": "eda3691b-7122-4f56-a3e1-b80913f830ef",
            "thread_id": "t1",
            "session_id": "s1",
            "similarity": 0.21,
            "proposed_body": "Beste <script>alert(1)</script> Afifa,",
            "sent_body": "Beste Afifa,",
        }
        diff = sdr.build_diff(item["proposed_body"], item["sent_body"])
        page = sdr.render([item], [diff], {"d1": "brain over-personalised"}, "dentai / de-kies", "")
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("rc run debug eda3691b-7122-4f56-a3e1-b80913f830ef", page)
        self.assertIn("brain over-personalised", page)
        self.assertIn(".rootcause/", page)


if __name__ == "__main__":
    unittest.main()
