---
name: rc-debug
description: "Debug ONE rootcause production run, thread, or session from a brain checkout with `rc run debug` / `rc run thread`. Use when given a run UUID, thread/session id, a failed run, a UUID flagged by rc-fleet/rc-health, or a delivery question. Analysis-first: inspect evidence, propose the smallest fix, stop before editing brain files unless asked to implement."
---

# rc-debug — inspect one run/thread

Public `rc` only; scope comes from `.rootcause.toml` + the OAuth login
([docs/support-boundary.md](../../docs/support-boundary.md)).

## Required Context

- [docs/run-trace-model.md](../../docs/run-trace-model.md) — trace concepts, reading order, and the
  debug discipline (a single failed run is evidence, not permission to oversteer the brain).
- [docs/brain-model.md](../../docs/brain-model.md).
- [docs/actions.md](../../docs/actions.md) when the trace carries action/preflight artifacts.

## Resolve the input first

- **Run UUID** → `rc run thread <uuid>` for thread/session lineage, then `rc run debug <uuid>`.
- **`rc fleet actions` row** → use its `run_id` (not the action-run `id`); `run_url` opens the same
  originating run. A null `run_id` = a direct operator action with no run trace. Read `error_class`
  first: an infra class means RootCause's machinery failed — report it, do not edit the brain
  ([docs/actions.md](../../docs/actions.md#failure-classes-infra-vs-domain)).
- **Thread/session id** → `rc run thread <id>`. No run at all → explain the channel/support boundary.
- **A question, not an id** → `brain-ask`.
- **Nothing usable** → ask for a run UUID, thread/session id, or question, and stop.

## Drill

`rc run debug <uuid>` writes `.rootcause/debug/<run8>-<project>.{md,jsonl}`. Read the markdown index,
then query the JSONL by `disp` for one step's `command` / `stdout` / `reasoning`. On 401/scope errors
check `rc auth status`; on missing public data, produce a support request.

Missing tables or an unknown database short name in the trace usually means the database was never
registered — check `rc dev console database list` (authoritative;
[docs/secrets.md](../../docs/secrets.md#register-a-new-grounding-database)) before touching brain
files.

## Run header (JSONL line 1)

Line 1 is the `type=="run"` header — the run's **inputs**, not its steps; fields are top-level (no
`.run` wrapper). Every other line is `type=="event"`.

**Rule out drift first.** A run answered against the settings, brain SHA, and mirror state of *its*
moment, not today's:

```bash
F=.rootcause/debug/<file>.jsonl
jq 'select(.type=="run") | {tenant_settings, tenant_settings_current, tenant_settings_drift,
                            brain_resolved, grounding_sources}' "$F"
```

`system_prompt` is only plane 1 of the assembled context — the run also gets a bootstrap/brain-plane
user turn and the thread itself. Never read it as the whole prompt.

**Why didn't the model know X?** The prompt-context capture (`context_schema_version`; `0` = a
pre-1.14 run or past the 7-day retention window) carries the section map. The `on:false` sections are
the signal — a gate that stayed shut is context the run never received:

```bash
jq -r 'select(.type=="run").prompt_sections[]? | select(.on|not) | .id + "  " + .gate' "$F"
jq -r 'select(.type=="run").prompt_sections[]? | select(.id=="<id>").text' "$F"  # off sections carry no .text
jq -r 'select(.type=="run") | .bootstrap_turn, .preselected_turn' "$F"
jq -r 'select(.type=="run").manifest_blocks[]? | "\(.presence)\t\(.chars)\t\(.path)\t\(.gloss)"' "$F"
jq -r 'select(.seq>=4000000).args | .before, .after, .rejected_diff' "$F"  # draft-cleanup passes (disp C1, C2, …)
```

## Stop with

Root cause or best hypothesis · trace/file evidence · the smallest proposed brain change ·
a verification plan (usually `brain-ask --brain-ref dev/<branch>`) · the publish path
(`brain-publish`).

Edit files only when the user explicitly asks to implement. Then verify with `local-brain-work` /
`brain-ask` and finish through `brain-publish`.
