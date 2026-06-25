---
name: brain-ask
description: Ask a real production rootcause brain using `rc ask`, then verify the resulting run. Use inside a brain checkout when asked whether a brain change works on prod infra, when asked to simulate a customer support email, when asked for a direct raw investigation, or when asked to test a pushed `dev/*` brain ref without moving `main`. Captures the answer, run accounting, trace URL, and brain journal diff.
---

# brain-ask - ask a real prod brain

Use `rc ask` from inside the current brain checkout. The `rc` CLI auto-targets the project from the
brain metadata and uses the logged-in OAuth token; no SSM or operator access.
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

   If the draft/note mentions a state-changing operation (booked, moved, cancelled, refunded, sent,
   updated) or any action/preflight caveat, do a quick action sanity check before reporting:
   ```bash
   rc run <run_id> --events
   ```
   Distinguish "draft text said it happened" from the action lifecycle: preflight failed ⇒ no proposal
   and no mutation; proposed action ⇒ pending human confirm; succeeded/failed action ⇒ post-loop
   execution happened. Use `../brain-dev/action-run-triage.md` for the decision table.

4. Show what the run wrote to the brain:
   ```bash
   rc run <run_id> --brain-diff
   ```
   Report the journal commit SHA, message, changed files, and meaningful diff summary. If nothing
   changed, say the run answered without persisting durable knowledge.

For the full reasoning/tool trail, use the `rc-inspect` skill with the captured `run_id`.
