---
description: Trigger a REAL prod run of this brain from a question, wait for the answer, then show the brain commit it wrote.
argument-hint: "\"<question>\" [--brain-ref dev/<branch>]"
---

Use the **observability** skill. Trigger a real production run of the current brain and verify it end
to end. Everything is over the project's `rc login` OAuth token (no SSM/operator access); `rc`
auto-targets the brain you're in.

`$ARGUMENTS` is the customer-style **question** (quoted), optionally followed by `--brain-ref
dev/<branch>` to test a pushed dev branch without moving `main`. If empty, ask for one and stop.

## What to do

1. **Trigger and wait** — `rc ask` submits and waits for the answer:
   ```bash
   rc ask "<question>"                          # against main HEAD
   rc ask "<question>" --brain-ref dev/<branch>  # test a pushed dev branch; main stays live, run flagged `test`
   ```
   Relay the **answer**, any **note/caveats**, the **run accounting** (status · turns · cost · outcome),
   and the **trace URL** (surface it — it shows every step). It prints the **`run_id`**; capture it. On
   `status: error`, surface the error and stop.

2. **Show what the run wrote to the brain:**
   ```bash
   rc run <run_id> --brain-diff
   ```
   Prints the journal commit (SHA · message · files · diff). If nothing changed, say so — the agent
   answered without persisting anything durable.

**When to use:** the fastest "did my brain change actually work on real prod infra?" check — use it
after a brain edit (push a `dev/*` branch first to keep `main` live). For the full reasoning/tool-trail
of the run, follow up with [`/rc-inspect <run_id>`](rc-inspect.md).
