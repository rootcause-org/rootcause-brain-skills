---
name: rc-debug
description: "Debug one rootcause production run, thread, or session from a brain checkout using public rc commands and the local run-dump renderer. Use when given a run UUID, thread/session id, failed run, fleet/health UUID, delivery question, or request for the full reasoning/tool trace. Analysis-first: inspect evidence, propose the smallest fix, and stop before editing brain files unless the user explicitly asks to implement."
---

# rc-debug - inspect one run/thread

Use the public `rc` CLI from inside a brain checkout. Scope comes from `.rootcause.toml` plus the
logged-in OAuth token. Do not use private RootCause repos, SSM, database shells, or host scripts.

## Required Context

Read these before debugging:

- [docs/run-trace-model.md](../../docs/run-trace-model.md)
- [docs/brain-model.md](../../docs/brain-model.md)
- [docs/support-boundary.md](../../docs/support-boundary.md)

Also read [docs/actions.md](../../docs/actions.md) when the trace includes action/preflight artifacts.

## Workflow

Default to evidence-first. A single run is signal, not permission to oversteer the brain.

1. Resolve the input.
   - Run UUID: continue with `rc run <uuid> --debug`.
   - Thread/session id: run `rc thread <id>`. If it prints a run UUID, continue with that run. If
     there is no run, explain the public channel/support boundary.
   - Question/prompt: use `brain-ask` unless the user explicitly asked to trigger and inspect a fresh
     run in one flow.
   - No usable input: ask for a run UUID, thread/session id, or question and stop.

2. Decompose the run:
   ```bash
   rc run <uuid> --debug
   ```
   Output lands under `rc-debug/<run8>-<project>.{md,jsonl}`. On 401/scope errors, run `rc whoami`
   and suggest `rc login`; on missing public data, produce a support request.

3. Read the markdown index first. Report status, scenario, question, test-run marker, tenant/ref,
   outcome, flags, and the likely area to inspect.

4. Drill into JSONL only for a specific step/question:
   ```bash
   jq -r 'select(.disp=="23").command' rc-debug/<file>.jsonl
   jq -r 'select(.disp=="23").stdout'  rc-debug/<file>.jsonl
   jq -r 'select(.exit_code != null and .exit_code != 0).disp' rc-debug/<file>.jsonl
   jq -r 'select(.command // "" | contains("invoice")).disp' rc-debug/<file>.jsonl
   jq -r 'select(.reasoning) | .disp + " " + .reasoning' rc-debug/<file>.jsonl
   ```

5. If evidence points to a brain bug, inspect likely brain files read by the run plus focused `rg`
   searches. Do not edit yet.

6. Stop with:
   - root cause or best hypothesis
   - evidence from trace/files
   - smallest proposed brain change
   - verification plan, usually `brain-ask` with `--brain-ref dev/<branch>`
   - publish path, usually `brain-publish` after the fix is committed

Only edit files when the user explicitly asks to implement the proposed fix. After edits, verify with
Local Brain Work (`local-brain-work`)/`brain-ask`, then use `brain-publish` for the live/support step.
