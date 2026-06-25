---
name: brain-dev
description: "Iterate on and verify a rootcause project's BRAIN locally: map a brain checkout, run grounding scripts, run offline/live test tiers, preview tenant projection, test hosted Python actions locally, route broad did-it-work/is-anything-broken prompts to the focused rc skills, and check a brain change before pushing. Use inside a rootcause-brain checkout; no private RootCause source required."
---

# brain-dev - local brain work

A brain is markdown knowledge plus Python grounding/action scripts that production mounts read-only at
`/brain`. This skill runs the reusable local engine shipped in `scripts/` against the current brain
checkout. Use public `rc` commands for production runs; do not use RootCause-private repos, SSM, host
database shells, or slash-command mechanics.

## Required Context

Read:

- [docs/brain-model.md](../../docs/brain-model.md)
- [docs/side-effects.md](../../docs/side-effects.md)

Read [docs/mirrors.md](../../docs/mirrors.md) when a script uses `lib.fs`, `/mirrors`, or source/KB
mounts.

## Route Broad Prompts

| User intent | Use |
|---|---|
| Run a grounding script, local tests, projection preview, or hosted-Python action dry-run | `brain-dev` |
| "Does this change work on prod infra?" or "simulate this customer email" | `brain-ask` |
| Debug one run/thread/session, read full trace, or explain why a draft/action happened | `rc-debug` |
| "Is anything broken?" stale mirrors or dead letters | `rc-health` |
| "What keeps failing?" recent fleet/pattern review | `rc-fleet` |
| Update local skills kit or `rc` CLI | `brain-dev-upgrade` |
| Make local brain edits live, sync, promote, publish, or prepare support handoff | `brain-publish` |

## Locate The Engine

Set `SKILL` to the directory containing this `SKILL.md`:

```bash
SKILL=<absolute path to skills/brain-dev>
```

The engine files are `brain_env.py`, `brain_run.py`, `brain_test.py`, `brain_action.py`,
`brain_projection.py`, and `brain_dump.py`. They resolve `lib` from the sibling `runtime/` package when
present, otherwise from the tag-pinned `rootcause-runtime` git spec.

## Local Workflow

1. Map the brain:
   ```bash
   uv run "$SKILL/scripts/brain_run.py" --brief
   ```
   Also read the brain's `AGENTS.md` and relevant project skill/playbook docs.

2. Run one grounding script or ad-hoc read-only query:
   ```bash
   uv run "$SKILL/scripts/brain_run.py" skills/databases/scripts/lookup_customer.py --email a@b.com
   uv run "$SKILL/scripts/brain_run.py" -m lib.db --list
   uv run "$SKILL/scripts/brain_run.py" -m lib.db "select count(*) from accounts"
   ```

3. Run test tiers:
   ```bash
   uv run "$SKILL/scripts/brain_test.py"
   uv run "$SKILL/scripts/brain_test.py" --live
   uv run "$SKILL/scripts/brain_test.py" --require-live
   uv run "$SKILL/scripts/brain_test.py" --live --tenant 103
   ```

4. Use docker mode for image/dependency/read-only-mount confidence:
   ```bash
   uv run "$SKILL/scripts/brain_run.py"  --mode docker skills/databases/scripts/lookup_customer.py --email a@b.com
   uv run "$SKILL/scripts/brain_test.py" --mode docker --live
   ```
   Docker mode uses the published workspace image, `/brain:ro`, `/mirrors:ro`, container user/rootfs/env
   isolation, and the same runtime dependency surface. It does **not** prove the production egress
   allowlist; use `rc ask --brain-ref` for that.

5. Report result, mode, and fidelity caveat. A green `uv` run is not a guaranteed-green production run.

## Hosted Python Actions

`brain_action.py` is the local state-changing exception. It reproduces hosted-Python action validation,
preflight, and body execution using `./.env.action`; dry-run rolls back by default.

```bash
uv run "$SKILL/scripts/brain_action.py" --list
uv run "$SKILL/scripts/brain_action.py" <id> --params '<json>' --preflight-only
uv run "$SKILL/scripts/brain_action.py" <id> --params '<json>'
uv run "$SKILL/scripts/brain_action.py" <id> --params '<json>' --commit
```

`--commit` writes for real to whatever `.env.action` targets. Use safe local/staging targets unless the
user intentionally asked for a real write. Read [docs/actions.md](../../docs/actions.md) and
[action-run-triage.md](action-run-triage.md) for production action evidence.

## Tenant Projection

For templated shared project brains, production may compile a tenant-specific `/brain` view from
`projection.yaml` plus tenant settings. Preview locally:

```bash
uv run "$SKILL/scripts/brain_projection.py" --tenant <slug>
```

Tenant-enabled shared brains often run from a channel ref such as `stable`; pushing `main` is not the
same as making a shared-brain change live. Use `brain-publish` after committing.

## Production Confidence

Local runners do not reproduce the full LLM loop, warm start, grounding pre-step, tenant scoping,
production egress, callback delivery, or post-loop journal/action handling. For that, use `rc`:

```bash
rc ask "<customer-style question>"
rc ask "<direct investigation>" --scenario raw
rc ask "<question>" --brain-ref dev/<branch>
rc run <run_id> --debug
uv run "$SKILL/scripts/brain_dump.py" <run_id>
```

`brain_dump.py` writes gitignored files under `.rootcause/dump/`. Read the markdown index first, then
drill into JSONL with `jq`. For analysis-first debugging, use `rc-debug`.

## Env

If `.env` is missing for live local checks, use:

```bash
rc env pull
```

`rc env pull` writes the production grounding `.env` using the logged-in OAuth token. It prints no
secret values. If a private DB is not reachable from the laptop, treat that as an infra boundary and
verify with `rc ask` instead of forcing local live tests.

## Finish

After local edits: verify with the smallest useful local checks, commit in the brain repo, optionally
run `brain-ask` against a pushed `dev/*` ref, then use `brain-publish`.
