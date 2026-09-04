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
- `rc auth login`;
- maybe Docker and local `.env` via `rc project env pull`.

They do **not** have:

- private RootCause app source;
- host shells, SSM, registry DB access, `accounts.yml`;
- private operator scripts.

If a workflow is not exposed through public `rc`/API, produce a RootCause support request. Do not leak
private mechanics into shipped docs or skills.

## Two Execution Contexts

Keep these planes explicit in every skill, template, and brain edit:

- **Local brain development:** a developer or coding agent runs outside production from a brain
  checkout. After OAuth login, it may use the public `rc` CLI for runs, settings, diagnostics, and
  guarded production primitives. The CLI skills in this kit serve only this context and install
  locally into the ignored Codex-native `.agents/skills/` tree; `.claude/skills` is a **committed**
  relative symlink to that tree (Claude Code discovers only `.claude/skills`, and git worktrees carry
  tracked files only). The alias is tracked, the skills behind it are not, so the link dangles in a
  fresh checkout — prod skips dangling relative in-repo symlinks and brain lint only WARNs.
  `.worktreeinclude` carries the ignored kit paths into worktrees. They never mount into `/brain`.
- **Production main loop:** the model has `bash` plus its scenario terminal tool (`reply` for email),
  not an `rc` binary. The committed brain is mounted read-only at `/brain`. Ground through `/brain`
  scripts and the injected `lib.db`, `lib.cloudwatch`, `lib.http`, `lib.fs`, `lib.connectors`,
  `lib.api`, `lib.mcp`, and `lib.image` capabilities available for that project/run.
  (`lib.image` — cheap preview → refine-from-preview image generation over the broker's `image`
  mount, saving to `/tmp/outbox`; absent mount ⇒ one clear "not enabled" sentence.)

Never put `rc ...` command guidance in committed project-brain content. Describe the project-specific
evidence or decision; keep laptop-side control-plane steps in this kit's local skills/docs.

Do not repeat RootCause's generic production prompt in brain docs. `emailPreamble` and capability-gated
sections in `rootcause/internal/agent/prompt.go` already own the generic support-engineer role,
draft/note split, read-only workspace, actions/preflight, PII, DB scoping, mirrors, and grounding
mandate. Persona settings own brand voice, language, formality, and signature at project, tenant, and
mailbox scope. Brain docs should add project business context, terminology, playbooks, source/KB
pointers, and action rules.

## Core Model

Read these before changing product-facing docs/skills:

- [docs/brain-model.md](docs/brain-model.md) — audience, brain-vs-external context, prompt boundary,
  `include_in` frontmatter (triage/grounding/agent), layout, refs, mounts, tenant/project model.
- [docs/run-trace-model.md](docs/run-trace-model.md) — how to read `rc run debug`.
- [docs/side-effects.md](docs/side-effects.md) — read-only vs explicit side effects.
- [docs/support-boundary.md](docs/support-boundary.md) — brain fix vs support escalation.
- [docs/mirrors.md](docs/mirrors.md) — source mirror freshness and local/prod gaps.
- [docs/secrets.md](docs/secrets.md) — public `rc` flow for adding/rotating grounding env and action
  credentials, and for [registering a new grounding
  database](docs/secrets.md#register-a-new-grounding-database) (there is no `rc project database add`:
  sealing the `<PROJECT>_<DBKEY>_DSN` key creates it, `rc project database set … description=…` surfaces
  it in `ls`).

## Canonical Skills

Only these are first-class:

- `local-brain-work` — local scripts/tests/projection/action checks; broad router.
- `brain-dream-cycle` — local consolidation from run feedback/sent deltas/patterns using public `rc`,
  including persona and triage setting updates.
- `brain-harvest` — local synthesis from a mailbox's harvested sent-history corpus using public `rc`:
  trigger export, download, per-topic subagent distillation, privacy/contract lint, brain/persona/triage
  homes.
- `brain-website-scout` — local broad public-site mapping and Firecrawl capture, then per-topic
  synthesis into a progressive-disclosure brain; raw pages stay gitignored.
- `brain-ask` — last-mile prod/test run validation with `rc ask`.
- `rc-debug` — one run/thread/session trace; inspect/propose/stop before edits.
- `rc-health` — stale mirrors and dead-lettered runs.
- `rc-fleet` — recent fleet and recurring failure patterns.
- `prod-console` — direct guarded production primitives through `rc dev console capabilities`,
  `rc dev console database`, `rc dev console bash`, and `rc dev console action`.
- `rc-script-wrapper` — deterministic local Python/shell wrappers around `rc`, including complete
  exports, typed failures, parameters, and remote artifact fetches.
- `brain-dev-upgrade` — update kit and `rc`.
- `brain-git-sync` — safely reconcile local brain work with cross-computer `origin/main` and push.
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
(bumps, publishes main, tags/pushes the whole line below, then bumps + commits the sibling
**rootcause** pin for you; `--relock` if deps changed) and **promote rootcause** afterwards. Full
steps: [RELEASING.md](RELEASING.md). Three gates back this up: the release exits non-zero if the prod
bump did not land, rootcause CI (`scripts/check_workspace_pin.py`) goes red on unconsumed drift, and
rootcause's `promote.py` preflight **FAILs** while lib commits sit past the pin (even unpushed local
ones) — but don't rely on the gates; release as you go.

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
floating `main`. Releases must run on `main`; the publisher pushes `HEAD:main`, verifies
`origin/main == HEAD`, and only then pushes the version tag. `--no-push` changes neither remote ref.

Hold instead only for concrete blockers: failing checks, unrelated dirty files that would be swept in,
missing image/runtime access, secrets/irreversible effects, or explicit user instruction.

## Verification

Use the smallest checks that cover the change:

```bash
uv run --no-project python -m py_compile skills/local-brain-work/scripts/*.py
# PYTHONPATH is load-bearing here and in uv-mode children: `--with .` can serve a CACHED wheel of an
# unchanged version, so local runtime source must precede site-packages while iterating on runtime/lib.
cd runtime && PYTHONPATH=$(pwd) uv run --with '.[test]' --no-project pytest tests -q
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
