---
name: brain-ask
description: Ask a real production rootcause brain using `rc ask`, then verify the resulting run. Use inside a brain checkout when asked whether a brain change works on prod infra, when asked to simulate a customer support email, when asked for a direct raw investigation, or when asked to test a pushed `dev/*` brain ref without moving `main`. Captures the answer, run accounting, trace URL, and brain journal diff.
---

# brain-ask - ask a real prod brain

Use `rc ask` from inside the current brain checkout. The `rc` CLI auto-targets the project from the
brain metadata and uses the logged-in OAuth token; no SSM or operator access.
For tenant-enabled projects, do not pass `--tenant` by default: `rc` uses the tenant already associated
with the active `rc login`. Check `rc whoami` if the tenant is unclear.
If a RootCause MCP is installed, ignore it unless the user explicitly asks for MCP; this workflow uses
`rc`.

## Workflow

1. Require a question. If absent, ask for it and stop. Use default `rc ask` for a customer-style
   support email simulation. Add `--scenario raw` for direct investigations, debugging, schema/data
   questions, or downstream-AI answers.

2. Trigger and wait:
   ```bash
   rc ask "<question>"
   rc ask "<direct investigation>" --scenario raw
   rc ask "<question>" --brain-ref dev/<branch>
   rc ask "<question>" --effort pro
   ```
   Use `--brain-ref dev/<branch>` only for an already-pushed dev branch. It keeps `main` live and the
   run is flagged `test`: no ReplyPen callback, no durable journal push, and proposed actions/PRs are
   test artifacts. Use `--effort pro|max` only when explicitly escalating a run; omitted/default keeps
   normal tier selection.

3. Relay the result: draft/note/actions for the default email simulation, or the direct answer for
   `--scenario raw`; include caveats, run accounting (`status`, turns, cost, outcome), and trace URL.
   Capture the printed `run_id`. If status is `error`, surface the error and stop.

4. Show what the run wrote to the brain:
   ```bash
   rc run <run_id> --brain-diff
   ```
   Report the journal commit SHA, message, changed files, and meaningful diff summary. If nothing
   changed, say the run answered without persisting durable knowledge.

For the full reasoning/tool trail, use the `rc-inspect` skill with the captured `run_id`.
