# rootcause-brain-skills

Reusable brain-development kit for **external project developers and their AI agents**.

This repo is the kit, not a brain:

- shipped agent skills in `skills/*/SKILL.md`;
- the local brain-dev engine in `skills/brain-dev/scripts/`;
- the `rootcause-runtime` Python package in `runtime/` (`lib` helpers imported by brain scripts);
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

## Canonical Skills

Only these are first-class:

- `brain-dev` — local scripts/tests/projection/action checks; broad router.
- `brain-ask` — trigger one real prod/test run with `rc ask`.
- `rc-debug` — one run/thread/session trace; inspect/propose/stop before edits.
- `rc-health` — stale mirrors and dead-lettered runs.
- `rc-fleet` — recent fleet and recurring failure patterns.
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

One version line moves together:

- plugin manifests;
- `skills/brain-dev/scripts/brain_env.py`;
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
uv run --no-project python -m py_compile skills/brain-dev/scripts/*.py
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
