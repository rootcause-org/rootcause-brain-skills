# Client onboarding — `cd brain → install → run`

From zero to iterating on a brain, with **no `rootcause-light` source**.

## 1. `cd` into a brain

```bash
cd ~/code/rootcause-org/rootcause-brain-<project>
```

You need the brain's gitignored plaintext **`.env`** at its root (DSNs + API keys). Don't have it?
Operators recover it with rootcause-light's `rc_env.py <project> --pull`.

## 2. Install the kit locally (gitignored — recommended)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/v0.1.0/install.sh)
```

This clones the kit once to `~/.rootcause-brain-skills` and symlinks the skill into this brain's
gitignored `.agents/skills/brain-dev` (any agent) + `.claude/skills/brain-dev` (Claude Code, plus the
`/brain-dev` command). Nothing is committed; nothing reaches `/brain`.
`KIT="${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}/scripts"`.

> **Alternative:** the Claude Code plugin (user scope) — `/plugin marketplace add
> rootcause-org/rootcause-brain-skills` + `/plugin install rootcause-brain-dev`; then
> `KIT=${CLAUDE_PLUGIN_ROOT}/scripts`.

> **Private-repo auth.** While this repo is private, the clone (and any `uv` git-deps) need your
> git/SSH or a token with read access. Arms-length clients get a public marketplace + a real package
> registry later (SPEC §9).

## 3. Run

Invoke the **brain-dev** skill (or `/brain-dev`), or call the engine directly:

```bash
KIT="${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}/scripts"   # or ${CLAUDE_PLUGIN_ROOT}/scripts for the plugin install

uv run "$KIT/brain_run.py" --brief                                   # map the brain
uv run "$KIT/brain_run.py" skills/databases/scripts/lookup_customer.py --email a@b.com
uv run "$KIT/brain_test.py"                                          # offline tier
uv run "$KIT/brain_test.py" --live                                  # live tier (read-only prod)

# faithful pre-push gate (same image prod runs):
uv run "$KIT/brain_run.py"  --mode docker skills/databases/scripts/lookup_customer.py --email a@b.com
uv run "$KIT/brain_test.py" --mode docker --live
```

Docker mode needs a running Docker (colima) and pulls `ghcr.io/rootcause-org/workspace:<tag>`.

## Definition of done (SPEC §10)

From a brain repo with only `.env` + the installed plugin: both `uv` and `docker` modes run a
grounding script and the live test tier read-only against prod, and the `rootcause-runtime` / image
pins match what prod runs.
