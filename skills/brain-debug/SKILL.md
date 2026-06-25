---
name: brain-debug
description: Replay and inspect one production rootcause brain run from inside a brain checkout. Use when given a run UUID, asked to debug a prod run, asked to trigger a customer-style email simulation or direct raw investigation and inspect the resulting run, or asked for the full reasoning/tool trace. Dumps via the public API using `rc` plus the sibling `brain-dev` engine, then reads the markdown index first and drills into JSONL only as needed.
---

# brain-debug - dump and read one prod run

Use the sibling `brain-dev` skill to dump and read one production run of the current brain. This path
needs only `rc login`, the `rc` CLI, and the public API. It does not need SSM or operator access.
If a RootCause MCP is installed, ignore it unless the user explicitly asks for MCP; brain debugging uses
`rc` + the local dump renderer.

Resolve the engine relative to this skill:

```bash
SKILL=<the absolute directory of this SKILL.md>
BRAIN_DEV_SKILL="$(cd "$(dirname "$SKILL")/../brain-dev" && pwd)"
```

If the harness already provides the absolute path of `skills/brain-dev`, use that instead.

## Workflow

Default to an analysis-first stance. Be autonomous and thorough in finding the likely issue, but stop
before changing files: inspect the dump, relevant JSONL slices, and likely culprit brain files, then
propose the smallest fix that should move the needle. Do not edit files, commit, publish, or run a
verification rerun for a proposed fix until the user confirms the proposal.

1. Get a run id.
   - If the input looks like a UUID, use it directly.
   - If the input is a quoted question, trigger a run first. Default `rc ask` is an email simulation;
     use `--scenario raw` for direct investigations/debugging/schema/data questions:
     ```bash
     rc ask "<question>"
     rc ask "<direct investigation>" --scenario raw
     rc ask "<question>" --brain-ref dev/<branch>
     rc ask "<question>" --effort pro
     ```
     A `--brain-ref` run is side-effect-light: no callback or journal push; proposed actions are
     flagged `test`. Use it for verification against a pushed `dev/*` branch. Use `--effort pro|max`
     only for an explicit stronger-tier retry.
   - If no run id or question was provided, ask for one and stop.

2. Dump the run to gitignored `.rootcause/dump/<run8>-<project>.{md,jsonl}`:
   ```bash
   uv run "$BRAIN_DEV_SKILL/scripts/brain_dump.py" <run_id>
   ```
   It prints both paths plus a one-line summary. On `error:`, surface the error and stop.

3. Read the `.md` index first. Summarize status, scenario if shown, question, flags/anomalies, and the
   gist of the final draft/note or raw answer. The index is the map: timeline, system prompt,
   grounding pre-pass, anomalies, and a ready-made drill-down block.

4. Drill into the `.jsonl` only for a specific step or question. Do not read it top to bottom:
   ```bash
   jq -r 'select(.disp=="23").command' <file>.jsonl
   jq -r 'select(.disp=="23").stdout'  <file>.jsonl
   jq -r 'select(.exit_code != null and .exit_code != 0).disp' <file>.jsonl
   jq -r 'select(.command // "" | contains("invoice")).disp' <file>.jsonl
   jq -r 'select(.reasoning) | .disp + " " + .reasoning' <file>.jsonl
   ```
   Use `// ""` for string matching because the `{type:"run"}` header lacks event fields.

5. If the dump exposes a likely brain bug, diagnose the brain, not the run. Use the index's "Files the
   run read" list to inspect likely culprit files, plus focused `rg`/read-only checks as needed. Be
   willing to keep digging until you can name the likely root cause or the remaining uncertainty.

6. Before changing any brain file, report:
   - root cause or best hypothesis
   - evidence from the dump/files
   - proposed file changes, scoped to the smallest useful fix
   - verification plan

   Ask for confirmation and stop.

7. If the user confirms, implement the proposed fix, verify it, and commit. Do not leave publish as an
   implicit human follow-up. Ask explicitly:
   `Can I now publish them?` If approved, push/sync/re-run via
   `skills/brain-dev/ship-and-verify.md`.

This skill is read-only unless you pass a question, which intentionally triggers a new test run, or the
user explicitly confirms the proposed file changes.
