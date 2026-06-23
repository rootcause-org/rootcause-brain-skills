---
description: At-a-glance digest of this project's recent prod runs — flags, rates, worst offenders — then mine recurring failure patterns.
argument-hint: "[--days <n>] [--kind email|prompt|mcp|analysis]"
---

Use the **observability** skill. A fleet-level read of the current project's recent runs, over the `rc
login` OAuth token — no SSM/operator access. Scope is automatic (your token only sees your project).

`$ARGUMENTS` → pass through `--days` / `--kind` if present.

## What to do

1. **Digest first** — read only this, then narrow:
   ```bash
   rc fleet --days 7
   ```
   Per-run flag table → aggregate rates → worst offenders. `--format agent` gives a token-lean
   shortlist when you want to triage cheaply. Flags: `ERR×n` bash-fails · `EGR×n` blocked-egress · `$!`
   cost-spike · `CTX·Nk` context-rot · `GD` grounding-discarded.

2. **Drill 2–5 flagged runs** — the worst-offenders section gives full UUIDs:
   [`/rc-inspect <uuid>`](rc-inspect.md) (`rc run <uuid> --debug`).

3. **Confirm what's systemic** vs a one-off:
   ```bash
   rc patterns --days 14
   ```
   Ranked clusters of failing bash calls + blocked egress. Each is a candidate brain fix (missing
   runbook, wrong query, a domain to allowlist) — author it from the evidence, then re-verify with
   [`/rc-run`](rc-run.md).

**When to use:** the periodic "how are my runs doing overall?" review, and the entry point when you
don't yet have a specific run/thread in mind.
