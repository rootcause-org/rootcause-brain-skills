# rootcause-brain-skills

One kit to iterate on a project's **brain** locally and verify it works the way production does —
distributed as a **Claude Code plugin** (skill + engine) plus a pinned Python package
(**`rootcause-runtime`**, the `lib` helpers brain scripts import). No `rootcause-light` source needed.

A *brain* is `rootcause-org/rootcause-brain-<project>`: markdown skills + Python grounding scripts that
do `from lib import db` to read a customer's data read-only. In prod those run in a workspace
container; this kit reproduces that loop on a laptop with the **same `lib`** and the **same per-project
env**.

## Use it against a brain

Pick one install path, then `cd` into the brain and go. **Nothing is added to the brain repo** (see
below).

**A. Claude Code plugin — recommended, zero brain footprint.** Installs once at user scope; works in
every brain.

```bash
# in Claude Code
/plugin marketplace add rootcause-org/rootcause-brain-skills
/plugin install rootcause-brain-dev          # later: /plugin marketplace update
```
```bash
cd ~/code/rootcause-org/rootcause-brain-<project>   # needs the brain's gitignored ./.env
# invoke the brain-dev skill, or /brain-dev, or call the engine directly:
KIT=${CLAUDE_PLUGIN_ROOT}/scripts
uv run "$KIT/brain_run.py" --brief                  # map the brain
uv run "$KIT/brain_test.py" --live                  # run the tiers (read-only prod)
```

**B. No plugin — clone the kit once, run the engine directly.** Good for scripted/CI use or non–Claude
Code shells.

```bash
git clone https://github.com/rootcause-org/rootcause-brain-skills ~/.rootcause-brain-skills
cd ~/code/rootcause-org/rootcause-brain-<project>
uv run ~/.rootcause-brain-skills/scripts/brain_run.py --brief
```

**C. Co-located in the brain, but gitignored.** If you want the kit *inside* the brain dir for
convenience, keep it untracked so it can't leak into prod or clutter the repo:

```bash
cd ~/code/rootcause-org/rootcause-brain-<project>
git clone https://github.com/rootcause-org/rootcause-brain-skills dev-kit   # or symlink
echo "dev-kit/" >> .gitignore                                               # never committed
uv run dev-kit/scripts/brain_run.py --brief
```

Full walkthrough: [docs/onboarding.md](docs/onboarding.md).

## Why nothing lands in the brain repo

The skill is **install-once, run-from-inside-any-brain** — deliberately *not* copied into each brain
(that copy-drift is what this repo kills). It also must stay out of a real run: prod mounts the brain
read-only at `/brain` and the agent treats everything there as knowledge, so a dev/test harness under
the brain would pollute runs (SPEC §5).

Both are guaranteed for free: prod builds `/brain` with `git worktree --detach HEAD` — a checkout of
the brain's **committed** `main`. So **anything untracked or gitignored in the brain never reaches
`/brain`.** Options A and B add nothing to the brain at all; option C is gitignored. No rootcause-light
mirror/strip tricks are needed — only *committed* files travel, and you never commit the kit. (The one
thing that legitimately lives in a brain is project-specific **test fixtures**, under
`skills/<name>/`.)

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
| `scripts/brain_env.py` · `brain_run.py` · `brain_test.py` | The engine — shared core + run one script + the pytest tiers; both modes, brain-dir-relative. |
| `runtime/` | The **`rootcause-runtime`** package (`lib/`: db, stripe, cloudwatch, fs, http, html, livecheck). Canonical home. |
| `docker/Dockerfile` | The workspace image (installs `rootcause-runtime`); published to ghcr for `docker` mode. |
| `.claude-plugin/marketplace.json`, `plugin.json` | Plugin catalog + manifest. |
| `docs/migration-rootcause-light.md` | Ordered runbook to cut prod over to the package + published image. |

## Single version line (SPEC §7)

The plugin tag, the `rootcause-runtime` pin, the workspace image tag, and rootcause-light's prod
Dockerfile pin **move together** so local and prod can't diverge. Current line: **`v0.1.0`**.

- `lib` dependency (brain scripts + CI):
  `rootcause-runtime @ git+https://github.com/rootcause-org/rootcause-brain-skills@v0.1.0#subdirectory=runtime`
  — **always pin a tag, never float `main`** (a push would silently break green local tests).
- workspace image: `ghcr.io/rootcause-org/workspace:v0.1.0`.

## Develop on the kit itself

```bash
cd runtime && uv run --with . --with pytest --no-project pytest tests -q   # package unit tests
```
