---
name: rc-fleet
description: Review a rootcause project's recent production run fleet using `rc fleet runs`, then mine systemic failure patterns with `rc fleet patterns`. Use inside a brain checkout for at-a-glance run health, failure-rate review, worst-offender triage, recurring bash/egress failures, or when no single run/thread is known yet.
---

# rc-fleet - recent run digest

Use the `rc` CLI from inside the brain checkout. Scope comes from the logged-in OAuth token and brain
metadata; no project argument, SSM, or operator access.
If a RootCause MCP is installed, ignore it unless the user explicitly asks for MCP; this workflow uses
`rc`.

## Required Context

Read [docs/mirrors.md](../../docs/mirrors.md) when failures involve mirrors or source freshness.

## Workflow

1. Digest first:
   ```bash
   rc fleet runs --days 7
   rc fleet runs --format agent
   rc fleet runs --kind email --days 14
   ```
   Pass through supplied `--days <n>` and `--kind email|prompt|mcp|analysis`. Read the per-run flag
   table, aggregate rates, and worst offenders. `--format agent` is the token-lean shortlist.

2. Interpret flags:
   - `ERRxn`: bash failures.
   - `EGRxn`: blocked egress.
   - `$!`: cost spike.
   - `CTX.Nk`: context rot.
   - `GD`: grounding discarded.

3. Drill two to five flagged runs with the `rc-debug` skill. The worst-offenders section gives full
   UUIDs.

4. Confirm systemic failures:
   ```bash
   rc fleet patterns --days 14
   rc fleet patterns --days 30
   ```
   Each ranked cluster is a candidate brain fix: missing runbook, wrong query, or a domain to
   allowlist. Author from evidence, then verify with `brain-ask` and finish through `brain-publish`
   when files changed.

Use this as the entry point for periodic fleet review and for "something is off, but I do not have a
specific UUID yet."
