# rootcause-brain-skills

**This repo produces the reusable skills (Agent Skills) + engine + `rootcause-runtime` package that get
imported into our project BRAIN repositories.** It is the *kit*, not a brain itself. Skills authored
here are installed once (Claude Code / Codex plugin, or a local symlink) and run against whatever
brain you're `cd`'d into — killing the per-brain copy/drift problem.

The code + the shipped `SKILL.md` are the durable record; release mechanics live in
[RELEASING.md](RELEASING.md).

## Default close-out

For this repo, a completed agent change should be **released and pushed by default** after the focused
checks pass. Use best judgment to hold instead when there is a concrete reason: failing verification,
unrelated dirty worktree state that would be swept into the release, secrets or irreversible external
effects, missing runtime/image access, or the user explicitly asked not to push/release. Skill/doc-only
changes still get the standard patch release (`./refresh-brains.sh --release patch`) so local brains
can actually fetch the new tag.

## What a "brain" is (the consumer)

A **brain** = `rootcause-org/rootcause-brain-<project>` — a private git repo of markdown **skills** +
Python **grounding scripts** (`from lib import db`) + **runbooks** + **actions** that an agent loop
reads (mounted **read-only at `/brain`**) to draft a project's support replies. It's also the
customer's read-only audit/trust artifact (human-readable notes + code, **never secrets**).

- Brain git mechanics: [rootcause `.agents/skills/architecture/brain.md`](../rootcause/.agents/skills/architecture/brain.md)
- Authoring brain content: [rootcause `.agents/skills/brain-authoring/SKILL.md`](../rootcause/.agents/skills/brain-authoring/SKILL.md)

A run never writes its brain; durable knowledge grows out-of-band (per-run journal → weekly
consolidation PR an operator merges).

## What ships from HERE (and what must NOT)

Only **brain-author/test tooling** that is *infra-free and customer-world-facing* belongs here. The
litmus test: **does it touch OUR host** (Postgres registry / River / `run_events` / Frankfurt
CloudWatch / the box over SSM)? If yes → it stays in `rootcause`, never here.

| Ships here | Stays in rootcause |
|---|---|
| `brain_run.py`, `brain_test.py`, the `brain-dev` + `observability` skills | operator host-debug over SSM (`db.py`, `logs.py`) |
| `rootcause-runtime` (`lib`) package, incl. the `lib/run_dump` index+JSONL renderer | the operator raw-SQL escape hatch (`db.py` over the registry DB) |
| `brain_dump.py` (run dump over the public API + OAuth token) | — (the operator run dump migrated to the public-API CLI: `rc run <id> --debug`) |
| workspace Dockerfile / published image ref | anything reading `accounts.yml` or SSM |

The run-dump split is the litmus test in action: the **renderer is shared** (one `rootcause-runtime`
module, pulled by every consumer via the tag pin, so output is byte-identical), but the **fetch
differs** — `brain_dump.py` shells `rc run <id> --full` (public API, OAuth token, infra-free → here);
the operator's `rc run <id> --debug` likewise rides the public API now (the old SSM `rc_agent_debug.py`
was retired). Only `db.py`/`logs.py` (true host/SSM access) stay in rootcause.

## Two distribution concerns (keep separate)

| Concern | Mechanism | Update |
|---|---|---|
| **Skills + engine** | a skill collection (`skills/*`), with the engine inside `skills/brain-dev/scripts/`, shipped three ways: Claude Code plugin (`.claude-plugin/marketplace.json`), Codex plugin (`.agents/plugins/marketplace.json` + `.codex-plugin/plugin.json`), local symlink (`install.sh`) | `/plugin marketplace update` · `codex plugin marketplace upgrade` · the local-symlink fleet updates via the standard flow [`./refresh-brains.sh`](refresh-brains.sh) (release + re-run `install.sh` per brain) |
| **`lib` → `rootcause-runtime`** | pinned Python package, consumed by git tag | bump the tag |

**The trap:** vendoring/copying `lib` here creates *`lib` drift* — a green local test against a stale
`lib` is a *false* green. `lib` must have exactly **ONE** source of truth, pinned by tag. Both prod
(`rootcause/runtime/Dockerfile`) and local installs pull identical versioned bytes, so "tested
locally" provably equals "runs in prod". Keep the plugin tag, `rootcause-runtime` pin, image tag, and
prod Dockerfile pin moving **together**.

**The prod consumer (other end of the coupling).** `runtime/lib/` here is canonical; the only place
that consumes it in production is **`rootcause/runtime/Dockerfile`**, which installs
`rootcause-runtime @ git+…@v<TAG>#subdirectory=runtime` (NOT a `COPY lib/`). That repo builds its
workspace image from `runtime/` on deploy (`deploy/bootstrap.sh`, triggered by a push to its `stable`
branch). So a `lib` change is only *live* once you: edit it here → bump the version line per
[RELEASING.md](RELEASING.md) + tag + publish the ghcr image → bump the pin in
`rootcause/runtime/Dockerfile` → deploy. Never edit `lib` in `rootcause` — the cutover
removes its duplicate copy ([docs/migration-rootcause.md](docs/migration-rootcause.md));
`rootcause`'s devops skill carries the reciprocal note.

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
skills/*/SKILL.md                     # install-once skills for local brain dev, run debugging, and rc observability
skills/brain-dev/scripts/             # ENGINE inside the skill: brain_env.py · brain_run.py · brain_test.py · brain_dump.py
.claude-plugin/marketplace.json       # Claude Code plugin catalog
plugin.json                           # Claude Code plugin manifest
.agents/plugins/marketplace.json      # Codex plugin catalog
.codex-plugin/plugin.json             # Codex plugin manifest (skills: ./skills/)
install.sh                            # per-brain primitive: pin the shared clone + symlink all skills in (gitignored)
refresh-brains.sh                     # STANDARD FLOW: cut a release (RELEASING.md) + fan install.sh out to every local brain
runtime/                              # rootcause-runtime package (lib: db, stripe, cloudwatch, fs, http, livecheck, run_dump…)
docker/Dockerfile                     # workspace image (or published-tag ref)
docs/rc-cli.md                        # the project's `rc` CLI (sibling rootcause-cli) + ground-first author→verify loop
README.md  AGENTS.md  RELEASING.md
```

The skills install natively in Claude Code (`.claude/skills`) and Codex (`.agents/skills`). The
`brain-dev` skill is self-contained (engine in its own `scripts/`), and every SKILL.md references
scripts or sibling skills relatively — never `${CLAUDE_PLUGIN_ROOT}` or a clone path (those don't port
to Codex).

## Tooling

`uv` for Python (`uv add|run|sync`), `pnpm` for any JS, `mise` for versions. Author/edit a `SKILL.md`
with the `skill-creator` skill; mirror an example brain (`rootcause-brain-momentum-tools`,
`rootcause-brain-pro-backup`) for exact frontmatter/format. QA every script with `py_compile` and the
live test tiers before pushing.
