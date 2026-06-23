---
description: Decompose ONE prod run by UUID into a concise markdown index + a jq-queryable JSONL, read the index, drill the JSONL only where it matters.
argument-hint: <run-uuid>
---

Use the **observability** skill to triage one production run of the current brain. Over the project's
`rc login` OAuth token — no SSM/operator access.

`$ARGUMENTS` is a **run UUID** (e.g. from `rc fleet` or a `rc ask`). If empty, ask for one and stop.

## What to do

1. **Decompose the run** to two local files (under `rc-debug/`, gitignored):
   ```bash
   rc run <uuid> --debug
   ```
   Prints the index path then the jsonl path. On error (bad UUID, not your project, not logged in),
   surface it and stop — re-auth with `rc login` on a 401/scope error.

2. **Read the `.md` index first** and relay a tight summary: status, the question, the flags/anomalies,
   the gist of the final draft/note. The index is the map — timeline of substantive steps (each with a
   `#`), the system prompt, the grounding pre-pass, auto-flagged anomalies, and a "Drill down" block of
   ready-made jq calls.

3. **Progressive disclosure — do NOT read the `.jsonl` top to bottom.** It holds the full untruncated
   reasoning/command/stdout/stderr/cost per step, keyed by `disp` (the index's `#`). jq only the step a
   question points at:
   ```bash
   jq -r 'select(.disp=="23").command' rc-debug/<file>.jsonl    # full code of step 23
   jq -r 'select(.disp=="23").stdout'  rc-debug/<file>.jsonl    # its output / traceback
   jq -r 'select(.exit_code != null and .exit_code != 0).disp' rc-debug/<file>.jsonl   # failed steps
   jq -r 'select(.reasoning) | .disp + " " + .reasoning' rc-debug/<file>.jsonl         # reasoning per step
   ```
   Mind the `// ""` when string-matching: the `{type:"run"}` header line lacks event fields.

4. **If the dump exposes a brain bug** (a grounding script misparsing data, a wrong note, a playbook
   the run ignored) → fix the **brain**, not the run. The index's "Files the run read" list points at
   the culprit; edit, push, and re-verify with [`/rc-run`](rc-run.md).

**When to use:** any "why did this run do X?" — it triggers no new run. For a quick high-level look
instead, `rc run <uuid>` (no flag); for the raw inline trace, `rc run <uuid> --events`.
