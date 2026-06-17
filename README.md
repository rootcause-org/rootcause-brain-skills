# rootcause-brain-skills

One kit to iterate on a project's **brain** locally and verify it works the way production does —
distributed as a **Claude Code plugin** (skill + engine) plus a pinned Python package
(**`rootcause-runtime`**, the `lib` helpers brain scripts import). No `rootcause-light` source needed.

A *brain* is `rootcause-org/rootcause-brain-<project>`: markdown skills + Python grounding scripts that
do `from lib import db` to read a customer's data read-only. In prod those run in a workspace
container; this kit reproduces that loop on a laptop with the **same `lib`** and the **same per-project
env**.

## What's here

| Path | What |
|---|---|
| `skills/brain-dev/SKILL.md` | The skill: brief → run a grounding script / test tiers → report, in `uv` or `docker` mode. |
| `scripts/brain_env.py` | Engine core: load `./.env`, PYTHONPATH, `import lib.db` preflight, docker arg builder. |
| `scripts/brain_run.py` | Run one brain script/module — `uv` + `docker` modes. |
| `scripts/brain_test.py` | Run the pytest tiers (offline L1 · live L2 schema · L3 render) — both modes. |
| `runtime/` | The **`rootcause-runtime`** package (`lib/`: db, stripe, cloudwatch, fs, http, html, livecheck). Canonical home. |
| `docker/Dockerfile` | The workspace image (installs `rootcause-runtime`); published to ghcr for `docker` mode. |
| `.claude-plugin/marketplace.json`, `plugin.json` | Plugin catalog + manifest. |

## Install (operator / client)

```bash
# in Claude Code
/plugin marketplace add rootcause-org/rootcause-brain-skills
/plugin install rootcause-brain-dev
# later: /plugin marketplace update
```

Then **`cd` into any brain** and use the skill — see [docs/onboarding.md](docs/onboarding.md).

## The two modes

- **`uv` (inner loop)** — fast; reproduces the import surface, per-project env, read-only DB
  grounding, and the pytest tiers. Does **not** reproduce egress allowlist / `:ro` mounts / container
  isolation / the exact pinned dep set. *A green uv run is not a guaranteed-green prod run.*
- **`docker` (pre-push gate)** — `docker run` the published workspace image, brain + mirrors `:ro`,
  prod isolation. The honest "does it work in the box?" check. (Egress is left open by default and the
  runner says so.)

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
