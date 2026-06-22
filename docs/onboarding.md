# Client onboarding — `cd brain → install → run`

From zero to iterating on a brain, with **no `rootcause` source**.

## 1. `cd` into a brain

```bash
cd ~/code/rootcause-org/rootcause-brain-<project>
```

You need the brain's gitignored plaintext **`.env`** at its root (DSNs + API keys). Don't have it?
**Self-serve it with `rc env pull`** (from inside this brain) — it fetches the project's PRODUCTION
grounding `.env` over your `ROOTCAUSE_API_KEY` and writes a `0600 ./.env`, no operator/SSM access
needed (tenant-enabled projects: `rc env pull --tenant <slug>`). See [`rc-cli.md`](rc-cli.md). Operators
can still use the privileged `rc_env.py <project> --pull` (SSM) path.

## 2. Install the kit — pick one path

The `brain-dev` skill is self-contained (engine in its own `scripts/`), so the same skill installs
natively in any of these. All are read-only; none commit anything to the brain or reach `/brain`.

**A — Local, gitignored (recommended; any agent).**
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/v0.1.8/install.sh)
```
Clones the kit once to `~/.rootcause-brain-skills` and symlinks the skill into this brain's gitignored
`.agents/skills/brain-dev` (Codex auto-discovers) + `.claude/skills/brain-dev` (Claude Code, plus the
`/brain-dev` command). Engine then at
`SKILL="${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}/skills/brain-dev"`. Update: re-run the installer
(or, to release + update every local brain at once, the maintainer's standard flow `./refresh-brains.sh`).

**B — Claude Code plugin (user scope).** `/plugin marketplace add rootcause-org/rootcause-brain-skills`
then `/plugin install brain-dev` (update: `/plugin marketplace update`). Engine at
`${CLAUDE_PLUGIN_ROOT}/skills/brain-dev/scripts`.

**C — Codex plugin (user scope).** `codex plugin marketplace add rootcause-org/rootcause-brain-skills`
then `codex plugin install brain-dev` (update: `codex plugin marketplace upgrade`).

> The git clone (and the tag-pinned `rootcause-runtime` spec, if your install can't see a sibling
> `runtime/`) need read access to this repo. Local & CC-plugin installs ship `runtime/` alongside the
> skill, so `uv` mode resolves `lib` offline.

## 3. Run

Invoke the **brain-dev** skill (or `/brain-dev` in Claude Code), or call the engine directly. With the
plugin, the agent knows the skill's path; for path A set `SKILL` to the shared clone's skill dir:

```bash
SKILL="${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}/skills/brain-dev"   # path A
# (CC plugin: SKILL=${CLAUDE_PLUGIN_ROOT}/skills/brain-dev)

uv run "$SKILL/scripts/brain_run.py" --brief                                   # map the brain
uv run "$SKILL/scripts/brain_run.py" skills/databases/scripts/lookup_customer.py --email a@b.com
uv run "$SKILL/scripts/brain_test.py"                                          # offline tier
uv run "$SKILL/scripts/brain_test.py" --live                                  # live tier (read-only prod)

# faithful pre-push gate (same image prod runs):
uv run "$SKILL/scripts/brain_run.py"  --mode docker skills/databases/scripts/lookup_customer.py --email a@b.com
uv run "$SKILL/scripts/brain_test.py" --mode docker --live
```

Docker mode needs a running Docker (colima) and pulls `ghcr.io/rootcause-org/workspace:<tag>`.

## Definition of done

From a brain repo with only `.env` + the installed skill: both `uv` and `docker` modes run a
grounding script and the live test tier read-only against prod, and the `rootcause-runtime` / image
pins match what prod runs.
