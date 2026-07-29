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


class SignalTest(unittest.TestCase):
    def test_dropped_date_link_and_placeholder(self):
        diff = sdr.build_diff(
            "Uw afspraak staat op dinsdag 8 september om 11u20.\n\n"
            "Schrijf u in via https://forms.gle/abc\n\n[[STATUS — in te vullen]]",
            "Wij bellen u volgende week op.",
        )
        found = sdr.signals(diff)
        self.assertIn("date_dropped", found)
        self.assertIn("link_dropped", found)
        self.assertIn("placeholder_leak", found)

    def test_date_kept_by_the_human_is_not_a_signal(self):
        diff = sdr.build_diff("Kan dinsdag 8 september om 11u20?", "Het wordt maandag 12 juli 8u00.")
        self.assertNotIn("date_dropped", sdr.signals(diff))

    def test_confirmation_question_and_length_shift(self):
        diff = sdr.build_diff(
            "Kunt u kort bevestigen of dit past? " + "Nog een zin met veel woorden erin. " * 6,
            "Het is bevestigd.",
        )
        found = sdr.signals(diff)
        self.assertIn("confirm_dropped", found)
        self.assertIn("human_wrote_less", found)

    def test_every_emitted_signal_has_a_label(self):
        self.assertEqual(set(sdr.SIGNAL_LABELS), {
            "date_dropped", "link_dropped", "placeholder_leak", "confirm_dropped",
            "greeting_changed", "signoff_changed", "human_wrote_more", "human_wrote_less"})


class RenderTest(unittest.TestCase):
    item = {
        "id": "d1e2f3a4-0000-0000-0000-000000000000",
        "sender": "Praktijk <info@example.be>",
        "sent_at": "2026-07-15T06:23:41Z",
        "related_run_id": "eda3691b-7122-4f56-a3e1-b80913f830ef",
        "thread_id": "t1",
        "session_id": "s1",
        "similarity": 0.21,
        "proposed_body": "Beste <script>alert(1)</script> Afifa,\n\nKunt u bevestigen?",
        "sent_body": "Beste Afifa,\n\nHet is geregeld.",
    }

    def page(self, **kw):
        diff = sdr.build_diff(self.item["proposed_body"], self.item["sent_body"])
        notes = {self.item["id"]: "brain over-personalised"}
        return sdr.render([self.item], [diff], notes, "dentai / de-kies", ""), diff

    def test_html_escapes_customer_markup_and_keeps_the_conclusion(self):
        page, _ = self.page()
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("brain over-personalised", page)

    def test_html_drops_the_technical_labels_the_human_does_not_want(self):
        page, diff = self.page()
        for noise in ("thread t1", "session s1", "rc run debug", "server similarity", "word similarity"):
            self.assertNotIn(noise, page)
        # The one plain-language verdict stays, and the run stays reachable but muted.
        self.assertIn(f'class="tag shape">{diff.shape}<', page)
        self.assertIn("run <span class=\"mono\">eda3691b</span>", page)

    def test_html_links_the_run_when_the_payload_carries_a_url(self):
        item = dict(self.item, run_url="https://app.replypen.com/runs/eda3691b?t=tok")
        diff = sdr.build_diff(item["proposed_body"], item["sent_body"])
        page = sdr.render([item], [diff], {}, "dentai / de-kies", "")
        self.assertIn('href="https://app.replypen.com/runs/eda3691b?t=tok"', page)

    def test_markdown_carries_ids_signals_and_word_level_markers(self):
        _, diff = self.page()
        md = sdr.render_markdown([self.item], [diff], {self.item["id"]: "misread intent"},
                                 "dentai / de-kies", "top-level call", "out.html")
        self.assertIn("### d1e2f3a4", md)
        self.assertIn("run `eda3691b-7122-4f56-a3e1-b80913f830ef`", md)
        self.assertIn("confirm_dropped", md)
        self.assertIn("misread intent", md)
        self.assertIn("top-level call", md)
        self.assertIn("out.html", md)
        self.assertRegex(md, r"\[-.+?-\]")

    def test_markdown_omits_unchanged_paragraphs(self):
        diff = sdr.build_diff("Gelijk.\n\nBedankt voor uw bericht.", "Gelijk.\n\nHet is geregeld.")
        md = sdr.render_markdown([self.item], [diff], {}, "s", "", "out.html")
        self.assertIn("(1 unchanged paragraph omitted)", md)
        self.assertNotIn("Gelijk.", md)

    def test_markdown_keeps_whitespace_outside_the_markers(self):
        diff = sdr.build_diff("praktijk De Kies Alken", "praktijk te Alken")
        md = sdr.render_markdown([self.item], [diff], {}, "s", "", "out.html")
        self.assertNotIn("+}Alken", md)


if __name__ == "__main__":
    unittest.main()
