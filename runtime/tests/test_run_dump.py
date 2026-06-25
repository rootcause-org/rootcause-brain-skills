"""Pure-logic tests for the shared run-dump renderer. No DB/API: a synthetic bundle in, the index
markdown + JSONL lines out. Run with the rest of the runtime suite:

    cd runtime && uv run --no-project python -m unittest discover -s tests
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib.run_dump import decorate, emit_jsonl, files_read, flags, render_index  # noqa: E402


def _bundle(**run_over):
    """A representative bundle: one grounding pre-step (submit_selection) + a main loop that reads a
    brain file, runs a db query, hits a failing step, and replies. ISO-string timestamps (the /full
    API shape)."""
    run = {
        "run_id": "abcd1234-5678-90ab-cdef-1234567890ab",
        "project": "momentum-tools",
        "status": "ok",
        "kind": "prompt",
        "trigger": None,
        "brain_ref": None,
        "error": None,
        "thread_id": "thr_1",
        "session_id": "sess_1",
        "topic": "Open invoices",
        "question": "Do I still have open invoices?",
        "warm_start_digest": None,
        "grounding_seed": None,
        "system_prompt": "MODE: prompt\nYou are an autonomous agent with exactly two tools. "
                         "Always reply. Never end your turn without it.\nCAPABILITIES: db, stripe.",
        "created_at": "2026-06-21T12:00:00+00:00",
        "finished_at": "2026-06-21T12:00:42+00:00",
        "model": "claude-x",
        "run_cost_usd": 0.1234,
        "run_total_tokens": 5000,
        "draft": "Hello,\n\nYou have one open invoice for €42.\n\nBest,\nSupport",
        "notes": [{"key": "internal", "body": "Customer is on the pro plan."}],
        "metadata": {"model": "claude-x", "run_url": "https://example/runs/abcd1234"},
        "egress": [
            {"host": "api.stripe.com", "port": 443, "scheme": "https", "url": "https://api.stripe.com/v1",
             "bytes_out": 10, "decision": "allow", "at": "2026-06-21T12:00:10+00:00"},
        ],
    }
    run.update(run_over)
    events = [
        {"seq": -2, "tool": "bash", "args": {"command": "cd /brain && rg invoice /brain/skills/billing/SKILL.md"},
         "command": "cd /brain && rg invoice /brain/skills/billing/SKILL.md", "stdout": "12: open invoice\n", "stderr": "",
         "exit_code": 0, "status": "ok", "duration_ms": 120, "at": "2026-06-21T12:00:01+00:00",
         "reasoning": "Look for invoice docs.", "cost_usd": 0.001, "total_tokens": 100, "model": "claude-x"},
        {"seq": -1, "tool": "submit_selection",
         "args": {"selected": [{"path": "/brain/skills/billing/SKILL.md", "reason": "invoice flow"}],
                  "summary": "billing doc covers it"},
         "stdout": "", "stderr": "", "exit_code": 0, "status": "ok", "duration_ms": 5, "at": "2026-06-21T12:00:02+00:00",
         "reasoning": "", "cost_usd": 0.002, "total_tokens": 50, "model": "claude-x"},
        {"seq": 1, "tool": "bash",
         "args": {"command": "cd /brain && python -c 'from lib import db; print(db.query(\"select 1\"))'"},
         "command": "cd /brain && python -c 'from lib import db; print(db.query(\"select 1\"))'",
         "stdout": "[{'?column?': 1}]\n", "stderr": "", "exit_code": 0, "status": "ok", "duration_ms": 800,
         "at": "2026-06-21T12:00:20+00:00", "reasoning": "Query open invoices for the account.",
         "cost_usd": 0.05, "total_tokens": 2000, "model": "claude-x"},
        {"seq": 2, "tool": "bash", "args": {"command": "cd /brain && python boom.py"},
         "command": "cd /brain && python boom.py", "stdout": "", "stderr": "Traceback: KeyError 'x'\n",
         "exit_code": 1, "status": "error", "duration_ms": 90, "at": "2026-06-21T12:00:25+00:00",
         "reasoning": "Try the helper.", "cost_usd": 0.01, "total_tokens": 300, "model": "claude-x"},
        {"seq": 3, "tool": "reply", "args": {"draft": True, "journal": False},
         "stdout": "", "stderr": "", "exit_code": 0, "status": "ok", "duration_ms": 10,
         "at": "2026-06-21T12:00:40+00:00", "reasoning": "Send the answer.", "cost_usd": 0.06,
         "total_tokens": 2450, "model": "claude-x"},
    ]
    return {"run": run, "events": events}


class Decorate(unittest.TestCase):
    def test_disp_grounding_then_main(self):
        events = _bundle()["events"]
        decorate(events)
        self.assertEqual([e["disp"] for e in events], ["P1", "P2", "1", "2", "3"])
        self.assertEqual([e["grounding"] for e in events], [True, True, False, False, False])

    def test_labels(self):
        events = _bundle()["events"]
        decorate(events)
        labels = {e["disp"]: e["label"] for e in events}
        self.assertEqual(labels["P1"], "search files")
        self.assertEqual(labels["1"], "db query")
        self.assertEqual(labels["3"], "reply")

    def test_bash_command_from_top_level_when_args_empty(self):
        # The /full API shape: command at top level, args absent for bash.
        events = [{"seq": 1, "tool": "bash", "command": "ls -la", "args": {}, "exit_code": 0,
                   "status": "ok", "reasoning": ""}]
        decorate(events)
        self.assertEqual(events[0]["command"], "ls -la")
        self.assertEqual(events[0]["label"], "search files")

    def test_action_preflight_label(self):
        # An action preflight is a python run, but it gets the "check action" intent over "python".
        events = [{"seq": 1, "tool": "bash", "args": {},
                   "command": "cd /brain && python actions/create_appointment/preflight.py --params '{}'",
                   "exit_code": 0, "status": "ok", "reasoning": ""}]
        decorate(events)
        self.assertEqual(events[0]["label"], "check action")


class RenderIndex(unittest.TestCase):
    def test_sections_present(self):
        md = render_index(_bundle())
        for needle in ("# Run abcd1234 — momentum-tools · ok · prompt",
                       "## Question", "## Outcome", "**Draft** (", "## Grounding pre-step",
                       "## Timeline", "## Flags", "## Files the run read", "## Egress (by host)",
                       "## Drill down"):
            self.assertIn(needle, md)

    def test_duration_from_iso_timestamps(self):
        self.assertIn("· 42.0s", render_index(_bundle()))

    def test_failing_step_flagged(self):
        flag_lines = flags(_bundle())
        self.assertTrue(any("[2]" in f and "error" in f for f in flag_lines))

    def test_files_read(self):
        events = _bundle()["events"]
        decorate(events)
        self.assertIn("/brain/skills/billing/SKILL.md", files_read(events))

    def test_system_prompt_trimmed_in_index(self):
        md = render_index(_bundle())
        self.assertIn("standing systemPromptBody", md)  # the static body collapsed to a marker

    def test_prior_context_rendered_when_present(self):
        md = render_index(_bundle(
            prior_messages=[
                {"sender": "buyer@example.com", "sent_at": "2026-06-20T09:00:00+00:00",
                 "body": "First line\nSecond line", "is_inbound": True, "attachments": []},
                {"sender": "support@us.example", "sent_at": "2026-06-20T10:00:00+00:00",
                 "body": "Our earlier reply", "is_inbound": False, "attachments": []},
            ],
            prior_notes=[
                {"key": "triage", "body_markdown": "**md note**", "body_html": "<b>html</b>",
                 "created_at": "2026-06-20T09:30:00+00:00"},
                {"key": "fallback", "body_markdown": "", "body_html": "html only",
                 "created_at": "2026-06-20T09:45:00+00:00"},
            ],
        ))
        self.assertIn("## Prior context given to the brain", md)
        self.assertIn("### Conversation so far", md)
        self.assertIn("**inbound** · buyer@example.com · 2026-06-20T09:00:00+00:00", md)
        self.assertIn("**outbound** · support@us.example", md)
        self.assertIn("First line\nSecond line", md)
        self.assertIn("### Earlier internal notes (never sent)", md)
        self.assertIn("**md note**", md)        # body_markdown preferred
        self.assertIn("html only", md)          # body_html fallback when markdown empty
        # oldest-first as stored: first inbound message precedes our outbound reply
        self.assertLess(md.index("First line"), md.index("Our earlier reply"))

    def test_prior_context_omitted_when_absent(self):
        md = render_index(_bundle())  # no prior_messages / prior_notes — pre-change run
        self.assertNotIn("## Prior context given to the brain", md)

    def test_brain_ref_echoed_for_test_run(self):
        md = render_index(_bundle(brain_ref="dev/refund-rework", trigger="test"))
        self.assertIn("Test run", md)
        self.assertIn("dev/refund-rework", md)

    def test_projection_inputs_rendered_when_present(self):
        settings = {
            "source": "cli",
            "synced_at": "2026-06-22T00:00:00Z",
            "version": "sha256:abc",
            "settings": {
                "header_short_name": "De Kies",
                "newpatient_method": "waitlist",
                "existingpatient_method": "book_via_link",
                "reschedule_method": "propose_options_confirm",
                "booking_hygienist_dentist_interaction": "Tandarts",
            },
        }
        md = render_index(_bundle(
            brain_resolved="channel:stable @ abc1234",
            tenant="de-kies",
            tenant_settings=json.dumps(settings),
        ))
        self.assertIn("## Projection inputs", md)
        self.assertIn("**Brain resolved:** `channel:stable @ abc1234`", md)
        self.assertIn("**Tenant:** `de-kies`", md)
        self.assertIn("source `cli`", md)
        self.assertIn("synced_at `2026-06-22T00:00:00Z`", md)
        self.assertIn("version `sha256:abc`", md)
        self.assertIn("newpatient_method=`waitlist`", md)
        self.assertIn("existingpatient_method=`book_via_link`", md)
        self.assertIn("reschedule_method=`propose_options_confirm`", md)
        self.assertIn("booking_hygienist_dentist_interaction=`Tandarts`", md)
        self.assertNotIn("header_short_name", md)  # index stays concise; full snapshot is in JSONL

    def test_no_callback(self):
        md = render_index(_bundle(draft=None, notes=[], metadata=None))
        self.assertIn("no stored callback", md)

    def test_proposed_actions_render_even_when_proposals_note_empty(self):
        action = {
            "slug": "create_appointment",
            "status": "Awaiting approval",
            "params": {
                "patient_id": 482889,
                "start_time": "2026-07-08 11:25",
                "duration": 35,
                "agenda_id": 301,
                "subdivision_id": 333,
                "type_id": 404,
            },
        }
        md = render_index(_bundle(
            notes=[{"key": "proposals", "body": ""}],
            proposed_actions=[action],
        ))
        self.assertIn("**Note** `proposals`:", md)
        self.assertIn("_(empty)_", md)
        self.assertIn("**Proposed actions** (1):", md)
        self.assertIn("`create_appointment`", md)
        self.assertIn("Awaiting approval", md)
        self.assertIn("`patient_id=482889`", md)
        self.assertIn("`start_time=2026-07-08 11:25`", md)
        self.assertIn("`duration=35`", md)
        self.assertIn("`agenda_id=301`", md)
        self.assertIn("`subdivision_id=333`", md)
        self.assertIn("`type_id=404`", md)

    def test_blocked_egress_timestamp_normalized(self):
        # Byte-identity guard: a blocked-egress flag must print `at` via _as_dt, so an ISO-string `at`
        # (API path) renders the same as a datetime `at` (operator path) — no stray 'T'.
        egress = [{"host": "evil.example", "port": 443, "scheme": "https", "url": "https://evil",
                   "bytes_out": 0, "decision": "block", "at": "2026-06-21T12:00:10+00:00"}]
        flag_lines = flags(_bundle(egress=egress))
        blocked = [f for f in flag_lines if "egress BLOCKED" in f]
        self.assertTrue(blocked)
        self.assertIn("at 2026-06-21 12:00:10+00:00", blocked[0])  # space, not 'T'


class EmitJsonl(unittest.TestCase):
    def test_header_then_events(self):
        lines = list(emit_jsonl(_bundle()))
        header = json.loads(lines[0])
        self.assertEqual(header["type"], "run")
        self.assertEqual(header["run_id"], "abcd1234-5678-90ab-cdef-1234567890ab")
        self.assertEqual(header["draft"].splitlines()[0], "Hello,")
        self.assertEqual(header["run_total_tokens"], 5000)  # int, not 5000.0
        events = [json.loads(x) for x in lines[1:]]
        self.assertEqual([e["type"] for e in events], ["event"] * 5)
        self.assertEqual([e["disp"] for e in events], ["P1", "P2", "1", "2", "3"])

    def test_brain_ref_in_header(self):
        lines = list(emit_jsonl(_bundle(brain_ref="dev/x", trigger="test")))
        header = json.loads(lines[0])
        self.assertEqual(header["brain_ref"], "dev/x")
        self.assertEqual(header["trigger"], "test")

    def test_projection_inputs_in_header(self):
        settings = {"source": "cli", "settings": {"header_short_name": "De Kies"}}
        lines = list(emit_jsonl(_bundle(
            brain_ref="dev/x",
            brain_resolved="dev/x @ abc1234",
            tenant="de-kies",
            tenant_settings=json.dumps(settings),
        )))
        header = json.loads(lines[0])
        self.assertEqual(header["brain_ref"], "dev/x")
        self.assertEqual(header["brain_resolved"], "dev/x @ abc1234")
        self.assertEqual(header["tenant"], "de-kies")
        self.assertEqual(json.loads(header["tenant_settings"]), settings)

    def test_proposed_actions_in_header(self):
        action = {"slug": "create_appointment", "status": "Awaiting approval", "params": {"patient_id": 482889}}
        lines = list(emit_jsonl(_bundle(proposed_actions=[action])))
        header = json.loads(lines[0])
        self.assertEqual(header["proposed_actions"], [action])

    def test_non_bash_carries_args(self):
        lines = list(emit_jsonl(_bundle()))
        reply = next(json.loads(x) for x in lines[1:] if json.loads(x)["tool"] == "reply")
        self.assertIn("args", reply)
        self.assertEqual(reply["args"], {"draft": True, "journal": False})

    def test_datetimes_pass_through_as_iso(self):
        from datetime import datetime, timezone
        # operator path may hand datetimes; they serialize to ISO, same shape as the string path.
        b = _bundle(created_at=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc))
        header = json.loads(list(emit_jsonl(b))[0])
        self.assertEqual(header["created_at"], "2026-06-21T12:00:00+00:00")


class ByteIdentity(unittest.TestCase):
    """The headline DRY guarantee (spec acceptance #3 / server-spec #4): the renderer output is
    byte-identical whether fed the API-shape bundle (`fetch_via_api` here: ISO-string timestamps,
    float costs) or the operator-shape bundle (`fetch_via_db` in rc_agent_debug.py: datetime objects,
    Decimal costs/tokens). Same run → same bytes, because BOTH go through this ONE renderer."""

    @staticmethod
    def _operator_shape(bundle: dict) -> dict:
        """Re-cast an API-shape bundle the way the operator's SSM/DB fetch hands it over: datetimes for
        every timestamp, Decimal for the money/token columns psycopg returns as Decimal."""
        from copy import deepcopy
        from datetime import datetime
        from decimal import Decimal

        b = deepcopy(bundle)

        def dt(v):
            return datetime.fromisoformat(v) if isinstance(v, str) else v

        run = b["run"]
        run["created_at"] = dt(run["created_at"])
        run["finished_at"] = dt(run["finished_at"])
        if run.get("run_cost_usd") is not None:
            run["run_cost_usd"] = Decimal(str(run["run_cost_usd"]))
        if run.get("run_total_tokens") is not None:
            run["run_total_tokens"] = Decimal(str(run["run_total_tokens"]))
        for g in run.get("egress") or []:
            g["at"] = dt(g["at"])
        for e in b["events"]:
            e["at"] = dt(e["at"])
            if e.get("cost_usd") is not None:
                e["cost_usd"] = Decimal(str(e["cost_usd"]))
        return b

    def _assert_identical(self, api_bundle: dict):
        from copy import deepcopy
        op_bundle = self._operator_shape(api_bundle)
        # deepcopy each call: render_index/emit_jsonl mutate events in place (decorate), and the two
        # bundles must not share state.
        api_md = render_index(deepcopy(api_bundle))
        op_md = render_index(deepcopy(op_bundle))
        self.assertEqual(api_md, op_md, "index .md differs between API and operator bundle shapes")
        api_jl = "\n".join(emit_jsonl(deepcopy(api_bundle)))
        op_jl = "\n".join(emit_jsonl(deepcopy(op_bundle)))
        self.assertEqual(api_jl, op_jl, "JSONL differs between API and operator bundle shapes")

    def test_plain_run(self):
        self._assert_identical(_bundle())

    def test_test_run_with_brain_ref(self):
        self._assert_identical(_bundle(brain_ref="dev/refund-rework", trigger="test"))

    def test_cost_accounting_gap_decimal_ledger(self):
        # Operator path: ledger cost is a psycopg Decimal, metadata cost a JSON float, >2% apart — the
        # cost-accounting-gap flag must fire (not raise float−Decimal TypeError) and read identically to
        # the all-float API shape.
        from decimal import Decimal
        api = _bundle(run_cost_usd=1.00, metadata={"total_cost_usd": 1.10})
        op = _bundle(run_cost_usd=Decimal("1.00"), metadata={"total_cost_usd": 1.10})
        af, of = flags(api), flags(op)
        self.assertTrue(any("cost accounting gap" in f for f in af))
        self.assertEqual(af, of)  # Decimal ledger renders identically to float ledger

    def test_with_blocked_egress(self):
        egress = [
            {"host": "api.stripe.com", "port": 443, "scheme": "https", "url": "https://api.stripe.com",
             "bytes_out": 10, "decision": "allow", "at": "2026-06-21T12:00:10+00:00"},
            {"host": "evil.example", "port": 443, "scheme": "https", "url": "https://evil",
             "bytes_out": 0, "decision": "block", "at": "2026-06-21T12:00:11+00:00"},
        ]
        self._assert_identical(_bundle(egress=egress))


if __name__ == "__main__":
    unittest.main()
