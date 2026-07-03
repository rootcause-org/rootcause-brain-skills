---
name: prod-console
description: "Direct guarded production access from a brain checkout through `rc capabilities`, `rc db`, `rc bash`, and `rc action`. Use when a developer or their coding agent already knows the exact production primitive to run: SQL/schema lookup, cataloged brain script discovery, log command planning, or preflight-first action execution. No rootcause-side LLM; the caller reasons locally and rootcause supplies scoped, audited hands."
---

# prod-console - direct production primitives

Use this skill from inside a brain checkout when the task is targeted ("run this query", "inspect this
schema", "show available actions", "preflight this action"). If the question is vague and RootCause
should investigate, use `brain-ask` instead.

The console is public `rc` only. Do not use private RootCause repos, host shells, SSM, registry SQL, or
customer credentials. Scope comes from `.rootcause.toml`, the active OAuth login, and optional
`--project` / `--tenant`.

For debugging, tool parity, and "does this script/query work?" checks, prefer `rc db` and `rc bash`.
They are the fast production primitives. `rc ask` wraps those primitives in an LLM run and should be
reserved for full-loop behavior validation, ambiguous investigations, or customer-style simulations.

## Required Context

Read:

- [docs/side-effects.md](../../docs/side-effects.md)
- [docs/brain-model.md](../../docs/brain-model.md)

## Workflow

1. Discover first:
   ```bash
   rc capabilities
   ```
   Treat it as the manifest: database short names/descriptions, cataloged scripts, available actions,
   and which console planes are live.
   If a pushed brain script is missing or `/brain` looks stale, run:
   ```bash
   rc brain status
   rc brain sync
   rc bash list
   ```

2. For database work, fetch only what you need:
   ```bash
   rc db list
   rc db schema <db>
   rc db schema <db> --table <table>
   rc db query <db> "SELECT ..."
   ```
   Queries run host-side through RootCause's database proxy and project scoping. Prefer narrow SELECTs
   and explicit columns. Large or repeated analysis belongs in local `jq`/scripts over JSON output:
   ```bash
   rc db query <db> "SELECT ..." -o json | jq '.rows[]'
   ```
   If a query fails on a column name, stop and inspect schema:
   ```bash
   rc db schema <db> --table <table> -o json |
     jq -r '.. | objects | select(has("name") and has("type")) | [.name,.type] | @tsv'
   ```

3. For mounted workspace files, inspect before assuming paths:
   ```bash
   rc bash run 'find /brain -maxdepth 2 -type f | sed -n "1,80p"'
   rc bash run 'find /kb -maxdepth 3 -type d -print | sed -n "1,120p"'
   rc bash run 'rg -n -i "invoice|payment|refund" /kb /brain/knowledge -g "*.md" 2>/dev/null | sed -n "1,60p"'
   ```
   Use `/kb` for synced knowledge-base articles when configured; use `/brain/knowledge` when the brain
   commits its own knowledge articles. For title and frontmatter filters, read
   [docs/knowledge-base.md](../../docs/knowledge-base.md).

4. For scripts/logs, inspect the catalog:
   ```bash
   rc bash list
   ```
   `rc bash run` is the workspace-exec plane and may be unavailable on older/v1 servers. When available,
   use cataloged scripts before raw bash. `rc brain sync` invalidates warm bash workspaces, so the next
   run remounts the refreshed `/brain`. Logs are reached through that exec plane, usually with `python -m
   lib.cloudwatch ...`, not a separate log verb.

5. For actions, preflight before running:
   ```bash
   rc action list
   rc action show <id>
   rc action preflight <id> --params '{"key":"value"}'
   rc action run <id> --params '{"key":"value"}'
   ```
   `run` is a real state-changing operation. Use it only when the user asked for execution or the task
   clearly requires it and params were grounded. Report the action run id, status, and result summary.

## Brain-script catalog convention

To make scripts discoverable through `rc bash list` / `rc capabilities`, put small comment metadata near
the top of each `skills/*/scripts/*.py` or `.sh` file:

```python
# name: invoice_lookup
# purpose: Find invoice/payment state for a customer-visible invoice id.
# args: --invoice-id <id>
# required_env: APP_DSN, STRIPE_API_KEY
```

Keep names stable, purposes one line, args human-readable, and required env names only. The script body
stays the source of truth for behavior.

## Close-out

Report exactly what was run, the scoped project/tenant if relevant, the material result, and any next
command worth running. For any mutation, include whether it was preflight-only or executed.
