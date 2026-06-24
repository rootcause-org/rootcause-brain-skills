---
name: rc-run
description: Trigger and verify a real production rootcause run from a customer-style question using the `rc` CLI. Use inside a brain checkout when asked whether a brain change works on prod infra, when asked to run a prompt against `main`, or when asked to test a pushed `dev/*` brain ref without moving `main`. Captures the answer, run accounting, trace URL, and brain journal diff.
---

# rc-run - trigger a real prod run

Use `rc ask` from inside the current brain checkout. The `rc` CLI auto-targets the project from the
brain metadata and uses the logged-in OAuth token; no SSM or operator access.

## Workflow

1. Require a customer-style question. If absent, ask for it and stop.

2. Trigger and wait:
   ```bash
   rc ask "<question>"
   rc ask "<question>" --brain-ref dev/<branch>
   ```
   Use `--brain-ref dev/<branch>` only for an already-pushed dev branch. It keeps `main` live and the
   run is flagged `test`.

3. Relay the answer, note/caveats, run accounting (`status`, turns, cost, outcome), and trace URL.
   Capture the printed `run_id`. If status is `error`, surface the error and stop.

4. Show what the run wrote to the brain:
   ```bash
   rc run <run_id> --brain-diff
   ```
   Report the journal commit SHA, message, changed files, and meaningful diff summary. If nothing
   changed, say the run answered without persisting durable knowledge.

For the full reasoning/tool trail, use the `rc-inspect` skill with the captured `run_id`.
