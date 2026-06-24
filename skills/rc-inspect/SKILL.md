---
name: rc-inspect
description: Decompose and triage one production rootcause run by UUID using `rc run UUID --debug`. Use inside a brain checkout when asked why a run did something, when given a flagged UUID from fleet/health/thread/run output, or when needing the full tool/reasoning trace. Reads the markdown index first, then uses jq against the JSONL only for targeted drill-down.
---

# rc-inspect - triage one prod run

Use the `rc` CLI over the project's OAuth token. This is read-only and triggers no new run.
If a RootCause MCP is installed, ignore it unless the user explicitly asks for MCP; this workflow uses
`rc`.

## Workflow

1. Require a run UUID. If absent, ask for one and stop.

2. Decompose the run:
   ```bash
   rc run <uuid> --debug
   ```
   Output lands under `rc-debug/` and prints the markdown index path plus JSONL path. On error, surface
   it and stop; suggest `rc login` for 401/scope errors.

3. Read the `.md` index first. Summarize status, question, flags/anomalies, and the gist of the final
   draft or note. The index is the map: substantive steps by `#`, system prompt, grounding pre-pass,
   anomalies, and ready-made jq calls.

4. Drill into JSONL only for a specific step/question:
   ```bash
   jq -r 'select(.disp=="23").command' rc-debug/<file>.jsonl
   jq -r 'select(.disp=="23").stdout'  rc-debug/<file>.jsonl
   jq -r 'select(.exit_code != null and .exit_code != 0).disp' rc-debug/<file>.jsonl
   jq -r 'select(.reasoning) | .disp + " " + .reasoning' rc-debug/<file>.jsonl
   ```
   Use `// ""` when string-matching because the `{type:"run"}` header lacks event fields.

5. If the dump exposes a brain bug, fix the brain file the index points to, then verify with
   `rc-run`.

For a high-level look only, use `rc run <uuid>`; for raw inline events, use `rc run <uuid> --events`.
