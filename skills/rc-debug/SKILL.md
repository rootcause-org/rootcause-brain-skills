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
   - Run UUID: continue with `rc run debug <uuid>`.
   - `rc fleet actions` row: use its `run_id`, not the action-run `id`; the tokenized `run_url` opens
     the same originating run in the UI. A null `run_id` is a direct operator action with no run trace.
     Read `error_class` first (`rc run actions <uuid>` shows it per run too): an infra class
     (`executor_predispatch`, `executor_error`, `no_executor`, `no_runner_url`, `attachment_fetch`) means
     RootCause's machinery failed — report it, do not edit the brain. See
     [docs/actions.md](../../docs/actions.md#failure-classes-infra-vs-domain).
   - Thread/session id: run `rc run thread <id>`. If it prints a run UUID, continue with that run. If
     there is no run, explain the public channel/support boundary.
   - Question/prompt: use `brain-ask` unless the user explicitly asked to trigger and inspect a fresh
     run in one flow.
   - No usable input: ask for a run UUID, thread/session id, or question and stop.

2. Decompose the run:
   ```bash
   rc run debug <uuid>
   ```
   Output lands under `.rootcause/debug/<run8>-<project>.{md,jsonl}`. On 401/scope errors, run `rc auth
   status` and suggest `rc auth login`; on missing public data, produce a support request.

3. Read the markdown index first. Report status, scenario, question, test-run marker, tenant/ref,
   outcome, flags, and the likely area to inspect.

4. Drill into JSONL only for a specific step/question:
   ```bash
   jq -r 'select(.disp=="23").command' .rootcause/debug/<file>.jsonl
   jq -r 'select(.disp=="23").stdout'  .rootcause/debug/<file>.jsonl
   jq -r 'select(.exit_code != null and .exit_code != 0).disp' .rootcause/debug/<file>.jsonl
   jq -r 'select(.command // "" | contains("invoice")).disp' .rootcause/debug/<file>.jsonl
   jq -r 'select(.reasoning) | .disp + " " + .reasoning' .rootcause/debug/<file>.jsonl
   ```

5. If evidence points to a brain bug, inspect likely brain files read by the run plus focused `rg`
   searches. Do not edit yet. If the trace shows missing tables or an unknown database short name, the
   database may simply not be registered — check `rc project database ls` and
   [docs/secrets.md](../../docs/secrets.md#register-a-new-grounding-database) before touching brain
   files.

6. Stop with:
   - root cause or best hypothesis
   - evidence from trace/files
   - smallest proposed brain change
   - verification plan, usually `brain-ask` with `--brain-ref dev/<branch>`
   - publish path, usually `brain-publish` after the fix is committed

## Run header (JSONL line 1)

Line 1 is the `type=="run"` header — the run's inputs, not its steps. Every field is top-level (no
`.run` wrapper); every other line is `type=="event"`.

```bash
F=.rootcause/debug/<file>.jsonl
jq -r 'select(.type=="run").system_prompt' "$F"
jq    'select(.type=="run").tenant_settings' "$F"          # settings as they were at run time
jq    'select(.type=="run").tenant_settings_current' "$F"  # settings now — diff the two for drift
jq    'select(.type=="run").tenant_settings_drift' "$F"    # host-computed {key,then,now}, when it differs
jq -r 'select(.type=="run").brain_resolved' "$F"           # the exact brain ref/SHA the run read
jq    'select(.type=="run").grounding_sources' "$F"        # per-source mounted/available/ref + .drift
```

Drift is the first thing to rule out: a run answered against the settings, brain SHA, and mirror
state of *its* moment, not today's.

The `system_prompt` is long (tens of thousands of chars) but is only plane 1 of the assembled
context — the run also receives a
bootstrap/brain-plane user turn and the thread itself, so never read it as the whole prompt.

### Prompt context (rc >= 1.14.0)

```bash
jq -r 'select(.type=="run").context_schema_version' "$F"   # 0 = not captured (pre-1.14 run, or past the 7-day retention window)
jq -r 'select(.type=="run").prompt_sections[]? | "\(.on) \(.id) \(.gate)"' "$F"                 # section map, ~44 rows
jq -r 'select(.type=="run").prompt_sections[]? | select(.on|not) | .id + "  " + .gate' "$F"     # sections that were OFF
jq -r 'select(.type=="run").prompt_sections[]? | select(.id=="preamble").text' "$F"             # one section verbatim (off sections carry no .text)
jq -r 'select(.type=="run").bootstrap_turn, select(.type=="run").preselected_turn' "$F"         # the orientation user turns, verbatim
jq -r 'select(.type=="run").manifest_blocks[]? | "\(.presence)\t\(.chars)\t\(.path)\t\(.gloss)"' "$F"  # what was pasted/mapped into context
jq -r 'select(.seq>=4000000) | "\(.disp) \(.command)"' "$F"          # draft-cleanup polish passes (disp C1, C2, …)
jq -r 'select(.disp=="C1").args | .before, .after, .rejected_diff' "$F"  # applied rewrite, or the refused one
```

For "why didn't the model know X", the `on:false` sections are the signal — a gate that stayed shut is
context the run never received.

Full per-section provenance of the assembled prompt lives host-side (`rootcause` repo,
`prompt-assembly-map.md`); operators with host access read it there.

Only edit files when the user explicitly asks to implement the proposed fix. After edits, verify with
Local Brain Work (`local-brain-work`)/`brain-ask`, then use `brain-publish` for the live/support step.
