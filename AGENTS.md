# rootcause-brain-skills

Reusable brain-development kit for **external project developers and their AI agents**.

This repo is the kit, not a brain:

- shipped agent skills in `skills/*/SKILL.md`;
- the Local Brain Work engine in `skills/local-brain-work/scripts/`;
- the `rootcause-runtime` Python package in `runtime/` (`lib` helpers imported by brain scripts) —
  **edits here are release-gated; see [Version Coherence](#version-coherence)**;
- the workspace image in `docker/`;
- installer/plugin/release plumbing.

Brains consume this kit from a brain checkout (`rootcause-brain-<project>`). Skills install once and
run from inside any brain; never copy this kit into a brain's committed `skills/`.

## Audience

One audience only: project developers and their agents.

They have:

- a brain checkout;
- `rc login`;
- maybe Docker and local `.env` via `rc env pull`.

They do **not** have:

- private RootCause app source;
- host shells, SSM, registry DB access, `accounts.yml`;
- private operator scripts.

If a workflow is not exposed through public `rc`/API, produce a RootCause support request. Do not leak
private mechanics into shipped docs or skills.

Do not repeat RootCause's generic production prompt in brain docs. `emailPreamble` and capability-gated
sections in `rootcause/internal/agent/prompt.go` already own the generic support-engineer role,
draft/note split, read-only workspace, actions/preflight, PII, DB scoping, mirrors, and grounding
mandate. Brain docs should add project business context, playbooks, source/KB pointers, and action
rules.

## Core Model

Read these before changing product-facing docs/skills:

- [docs/brain-model.md](docs/brain-model.md) — audience, brain-vs-external context, prompt boundary,
  layout, refs, mounts, tenant/project model.
- [docs/run-trace-model.md](docs/run-trace-model.md) — how to read `rc run --debug`.
- [docs/side-effects.md](docs/side-effects.md) — read-only vs explicit side effects.
- [docs/support-boundary.md](docs/support-boundary.md) — brain fix vs support escalation.
- [docs/mirrors.md](docs/mirrors.md) — source mirror freshness and local/prod gaps.
- [docs/secrets.md](docs/secrets.md) — public `rc` flow for adding/rotating grounding env and action
  credentials.

## Canonical Skills

Only these are first-class:

- `local-brain-work` — local scripts/tests/projection/action checks; broad router.
- `brain-ask` — last-mile prod/test run validation with `rc ask`.
- `rc-debug` — one run/thread/session trace; inspect/propose/stop before edits.
- `rc-health` — stale mirrors and dead-lettered runs.
- `rc-fleet` — recent fleet and recurring failure patterns.
- `prod-console` — direct guarded production primitives through `rc capabilities`, `rc db`, `rc bash`, and `rc action`.
- `brain-dev-upgrade` — update kit and `rc`.
- `brain-publish` — post-edit publish/support-request step.

Do not reintroduce aliases such as `brain-debug`, `observability`, `rc-inspect`, or `rc-thread`.

## Boundaries

Ships here:

- local brain engine: `brain_run.py`, `brain_test.py`, `brain_projection.py`, `brain_action.py`,
  `brain_dump.py`;
- public-API skills over `rc`;
- `rootcause-runtime` (`runtime/lib`);
- workspace Dockerfile/image.

Stays out:

- RootCause host/debug/operator mechanics;
- registry/River/raw SQL/debug scripts;
- secrets, credentials, private repo paths;
- fake publish/promote wrappers for capabilities not public yet.

Diagnosis is read-only by default. Exceptions must be explicit: `rc ask` creates runs, action confirm
executes writes, and `brain_action.py --commit` writes to whatever `.env.action` targets.

## Version Coherence

**Did you touch `runtime/lib/**`, `runtime/pyproject.toml`, or `runtime/requirements.lock`? Then you
owe a release — a bare merge/push does NOT reach prod.** The box installs the *pinned tag*, not `main`,
so unreleased lib bytes never ship. Close it out with `./refresh-brains.sh --release <patch|minor>`
(bumps + tags + pushes the whole line below; `--relock` if deps changed), then in the **rootcause** repo
`scripts/bump-workspace-pin.py vX.Y.Z` → commit → promote. Full steps: [RELEASING.md](RELEASING.md).
rootcause's `promote.py` preflight now **FAILs** while lib commits sit past the pin (even unpushed local
ones), so a forgotten release blocks the next deploy — but don't rely on the gate; release as you go.

One version line moves together:

- plugin manifests;
- `skills/local-brain-work/scripts/brain_env.py`;
- `runtime/pyproject.toml`;
- install docs;
- workspace image tag;
- `rootcause/runtime/Dockerfile` pin.

Run `./check-release-coherence.sh` before trusting a release. Runtime dependency changes require
`runtime/requirements.lock` and the sibling `rootcause/runtime/requirements.lock` to match.

## Default Close-Out

After focused checks pass, release and push by default.

Use `./refresh-brains.sh --release patch` for docs/skill changes too; local brains fetch tags, not
floating `main`.

Hold instead only for concrete blockers: failing checks, unrelated dirty files that would be swept in,
missing image/runtime access, secrets/irreversible effects, or explicit user instruction.

## Verification

Use the smallest checks that cover the change:

```bash
uv run --no-project python -m py_compile skills/local-brain-work/scripts/*.py
cd runtime && uv run --with . --with pytest --no-project pytest tests -q
./check-release-coherence.sh
```

For skill edits, validate each `skills/*/SKILL.md` with the skill-creator quick validator. For markdown
changes, scan relative links and stale private references.

## Tooling

- Python: `uv` only.
- Node/JS: `pnpm` only.
- Versions/env: `mise`.
- Shell search: prefer `rg`.
- Skill authoring: use the `skill-creator` skill.
