# rootcause-brain-skills

One kit to iterate on a project's **brain** locally and verify it works the way production does. It's
a **single self-contained skill** (`brain-dev`, engine in its own `scripts/`) that installs
natively in **Claude Code** *and* **OpenAI Codex** — as a plugin in either, or a local gitignored
symlink. Plus a pinned Python package (**`rootcause-runtime`**, the `lib` helpers brain scripts
import). No `rootcause-light` source needed.

A *brain* is `rootcause-org/rootcause-brain-<project>`: markdown skills + Python grounding scripts that
do `from lib import db` to read a customer's data read-only. In prod those run in a workspace
container; this kit reproduces that loop on a laptop with the **same `lib`** and the **same per-project
env**.

## Use it against a brain

Three install paths, one skill — pick by agent. All run read-only; none commit anything to the brain
or reach `/brain` (see below). After installing, `cd` into the brain (which needs its gitignored
`./.env`) and invoke the **brain-dev** skill, or call the engine directly.

**A — Local, per-repo, gitignored (recommended; works with any agent).** One pinned clone on disk;
the skill is symlinked into the brain's gitignored `.agents/skills/brain-dev` (Codex auto-discovers) +
`.claude/skills/brain-dev` (Claude Code). Nothing committed.

```bash
cd ~/code/rootcause-org/rootcause-brain-<project>
bash <(curl -fsSL https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/v0.1.0/install.sh)
# then, from the brain root — the engine ships inside the skill:
SKILL="${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}/skills/brain-dev"
uv run "$SKILL/scripts/brain_run.py" --brief        # map the brain
uv run "$SKILL/scripts/brain_test.py" --live        # run the tiers (read-only prod)
```

`install.sh` clones the kit once to `~/.rootcause-brain-skills` (override with `RC_BRAIN_KIT` /
`RC_BRAIN_KIT_TAG`), symlinks the skill in, and appends the ignore rules. To update: re-run it.

**B — Claude Code plugin (user scope).**

```text
/plugin marketplace add rootcause-org/rootcause-brain-skills
/plugin install brain-dev                    # later: /plugin marketplace update
# engine then lives at ${CLAUDE_PLUGIN_ROOT}/skills/brain-dev/scripts/
```

**C — Codex plugin (user scope).**

```bash
codex plugin marketplace add rootcause-org/rootcause-brain-skills
codex plugin install brain-dev               # later: codex plugin marketplace upgrade
```

Full walkthrough: [docs/onboarding.md](docs/onboarding.md).

## Why nothing lands in the brain repo

The skill is **install-once, run-from-inside-any-brain** — deliberately *not* copied into each brain
(that copy-drift is what this repo kills). It also must stay out of a real run: prod mounts the brain
read-only at `/brain` and the agent treats everything there as knowledge, so a dev/test harness under
the brain would pollute runs.

Both are guaranteed for free: prod builds `/brain` with `git worktree --detach HEAD` — a checkout of
the brain's **committed** `main`. So **anything untracked or gitignored in the brain never reaches
`/brain`.** The plugin installs (B, C) add nothing to the brain at all; the local install (A) is
gitignored. No rootcause-light mirror/strip tricks are needed — only *committed* files travel, and you
never commit the kit. (The one thing that legitimately lives in a brain is project-specific **test
fixtures**, under `skills/<name>/`.)

## The two modes

- **`uv` (inner loop)** — fast; reproduces the import surface, per-project env, read-only DB grounding,
  and the pytest tiers. Does **not** reproduce egress allowlist / `:ro` mounts / container isolation /
  the exact pinned dep set. *A green uv run is not a guaranteed-green prod run.*
- **`docker` (pre-push gate)** — `docker run` the published workspace image, brain + mirrors `:ro`,
  prod isolation. The honest "does it work in the box?" check. (Egress is left open by default and the
  runner says so.)

## What's here

| Path | What |
|---|---|
| `skills/brain-dev/SKILL.md` | The skill: brief → run a grounding script / test tiers → report, in `uv` or `docker` mode. |
| `skills/brain-dev/scripts/brain_env.py` · `brain_run.py` · `brain_test.py` | The engine, inside the skill — shared core + run one script + the pytest tiers; both modes, brain-dir-relative. |
| `runtime/` | The **`rootcause-runtime`** package (`lib/`: db, stripe, cloudwatch, fs, http, html, livecheck). Canonical home. |
| `docker/Dockerfile` | The workspace image (installs `rootcause-runtime`); published to ghcr for `docker` mode. |
| `.claude-plugin/marketplace.json`, `plugin.json` | Claude Code plugin catalog + manifest. |
| `.agents/plugins/marketplace.json`, `.codex-plugin/plugin.json` | Codex plugin catalog + manifest. |
| `docs/migration-rootcause-light.md` | Ordered runbook to cut prod over to the package + published image. |

## Single version line

The plugin versions, the `rootcause-runtime` pin, the workspace image tag, and rootcause-light's prod
Dockerfile pin **move together** so local and prod can't diverge — one bump point, see
[RELEASING.md](RELEASING.md). Current line: **`v0.1.0`**.

- `lib` dependency (brain scripts + CI):
  `rootcause-runtime @ git+https://github.com/rootcause-org/rootcause-brain-skills@v0.1.0#subdirectory=runtime`
  — **always pin a tag, never float `main`** (a push would silently break green local tests).
- workspace image: `ghcr.io/rootcause-org/workspace:v0.1.0`.

## Develop on the kit itself

```bash
cd runtime && uv run --with . --with pytest --no-project pytest tests -q   # package unit tests
```
