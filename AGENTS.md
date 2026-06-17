# rootcause-brain-skills

**This repo produces the reusable skills (Agent Skills) + engine + `rootcause-runtime` package that get
imported into our project BRAIN repositories.** It is the *kit*, not a brain itself. Skills authored
here are installed once (Claude Code / Codex plugin, or a local symlink) and run against whatever
brain you're `cd`'d into — killing the per-brain copy/drift problem.

The code + the shipped `SKILL.md` are the durable record; release mechanics live in
[RELEASING.md](RELEASING.md).

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
| **Skill + engine** | one self-contained skill (`skills/brain-dev/`, engine in its `scripts/`), shipped three ways: Claude Code plugin (`.claude-plugin/marketplace.json`), Codex plugin (`.agents/plugins/marketplace.json` + `.codex-plugin/plugin.json`), local symlink (`install.sh`) | `/plugin marketplace update` · `codex plugin marketplace upgrade` · re-run `install.sh` |
| **`lib` → `rootcause-runtime`** | pinned Python package, consumed by git tag | bump the tag |

**The trap:** vendoring/copying `lib` here creates *`lib` drift* — a green local test against a stale
`lib` is a *false* green. `lib` must have exactly **ONE** source of truth, pinned by tag. Both prod
(`rootcause-light/runtime/Dockerfile`) and local installs pull identical versioned bytes, so "tested
locally" provably equals "runs in prod". Keep the plugin tag, `rootcause-runtime` pin, image tag, and
prod Dockerfile pin moving **together**.

**The prod consumer (other end of the coupling).** `runtime/lib/` here is canonical; the only place
that consumes it in production is **`rootcause-light/runtime/Dockerfile`**, which installs
`rootcause-runtime @ git+…@v<TAG>#subdirectory=runtime` (NOT a `COPY lib/`). That repo builds its
workspace image from `runtime/` on deploy (`deploy/bootstrap.sh`, triggered by a push to its `stable`
branch). So a `lib` change is only *live* once you: edit it here → bump the version line per
[RELEASING.md](RELEASING.md) + tag + publish the ghcr image → bump the pin in
`rootcause-light/runtime/Dockerfile` → deploy. Never edit `lib` in `rootcause-light` — the cutover
removes its duplicate copy ([docs/migration-rootcause-light.md](docs/migration-rootcause-light.md));
`rootcause-light`'s devops skill carries the reciprocal note.

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

## Layout

```
skills/brain-dev/SKILL.md             # the install-once brain-dev/test skill (self-contained)
skills/brain-dev/scripts/             # ENGINE inside the skill: brain_env.py · brain_run.py · brain_test.py
.claude-plugin/marketplace.json       # Claude Code plugin catalog
plugin.json                           # Claude Code plugin manifest
.agents/plugins/marketplace.json      # Codex plugin catalog
.codex-plugin/plugin.json             # Codex plugin manifest (skills: ./skills/)
commands/brain-dev.md                 # Claude-Code-only /brain-dev sugar (Codex needs none — SKILL.md is self-sufficient)
install.sh                            # local gitignored symlink install (cross-agent)
runtime/                              # rootcause-runtime package (lib: db, stripe, cloudwatch, fs, http, livecheck…)
docker/Dockerfile                     # workspace image (or published-tag ref)
README.md  AGENTS.md  RELEASING.md
```

The skill is self-contained (engine in its own `scripts/`) so the SAME directory installs natively in
Claude Code (`.claude/skills`) and Codex (`.agents/skills`). SKILL.md references its scripts relative
to itself — never `${CLAUDE_PLUGIN_ROOT}` or a clone path (those don't port to Codex).

## Tooling

`uv` for Python (`uv add|run|sync`), `pnpm` for any JS, `mise` for versions. Author/edit a `SKILL.md`
with the `skill-creator` skill; mirror an example brain (`rootcause-brain-momentum-tools`,
`rootcause-brain-pro-backup`) for exact frontmatter/format. QA every script with `py_compile` and the
live test tiers before pushing.
