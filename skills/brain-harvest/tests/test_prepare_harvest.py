from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import random
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_harvest.py"
SPEC = importlib.util.spec_from_file_location("prepare_harvest", SCRIPT)
assert SPEC and SPEC.loader
ph = importlib.util.module_from_spec(SPEC)
sys.modules["prepare_harvest"] = ph
SPEC.loader.exec_module(ph)


V2_CORPUS = """---
harvest_format: v2
harvested_at: 2026-01-02T03:04:05Z
accepted_interactions: 8
unique_content: 8
terminal_condition: accepted_target
---

## Warranty question about widget — #1

**Occurrences:** 3

**external (2025-06-01):**
Hello, I have a question about the warranty on the widget that we bought from you.

**mailbox (2025-06-02):**
Thanks for reaching out. The warranty covers two years and we will gladly assist you with the paperwork.
_[attachment: manual.pdf]_

## Warranty question about widget 12 — #2

**external (2022-03-01):**
Hello, I have a longer question about the warranty on the widget that we bought from your webshop last month. The unit stopped working after two weeks and I would like to know which steps we should take now to have this looked at and what information you need from us to proceed.

**mailbox (2022-03-02):**
Thanks for the details. We will arrange an inspection and you will receive the instructions from us shortly.

## Re: Warranty question about widget 99 — #3

**external (2025-07-01):**
Hello, this is another warranty question about the widget for you.

**mailbox (2025-07-02):**
Thanks, we have received your warranty question and we will answer it for you tomorrow.

## Order — #4

**external (2024-02-01):**
Hello, where is my order? I would like to know when it will arrive at our address.

**mailbox (2024-02-02):**
Ok

## Contact — #5

**external (2026-01-01):**
Submitted through the contact form on the website. Please call me back about this.

## (no subject) — #6

**external (2023-05-05):**
Hello, just checking in with you about this and that.

## Order — #123 problem — #7

**external (2021-04-04):**
Hello, there is a problem with ticket #55 for this order and we would like an update from you.

**mailbox (2021-04-05):**
Thanks for the message. We looked into the problem and we will send you the update this week.

## Refund request for damaged widget — #8

**external (2010-05-01):**
Hello, the widget arrived damaged and I want a refund or a chargeback for this order please.

**mailbox (2010-05-02):**
Sorry to hear that. We will process the refund for you right away and you will see it on your statement.
"""


V1_CORPUS = """---
harvest_format: v1
mailbox: owner@example.com
harvested_at: 2026-01-02T03:04:05Z
threads: 2
cleaned: true
truncated: false
---

## Delivery issue — #1
**Participants:** anna@customer.example, owner@example.com
**Span:** 2019-03-04 → 2019-03-10

**anna@customer.example (2019-03-04):**
Hello, my delivery has not arrived and I would like to know where it is at the moment please.

**Owner@Example.com (2019-03-05):**
We checked with the courier and your parcel will arrive tomorrow, sorry for the delay and thanks for waiting.
_[attachment: label.pdf]_

## (no subject) — #2
**anna@customer.example (2020-01-01):**
Just one line for you.
"""


def write_corpus(root: Path, text: str, name: str = "corpus.md") -> Path:
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


def prepare(tmp: Path, text: str, **cfg_overrides) -> Path:
    corpus = write_corpus(tmp, text)
    scratch = tmp / "scratch"
    cfg = dict(ph.DEFAULTS)
    cfg.update(cfg_overrides)
    ph.prepare_scratch(corpus, scratch, cfg)
    return scratch


def read_manifest(scratch: Path) -> list[dict]:
    lines = (scratch / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def read_clusters(scratch: Path) -> list[dict]:
    return json.loads((scratch / "clusters.json").read_text(encoding="utf-8"))["clusters"]


def read_ledger(scratch: Path) -> dict:
    return json.loads((scratch / "ledger.json").read_text(encoding="utf-8"))


class ParseTests(unittest.TestCase):
    def test_v1_roles_dates_attachments_participants_span(self):
        meta, body = ph.parse_front_matter(V1_CORPUS)
        self.assertEqual(meta["harvest_format"], "v1")
        sections = ph.split_sections(body)
        self.assertEqual(len(sections), 2)
        thread = ph.parse_section(sections[0], "v1", meta["mailbox"], 0)
        self.assertEqual(thread.subject, "Delivery issue")
        self.assertEqual(thread.participants, ["anna@customer.example", "owner@example.com"])
        self.assertEqual(thread.span, ["2019-03-04", "2019-03-10"])
        self.assertEqual([(m.role, m.date) for m in thread.messages],
                         [("external", "2019-03-04"), ("mailbox", "2019-03-05")])
        self.assertEqual(thread.messages[1].attachments, ["label.pdf"],
                         "case-insensitive mailbox match must classify the owner reply")
        self.assertNotIn("_[attachment", thread.messages[1].body)

    def test_v2_roles_occurrences_and_attachment(self):
        meta, body = ph.parse_front_matter(V2_CORPUS)
        sections = ph.split_sections(body)
        self.assertEqual(len(sections), 8)
        thread = ph.parse_section(sections[0], "v2", "", 0)
        self.assertEqual(thread.occurrences, 3)
        self.assertEqual([(m.role, m.date) for m in thread.messages],
                         [("external", "2025-06-01"), ("mailbox", "2025-06-02")])
        self.assertEqual(thread.messages[1].attachments, ["manual.pdf"])

    def test_subject_with_em_dash_hash_and_plain_hash(self):
        meta, body = ph.parse_front_matter(V2_CORPUS)
        sections = ph.split_sections(body)
        tricky = ph.parse_section(sections[6], "v2", "", 6)
        self.assertEqual(tricky.subject, "Order — #123 problem")
        self.assertEqual(tricky.occurrence_index, 7)
        self.assertIn("#55", tricky.messages[0].body)

    def test_bom_crlf_front_matter(self):
        raw = "﻿" + V2_CORPUS.replace("\n", "\r\n")
        meta, body = ph.parse_front_matter(raw)
        self.assertEqual(meta["harvest_format"], "v2")
        self.assertEqual(len(ph.split_sections(body)), 8)

    def test_unknown_format_v3_fails_with_recovery_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = write_corpus(Path(tmp), V2_CORPUS.replace("harvest_format: v2",
                                                               "harvest_format: v3"))
            with self.assertRaisesRegex(ph.HarvestError, r"v3.*rc project corpus download") as ctx:
                ph.load_threads([corpus])
            self.assertIn("48h", str(ctx.exception))

    def test_missing_front_matter_fails_with_recovery_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = write_corpus(Path(tmp), "## Hello — #1\n\n**external (2024-01-01):**\nhi\n")
            with self.assertRaisesRegex(ph.HarvestError, "front-matter.*rc project corpus download"):
                ph.load_threads([corpus])

    def test_empty_corpus_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            front_only = "---\nharvest_format: v2\nharvested_at: 2026-01-02T03:04:05Z\n---\n\n"
            corpus = write_corpus(Path(tmp), front_only)
            with self.assertRaisesRegex(ph.HarvestError, "no '## "):
                ph.load_threads([corpus])


class PrepareTests(unittest.TestCase):
    def test_deterministic_byte_identical_reruns(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = prepare(Path(a), V2_CORPUS, holdout_count=1)
            second = prepare(Path(b), V2_CORPUS, holdout_count=1)
            for name in ("manifest.jsonl", "clusters.json", "ledger.json", "holdout.json"):
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)
            self.assertEqual(sorted(p.name for p in (first / "threads").iterdir()),
                             sorted(p.name for p in (second / "threads").iterdir()))

    def test_idempotent_rerun_replaces_stale_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=1)
            original = (scratch / "manifest.jsonl").read_bytes()
            (scratch / "threads" / "H999999.md").write_text("stale", encoding="utf-8")
            (scratch / "manifest.jsonl").write_text("corrupted", encoding="utf-8")
            cfg = dict(ph.DEFAULTS, holdout_count=1)
            ph.prepare_scratch(Path(tmp) / "corpus.md", scratch, cfg)
            self.assertFalse((scratch / "threads" / "H999999.md").exists())
            self.assertEqual((scratch / "manifest.jsonl").read_bytes(), original)

    def test_ids_are_opaque_and_ordered_by_first_date_then_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=1)
            rows = read_manifest(scratch)
            self.assertEqual([r["id"] for r in rows], [f"H{i:06d}" for i in range(1, 9)])
            self.assertEqual(rows[0]["date_first"], "2010-05-01")
            self.assertEqual([list(r)[0] for r in rows], ["id"] * 8, "first key must be id")
            ordering = [(r["date_first"] or "", r["section_index"]) for r in rows]
            self.assertEqual(ordering, sorted(ordering))
            for path in (scratch / "threads").iterdir():
                self.assertRegex(path.name, r"^H\d{6}\.md$")

    def test_mixed_bucket_and_generic_subjects_never_form_topic_clusters(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=0)
            clusters = {c["id"]: c for c in read_clusters(scratch)}
            self.assertIn("mixed", clusters)
            labels = {c["label"] for c in clusters.values()}
            self.assertIn("warranty-question-about-widget", labels)
            for generic in ("order", "contact", ""):
                self.assertNotIn(generic, labels)
            rows = {r["subject_family"]: r for r in read_manifest(scratch)}
            self.assertEqual(rows["order"]["cluster"], "mixed")
            self.assertEqual(rows["contact"]["cluster"], "mixed")
            self.assertEqual(rows[""]["cluster"], "mixed", "(no subject) must land in mixed")
            self.assertEqual(rows["order-problem"]["cluster"], "mixed",
                             "below-min-size families fall back to mixed")

    def test_prose_reply_flag_counts_and_form_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = read_manifest(prepare(Path(tmp), V2_CORPUS, holdout_count=0))
            by_family = {(r["subject_family"], r["date_first"]): r for r in rows}
            warranty = by_family[("warranty-question-about-widget", "2025-06-01")]
            self.assertTrue(warranty["prose_reply"])
            self.assertEqual(warranty["prose_reply_count"], 1)
            self.assertEqual(warranty["occurrences"], 3)
            self.assertTrue(warranty["attachments"])
            self.assertEqual(warranty["direction"], "external_first")
            order = by_family[("order", "2024-02-01")]
            self.assertFalse(order["prose_reply"], "a 2-char mailbox reply is not prose")
            self.assertEqual(order["prose_reply_count"], 0)
            self.assertEqual(order["mailbox_message_count"], 1)
            contact = by_family[("contact", "2026-01-01")]
            self.assertTrue(contact["form_source"])
            self.assertFalse(contact["prose_reply"])

    def test_v1_end_to_end_prepare(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V1_CORPUS, holdout_count=0)
            rows = read_manifest(scratch)
            self.assertEqual([r["source_format"] for r in rows], ["v1", "v1"])
            delivery = next(r for r in rows if r["subject_family"] == "delivery-issue")
            self.assertEqual(delivery["date_first"], "2019-03-04")
            self.assertEqual(delivery["date_last"], "2019-03-05")
            self.assertTrue(delivery["prose_reply"])
            self.assertTrue(delivery["attachments"])
            self.assertEqual(delivery["mailbox_message_count"], 1)
            self.assertEqual(read_ledger(scratch)["corpus"]["format"], "v1")
            self.assertEqual(ph.verify_scratch(scratch), [])

    def test_era_band_boundaries(self):
        cfg = dict(ph.DEFAULTS)
        harvested = date(2026, 1, 2)
        self.assertEqual(ph.era_band("2024-01-02", harvested, cfg), "recent")
        self.assertEqual(ph.era_band("2023-12-02", harvested, cfg), "mid")
        self.assertEqual(ph.era_band("2020-01-02", harvested, cfg), "mid")
        self.assertEqual(ph.era_band("2019-12-02", harvested, cfg), "old")
        self.assertEqual(ph.era_band(None, harvested, cfg), "old")
        self.assertEqual(ph.era_band("2024-01-02", None, cfg), "old")

    def test_risk_markers_deep_read_and_over_cap_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=0)
            rows = {r["date_first"]: r for r in read_manifest(scratch)}
            self.assertIn("payment_dispute", rows["2010-05-01"]["risk_markers"])
            self.assertIn("refund", rows["2010-05-01"]["risk_markers"])
            ledger = read_ledger(scratch)
            self.assertFalse(ledger["risk"]["over_cap"], "1/8 flagged is under the 15% cap")
            self.assertEqual(ledger["risk"]["flagged"], 1)
            self.assertAlmostEqual(ledger["risk"]["share"], 0.125)
            self.assertGreaterEqual(ledger["risk"]["by_marker"]["payment_dispute"], 1)
            risky_id = rows["2010-05-01"]["id"]
            cluster = next(c for c in read_clusters(scratch) if risky_id in c["thread_ids"])
            self.assertIn(risky_id, cluster["deep_read_ids"])
            self.assertNotIn(risky_id, cluster["sample_ids"])

    def test_risk_over_cap_is_reported_but_prepare_still_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=0, risk_cap=0.1)
            ledger = read_ledger(scratch)
            self.assertTrue(ledger["risk"]["over_cap"])
            self.assertEqual(ledger["risk"]["by_marker"],
                             {"payment_dispute": 1, "refund": 1})
            # report-only: the ledger is intact and reading plans stayed capped
            self.assertEqual(ph.verify_scratch(scratch), [])
            for cluster in read_clusters(scratch):
                self.assertLessEqual(len(cluster["sample_ids"]), ph.DEFAULTS["sample_cap"])

    def test_config_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            corpus = write_corpus(tmp_path, V2_CORPUS)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"risk_cap": 0.9, "unknown_knob": 1}), encoding="utf-8")
            args = SimpleNamespace(corpus=str(corpus), scratch=str(tmp_path / "scratch"),
                                   config=str(config), holdout=1, seed=7)
            cfg = ph.load_config(args)
            self.assertEqual(cfg["risk_cap"], 0.9)
            self.assertEqual(cfg["holdout_count"], 1)
            self.assertEqual(cfg["seed"], 7)
            self.assertNotIn("unknown_knob", cfg)
            with mock.patch.object(ph, "check_safe_output"):
                self.assertEqual(ph.cmd_prepare(args), 0)
            self.assertFalse(read_ledger(Path(args.scratch))["risk"]["over_cap"])


class HoldoutAndLedgerTests(unittest.TestCase):
    def test_holdout_reserved_eligible_and_absent_from_all_reading_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=1)
            holdout = json.loads((scratch / "holdout.json").read_text(encoding="utf-8"))
            self.assertEqual(holdout["count"], 1)
            rows = {r["id"]: r for r in read_manifest(scratch)}
            for hid in holdout["ids"]:
                self.assertTrue(rows[hid]["holdout"])
                self.assertIsNone(rows[hid]["cluster"])
                self.assertTrue(rows[hid]["prose_reply"], "holdout needs a real human answer")
                for cluster in read_clusters(scratch):
                    for key in ("thread_ids", "sample_ids", "deep_read_ids"):
                        self.assertNotIn(hid, cluster[key], f"{key} of {cluster['id']}")
            ledger = read_ledger(scratch)
            self.assertEqual(ledger["holdout"]["ids"], holdout["ids"])
            self.assertEqual(ledger["threads"][holdout["ids"][0]]["status"], "holdout")
            # only the long-external prose-answered thread is eligible in this corpus
            self.assertEqual(rows[holdout["ids"][0]]["date_first"], "2022-03-01")

    def test_verify_catches_double_assignment_and_missing_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=0)
            self.assertEqual(ph.main(["verify", "--scratch", str(scratch)]), 0)
            clusters_doc = json.loads((scratch / "clusters.json").read_text(encoding="utf-8"))
            moved = clusters_doc["clusters"][0]["thread_ids"][0]
            mixed = next(c for c in clusters_doc["clusters"] if c["id"] == "mixed")
            mixed["thread_ids"].append(moved)
            mixed["size"] = len(mixed["thread_ids"])
            (scratch / "clusters.json").write_text(json.dumps(clusters_doc), encoding="utf-8")
            violations = "\n".join(ph.verify_scratch(scratch))
            self.assertIn("appears in 2 cluster lists", violations)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(ph.main(["verify", "--scratch", str(scratch)]), 1)
            self.assertIn("FAILED", stderr.getvalue())

            ledger = read_ledger(scratch)
            dropped = sorted(ledger["threads"])[-1]
            del ledger["threads"][dropped]
            (scratch / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
            violations = "\n".join(ph.verify_scratch(scratch))
            self.assertIn(f"{dropped}: in manifest but absent from ledger", violations)

    def test_ledger_apply_moves_route_elsewhere_and_records_read_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=0)
            cluster = next(c for c in read_clusters(scratch) if c["id"] == "C01")
            moved, sampled = cluster["thread_ids"][0], cluster["thread_ids"][1]
            report = {"cluster": "C01", "read_deep": [moved], "read_sampled": [sampled],
                      "route_elsewhere": [{"id": moved, "suggested_cluster": "mixed",
                                           "reason": "actually a generic inquiry"}],
                      "contradictions": [], "saturation": {"still_yielding": False, "note": ""},
                      "counts": {"assigned": len(cluster["thread_ids"]), "read": 2}}
            report_path = scratch / "drafts" / "C01.report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(ph.main(["ledger", "apply", "--scratch", str(scratch),
                                      str(report_path)]), 0)
            ledger = read_ledger(scratch)
            self.assertEqual(ledger["threads"][moved]["cluster"], "mixed")
            self.assertEqual(ledger["threads"][moved]["routed_to"], "mixed")
            self.assertEqual(ledger["threads"][moved]["read"], "deep")
            self.assertEqual(ledger["threads"][sampled]["read"], "sampled")
            self.assertIsNone(ledger["threads"][sampled]["routed_to"])
            clusters = {c["id"]: c for c in read_clusters(scratch)}
            self.assertNotIn(moved, clusters["C01"]["thread_ids"])
            self.assertIn(moved, clusters["mixed"]["thread_ids"])
            self.assertEqual(clusters["C01"]["size"], len(clusters["C01"]["thread_ids"]))
            self.assertEqual(ph.verify_scratch(scratch), [])

    def test_ledger_apply_never_touches_holdout_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=1)
            holdout_id = read_ledger(scratch)["holdout"]["ids"][0]
            report = {"cluster": "C01", "read_deep": [holdout_id], "read_sampled": [holdout_id],
                      "route_elsewhere": [{"id": holdout_id, "suggested_cluster": "mixed",
                                           "reason": "should be ignored"}]}
            report_path = scratch / "drafts" / "C01.report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(ph.main(["ledger", "apply", "--scratch", str(scratch),
                                      str(report_path)]), 0)
            record = read_ledger(scratch)["threads"][holdout_id]
            self.assertEqual(record["status"], "holdout")
            self.assertEqual(record["read"], "none", "holdouts are reserved for the evaluation")
            self.assertIsNone(record["routed_to"])
            self.assertEqual(ph.verify_scratch(scratch), [])


class SafetyAndCLITests(unittest.TestCase):
    def test_cleanup_requires_yes_removes_root_and_verifies_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=0)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(ph.main(["cleanup", "--scratch", str(scratch)]), 2)
            self.assertIn("--yes", stderr.getvalue())
            self.assertTrue(scratch.exists())
            self.assertEqual(ph.main(["cleanup", "--scratch", str(scratch), "--yes"]), 0)
            self.assertFalse(scratch.exists())

    def test_cleanup_refuses_directories_that_are_not_scratch_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            precious = Path(tmp) / "precious"
            precious.mkdir()
            (precious / "notes.txt").write_text("keep me", encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(ph.main(["cleanup", "--scratch", str(precious), "--yes"]), 2)
            self.assertTrue((precious / "notes.txt").exists())

    def test_check_safe_output_refuses_stageable_scratch_root(self):
        def fake_git(cmd, **kwargs):
            if cmd[1] == "rev-parse":
                return SimpleNamespace(returncode=0, stdout="/repo\n")
            return SimpleNamespace(returncode=1)  # check-ignore: NOT ignored
        with self.assertRaisesRegex(ph.HarvestError, "refusing stageable scratch root"):
            ph.check_safe_output(Path("/repo/.rootcause/harvest/x"), git_runner=fake_git)

        def fake_git_ok(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout="/repo\n")
        ph.check_safe_output(Path("/repo/.rootcause/harvest/x"), git_runner=fake_git_ok)
        with self.assertRaisesRegex(ph.HarvestError, "inside the current git checkout"):
            ph.check_safe_output(Path("/elsewhere/scratch"), git_runner=fake_git_ok)
        def fake_git_norepo(cmd, **kwargs):
            return SimpleNamespace(returncode=128, stdout="")
        with self.assertRaisesRegex(ph.HarvestError, "git checkout"):
            ph.check_safe_output(Path("/repo/x"), git_runner=fake_git_norepo)

    def test_preflight_warns_without_rc_and_fails_on_bad_corpus_format(self):
        def fake_git(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout=str(kwargs.get("cwd", "/repo")) + "\n") \
                if cmd[1] == "rev-parse" else SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "scratch"
            (scratch / "corpus").mkdir(parents=True)
            write_corpus(scratch / "corpus", V2_CORPUS)
            def git(cmd, **kwargs):
                if cmd[1] == "rev-parse":
                    return SimpleNamespace(returncode=0, stdout=tmp + "\n")
                return SimpleNamespace(returncode=0)  # check-ignore: ignored
            args = SimpleNamespace(scratch=str(scratch))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = ph.cmd_preflight(args, rc_runner=lambda argv: None, git_runner=git)
            output = stdout.getvalue()
            self.assertEqual(code, 0, "missing rc degrades to WARN, not failure")
            self.assertEqual(output.count("rc not available"), 4)
            self.assertIn("formats ['v2']", output)

            write_corpus(scratch / "corpus", V2_CORPUS.replace("harvest_format: v2",
                                                               "harvest_format: v9"), "bad.md")
            calls = []
            def rc(argv):
                calls.append(argv)
                return (0, "ok\n", "")
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = ph.cmd_preflight(args, rc_runner=rc, git_runner=git)
            self.assertEqual(code, 1, "unsupported corpus format must FAIL preflight")
            self.assertEqual(calls, [
                ["auth", "status"],
                ["project", "mailbox", "ls"],
                ["project", "settings", "behavior", "get", "-o", "json"],
                ["project", "triage", "policy", "get", "-o", "json"],
            ])


TOPICS = (
    ("Warranty claim for widget", "en"),
    ("Delivery delay for shipment", "en"),
    ("Retourzending van bestelling", "nl"),
    ("Vraag over garantie", "nl"),
    ("Question sur la livraison", "fr"),
    ("Frage zur Lieferung", "de"),
    ("Product manual request", "en"),
    ("Wholesale pricing request", "en"),
)
QUESTION = {
    "en": "Hello, we have a question about this and we would like to hear from you what the next steps are for our situation.",
    "nl": "Beste, wij hebben hierover een vraag en wij zouden graag van u horen wat de volgende stappen zijn voor onze situatie.",
    "fr": "Bonjour, nous avons une question et nous voudrions savoir de vous quelles sont les étapes suivantes pour notre situation.",
    "de": "Hallo, wir haben eine Frage dazu und wir möchten von Ihnen wissen, welche die nächsten Schritte für unsere Situation sind.",
}
DETAIL = {
    "en": " We already looked at the page you sent us and we still cannot find the answer, so please explain it once more for us in detail.",
    "nl": " Wij hebben de pagina al bekeken die u ons stuurde en wij vinden het antwoord nog niet, dus graag nog een keer uitleg voor ons.",
    "fr": " Nous avons déjà regardé la page que vous nous avez envoyée et nous ne trouvons pas la réponse, merci de nous expliquer encore.",
    "de": " Wir haben die Seite schon angesehen, die Sie uns geschickt haben, und wir finden die Antwort nicht, bitte erklären Sie es uns noch einmal.",
}
REPLY = {
    "en": "Thanks for reaching out. We looked at this for you and we will send the details shortly, please let us know if anything is unclear.",
    "nl": "Dank voor uw bericht. Wij hebben dit voor u bekeken en wij sturen de details zo snel mogelijk, laat het ons weten met vragen.",
    "fr": "Merci pour votre message. Nous avons regardé cela pour vous et nous allons envoyer les détails, dites-nous si ce n'est pas clair.",
    "de": "Danke für Ihre Nachricht. Wir haben das für Sie geprüft und wir senden die Details in Kürze, melden Sie sich gerne bei Fragen.",
}


def generate_v2_corpus(n: int = 1000, seed: int = 20260719) -> str:
    """Seeded synthetic v2 corpus shaped like the real harvests: 2007-2026 span, ~18% automated
    notification threads without a prose reply, en/nl/fr/de, deep multi-reply threads, duplicate
    subject families, a few risk-marked threads."""
    rng = random.Random(seed)
    sections = []

    def message(role: str, when: date, body: str) -> str:
        return f"**{role} ({when.isoformat()}):**\n{body}\n"

    for i in range(1, n + 1):
        if i == 1:
            start = date(2007, 1, 15)
        elif i == 2:
            start = date(2026, 5, 1)
        else:
            year = rng.randint(2007, 2026)
            month = rng.randint(1, 5) if year == 2026 else rng.randint(1, 12)
            start = date(year, month, rng.randint(1, 28))
        kind = rng.random()
        parts = []
        if kind < 0.18:  # automated notification, never prose-answered
            subject = f"Your order confirmation #{rng.randint(1000, 99999)}"
            parts.append(message("external", start,
                                 "This is an automated notification about your order. Do not reply to this message."))
        elif kind < 0.21:  # risk-marked dispute with a prose reply
            subject = f"Chargeback dispute for order {rng.randint(100, 999)}"
            parts.append(message("external", start,
                                 "Hello, my bank opened a chargeback for this order and I want a refund from you now." + DETAIL["en"]))
            parts.append(message("mailbox", start + timedelta(days=1), REPLY["en"]))
        elif kind < 0.25:  # generic subject -> mixed bucket
            subject = rng.choice(("Contact", "Order", "Invoice", "Question", "Info"))
            lang = rng.choice(("en", "nl", "fr", "de"))
            parts.append(message("external", start, QUESTION[lang]))
            parts.append(message("mailbox", start + timedelta(days=1), REPLY[lang]))
        else:  # duplicate subject families, occasionally deep threads
            topic, lang = rng.choice(TOPICS)
            subject = f"{topic} {rng.randint(1, 999)}"
            depth = rng.choice([2] * 8 + [3] * 3 + [8])
            body = QUESTION[lang] + (DETAIL[lang] if rng.random() < 0.4 else "")
            parts.append(message("external", start, body))
            for j in range(1, depth):
                role = "mailbox" if j % 2 else "external"
                text = REPLY[lang] if role == "mailbox" else QUESTION[lang]
                parts.append(message(role, start + timedelta(days=j), text))
        occurrences = rng.choice([1] * 8 + [2, 3])
        sections.append(f"## {subject} — #{i}\n\n**Occurrences:** {occurrences}\n\n" + "\n".join(parts))

    front = ("---\nharvest_format: v2\nharvested_at: 2026-06-01T00:00:00Z\n"
             f"accepted_interactions: {n}\nunique_content: {n}\nterminal_condition: accepted_target\n---\n\n")
    return front + "\n".join(sections)


class LargeCorpusTests(unittest.TestCase):
    scratch: Path

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        corpus = write_corpus(tmp, generate_v2_corpus())
        cls.scratch = tmp / "scratch"
        started = time.monotonic()
        ph.prepare_scratch(corpus, cls.scratch, dict(ph.DEFAULTS))
        cls.elapsed = time.monotonic() - started

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_runs_in_seconds_with_full_coverage_ledger(self):
        self.assertLess(self.elapsed, 30, "1,000-thread prepare must run in seconds")
        rows = read_manifest(self.scratch)
        self.assertEqual(len(rows), 1000)
        self.assertEqual(ph.verify_scratch(self.scratch), [])
        ledger = read_ledger(self.scratch)
        self.assertEqual(len(ledger["threads"]), 1000)
        statuses = {r["status"] for r in ledger["threads"].values()}
        self.assertLessEqual(statuses, set(ph.PRIMARY_STATUSES))
        self.assertEqual(ledger["corpus"]["date_span"][0][:4], "2007")
        self.assertEqual(ledger["corpus"]["date_span"][1][:4], "2026")
        self.assertEqual(len((list((self.scratch / "threads").iterdir()))), 1000)

    def test_automated_share_languages_and_families_look_like_real_harvests(self):
        rows = read_manifest(self.scratch)
        automated = [r for r in rows if r["form_source"]]
        self.assertAlmostEqual(len(automated) / 1000, 0.18, delta=0.04)
        self.assertTrue(all(not r["prose_reply"] for r in automated))
        self.assertGreaterEqual({r["language"] for r in rows} & {"en", "nl", "fr", "de"},
                                {"en", "nl", "fr", "de"})
        self.assertGreater(sum(1 for r in rows if r["message_count"] >= 8), 10,
                           "several deep multi-reply threads expected")
        families = [r["subject_family"] for r in rows]
        self.assertGreater(max(map(families.count, set(families))), 50,
                           "duplicate subject families expected")
        self.assertEqual({r["era"] for r in rows}, {"recent", "mid", "old"})

    def test_sampling_capped_risk_deep_read_and_mixed_present(self):
        clusters = {c["id"]: c for c in read_clusters(self.scratch)}
        self.assertIn("mixed", clusters)
        self.assertGreater(clusters["mixed"]["size"], 30)
        rows = {r["id"]: r for r in read_manifest(self.scratch)}
        ledger = read_ledger(self.scratch)
        self.assertFalse(ledger["risk"]["over_cap"])
        self.assertGreater(ledger["risk"]["flagged"], 20)
        self.assertIn("payment_dispute", ledger["risk"]["by_marker"])
        for cluster in clusters.values():
            self.assertLessEqual(len(cluster["sample_ids"]), ph.DEFAULTS["sample_cap"])
            for tid in cluster["deep_read_ids"]:
                self.assertTrue(rows[tid]["risk_markers"])
            if cluster["size"] > ph.DEFAULTS["sample_cap"] * 2:
                eras = {rows[tid]["era"] for tid in cluster["sample_ids"]}
                self.assertGreater(len(eras), 1, f"{cluster['id']} sample must span era bands")

    def test_holdout_default_eight_stratified_and_excluded(self):
        ledger = read_ledger(self.scratch)
        holdout_ids = ledger["holdout"]["ids"]
        self.assertEqual(len(holdout_ids), 8)
        rows = {r["id"]: r for r in read_manifest(self.scratch)}
        eras = {rows[hid]["era"] for hid in holdout_ids}
        self.assertGreater(len(eras), 1, "holdout must be stratified across era bands")
        for hid in holdout_ids:
            self.assertTrue(rows[hid]["prose_reply"])
            self.assertEqual(ledger["threads"][hid]["status"], "holdout")
        plan_ids = set()
        for cluster in read_clusters(self.scratch):
            plan_ids.update(cluster["thread_ids"])
            plan_ids.update(cluster["sample_ids"])
            plan_ids.update(cluster["deep_read_ids"])
        self.assertFalse(plan_ids & set(holdout_ids))


if __name__ == "__main__":
    unittest.main()
