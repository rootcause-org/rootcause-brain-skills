---
name: rc-fleet
description: Review recent production runs with `rc fleet runs`, discover exact cross-run action history with `rc fleet actions`, then mine systemic failures with `rc fleet patterns`. Use inside a brain checkout for run health, action arguments/statuses/run links over a date window, worst-offender triage, recurring failures, or when no single run is known yet.
---

# rc-fleet - recent run digest

Use the `rc` CLI from inside the brain checkout. Scope comes from the logged-in OAuth token and brain
metadata. Use `--project` only with an all-projects token; `--tenant` follows the normal token
pin/mismatch rules. Do not use SSM or private operator mechanics.
If a RootCause MCP is installed, ignore it unless the user explicitly asks for MCP; this workflow uses
`rc`.

## Required Context

Read:

- [docs/mirrors.md](../../docs/mirrors.md) when failures involve mirrors or source freshness.
- [docs/side-effects.md](../../docs/side-effects.md) before interpreting action statuses.

## Workflow

1. Digest first:
   ```bash
   rc fleet runs --days 7
   rc fleet runs --format agent
   rc fleet runs --kind email --days 14
   ```
   Pass through supplied `--days <n>` and `--kind email|prompt|mcp|analysis`. Read the per-run flag
   table, aggregate rates, and worst offenders. `--format agent` is the token-lean shortlist.

2. Find actions across runs before drilling into individual traces:
   ```bash
   rc fleet actions --days 14 \
     --action create_appointment --action update_appointment
   rc fleet actions --days 14 --action create_appointment \
     --status succeeded --format agent
   rc fleet actions --days 14 --action create_appointment -o json |
     jq '.items[] | {id, run_id, action_id, status, params, run_url}'
   ```
   Repeated `--action` and `--status` filters are exact-match ORs. Human and agent output include exact
   grounded params and the full freshly tokenized run URL by default; JSON preserves complete raw
   rows. Results page automatically. If the client warns that its page cap was reached, narrow the
   days/actions/statuses rather than trusting the partial tail.

   This feed requires `console:action` plus operator/admin action-view authority because params may
   contain customer values. Minimize filters and do not commit raw output or share tokenized URLs.
   Open `run_url` as returned; it can be null for direct operator actions or when run-page signing is
   unavailable.

   Status is lifecycle evidence:
   - `proposed` means recorded, not executed.
   - `executing` is non-terminal.
   - `succeeded`, `failed`, and `canceled` are terminal execution outcomes.

   There is no action-detail command. For result/error/preflight context, use the row's `run_id` with
   `rc-debug` or open `run_url`; a null `run_id` means no originating run trace.

3. Interpret run flags:
   - `ERRxn`: bash failures.
   - `EGRxn`: blocked egress.
   - `$!`: cost spike.
   - `CTX.Nk`: context rot.
   - `GD`: grounding discarded.

4. Drill two to five flagged runs with the `rc-debug` skill. The worst-offenders section gives full
   UUIDs.

5. Confirm systemic failures:
   ```bash
   rc fleet patterns --days 14
   rc fleet patterns --days 30
   ```
   Each ranked cluster is a candidate brain fix: missing runbook, wrong query, or a domain to
   allowlist. Author from evidence, then verify with `brain-ask` and finish through `brain-publish`
   when files changed.

Use this as the entry point for periodic fleet review and for "something is off, but I do not have a
specific UUID yet."
