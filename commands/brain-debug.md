---
description: Replay ONE prod brain run locally — dump it to a concise markdown index + a jq-queryable JSONL, read the index, drill into the JSONL only when a step matters.
argument-hint: "<run_id>  |  \"<question>\" [--brain-ref dev/x]"
---

Use the **brain-dev** skill to dump and read one production run of the current brain. The engine is at
`${CLAUDE_PLUGIN_ROOT}/skills/brain-dev/scripts`; the public-API path needs only the project's
`ROOTCAUSE_API_KEY` + the `rc` CLI (no SSM, no operator access).

`$ARGUMENTS` is **either** a run UUID (dump it directly) **or** a quoted question (trigger a run first,
then dump it). If empty, ask for one and stop.

## What to do

1. **Get a run_id.**
   - Looks like a UUID → use it as-is.
   - A quoted question → trigger a run, then dump its id:
     ```bash
     rc ask "<question>"                          # against main HEAD
     rc ask "<question>" --brain-ref dev/<branch>  # test a pushed dev branch, main stays live
     ```
     `rc ask` prints the `run_id`. A `--brain-ref` run is side-effect-free (no callback/journal;
     proposed actions flagged `test`).

2. **Dump the run to two local files** (gitignored `out/brain-dump/<run8>-<proj>.{md,jsonl}`):
   ```bash
   uv run "${CLAUDE_PLUGIN_ROOT}/skills/brain-dev/scripts/brain_dump.py" <run_id>
   ```
   It prints the two paths + a one-line summary (`status`, `brain_ref`, tool-call count). On `error:`
   (bad id, stale API/`rc`, missing key), surface it and stop.

3. **Read the `.md` index first** and relay a tight summary: status, the question, the flags/anomalies,
   and the gist of the final draft/note. The index is the map — it carries the timeline of substantive
   steps (each with a `#`), the system prompt, the grounding pre-pass, auto-flagged anomalies, and a
   "Drill down" block of ready-made jq calls.

4. **Progressive disclosure — do NOT read the `.jsonl` top to bottom.** It holds the FULL untruncated
   reasoning/command/stdout/stderr/cost per step, keyed by `disp` (the index's `#`). Only `jq` into the
   step a question actually points at:
   ```bash
   jq -r 'select(.disp=="23").command' <file>.jsonl    # full code of step 23
   jq -r 'select(.disp=="23").stdout'  <file>.jsonl    # its output / traceback
   jq -r 'select(.exit_code != null and .exit_code != 0).disp' <file>.jsonl   # failed steps
   jq -r 'select(.command // "" | contains("invoice")).disp' <file>.jsonl     # steps touching X
   jq -r 'select(.reasoning) | .disp + " " + .reasoning' <file>.jsonl         # reasoning per step
   ```
   Mind the `// ""` when string-matching: the `{type:"run"}` header line lacks event fields, and its
   rollups are `run_`-prefixed (`run_cost_usd`, `run_total_tokens`) so event-numeric queries skip it.

5. **If the dump exposes a brain bug** (a grounding script misparsing data, a wrong `# verified` note,
   a playbook the run ignored or that steered it wrong) → fix the **brain**, not the run. The index's
   "Files the run read" list points at the culprit; edit there, then push + sync + re-run per the
   brain-dev ship loop (`skills/brain-dev/ship-and-verify.md`).

This is read-only and triggers no new run unless you passed a question in step 1.
