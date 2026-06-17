# rootcause-brain-skills

**This repo produces the reusable skills (Agent Skills) + engine + `rootcause-runtime` package that get
imported into our project BRAIN repositories.** It is the *kit*, not a brain itself. Skills authored
here are installed once (as a Claude Code plugin) and run against whatever brain you're `cd`'d into —
killing the per-brain copy/drift problem.

Build details live in [SPEC.md](SPEC.md) (delete once implemented — code + shipped `SKILL.md` are the
durable record).

## What a "brain" is (the consumer)

A **brain** = `rootcause-org/rootcause-brain-<project>` — a private git repo of markdown **skills** +
Python **grounding scripts** (`from lib import db`) + **runbooks** + **actions** that an agent loop
reads (mounted **read-only at `/brain`**) to draft a project's support replies. It's also the
customer's read-only audit/trust artifact (human-readable notes + code, **never secrets**).

- Brain git mechanics: [rootcause-light `.agents/skills/architecture/brain.md`](../rootcause-light/.agents/skills/architecture/brain.md)
- Authoring brain content: [rootcause-light `.agents/skills/brain-authoring/SKILL.md`](../rootcause-light/.agents/skills/brain-authoring/SKILL.md)

A run never writes its brain; durable knowledge grows out-of-band (per-run journal → weekly
consolidation PR an operator merges).

## What ships from HERE (and what must NOT)

Only **brain-author/test tooling** that is *infra-free and customer-world-facing* belongs here. The
litmus test: **does it touch OUR host** (Postgres registry / River / `run_events` / Frankfurt
CloudWatch / the box over SSM)? If yes → it stays in `rootcause-light`, never here.

| Ships here | Stays in rootcause-light |
|---|---|
| `brain_run.py`, `brain_test.py`, the `brain-dev` skill | operator host-debug (`db.py`, `logs.py`, `rc_*_debug.py`) |
| `rootcause-runtime` (`lib`) package | key/env plumbing (`rc_secret.py`, `rc_env.py`) |
| workspace Dockerfile / published image ref | anything reading `accounts.yml` or SSM |

## Two distribution concerns (keep separate)

| Concern | Mechanism | Update |
|---|---|---|
| **Skill + engine** | Claude Code plugin marketplace (`.claude-plugin/marketplace.json`) | `/plugin marketplace update` |
| **`lib` → `rootcause-runtime`** | pinned Python package, consumed by git tag | bump the tag |

**The trap:** vendoring/copying `lib` here creates *`lib` drift* — a green local test against a stale
`lib` is a *false* green. `lib` must have exactly **ONE** source of truth, pinned by tag. Both prod
(`rootcause-light/runtime/Dockerfile`) and local installs pull identical versioned bytes, so "tested
locally" provably equals "runs in prod". Keep the plugin tag, `rootcause-runtime` pin, image tag, and
prod Dockerfile pin moving **together**.

## Two run modes (the engine offers both — fidelity vs. speed)

- **Fast `uv` mode** — inner loop. Reproduces the import surface + per-project env + read-only DB
  grounding. Does NOT reproduce the egress allowlist, `:ro` mounts (`EROFS`), or container isolation.
  **Green `uv` run ≠ guaranteed-green prod.**
- **Faithful `docker` mode** — pre-push gate. Runs the published workspace image with `/brain:ro` +
  `/mirrors/<name>:ro`. Reproduces the exact dep set, mounts, isolation. The honest "does it work in
  the box?" check.

## Invariants

- **No secrets, ever.** Env by **NAME** only; `.env` is gitignored and host-injected.
- **Read-only by default.** The kit, like a real run, never writes a brain, posts a callback, or hits
  the box. Diagnosis is always read-only; the only state-changing plane is a self-owned project's
  `actions/`.
- **Skills install once, run from inside any brain** — never copied into a brain's `skills/` (that's
  the drift this repo exists to kill). Only project-specific test fixtures live in the brain.
- **No ASCII diagrams** — Mermaid only.

## Layout (target, per SPEC §6)

```
.claude-plugin/marketplace.json   # plugin catalog
plugin.json                       # plugin manifest
skills/brain-dev/SKILL.md         # the install-once brain-dev/test skill
scripts/                          # ENGINE: brain_env.py · brain_run.py · brain_test.py (brain-dir-relative)
runtime/                          # rootcause-runtime package (lib: db, stripe, cloudwatch, fs, http, livecheck…)
docker/Dockerfile                 # workspace image (or published-tag ref)
SPEC.md  README.md  AGENTS.md
```

## Tooling

`uv` for Python (`uv add|run|sync`), `pnpm` for any JS, `mise` for versions. Author/edit a `SKILL.md`
with the `skill-creator` skill; mirror an example brain (`rootcause-brain-momentum-tools`,
`rootcause-brain-pro-backup`) for exact frontmatter/format. QA every script with `py_compile` and the
live test tiers before pushing.
