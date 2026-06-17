# Client onboarding — `add → install → cd brain → run`

From zero to iterating on a brain, with **no `rootcause-light` source**.

## 1. Add the marketplace + install the plugin (once)

```bash
# in Claude Code
/plugin marketplace add rootcause-org/rootcause-brain-skills
/plugin install rootcause-brain-dev
```

> **Private-repo auth.** While this repo is private, plugin install (and any `uv` git-deps) need your
> git/SSH or a token with read access. Fine for us + a granted pilot; arms-length clients get a public
> marketplace + a real package registry later (SPEC §9).

## 2. `cd` into a brain

```bash
cd ~/code/rootcause-org/rootcause-brain-<project>
```

You need the brain's gitignored plaintext **`.env`** at its root (DSNs + API keys). Don't have it?
Operators recover it with rootcause-light's `rc_env.py <project> --pull`.

## 3. Run

Invoke the **brain-dev** skill (or `/brain-dev`), or call the engine directly:

```bash
KIT=${CLAUDE_PLUGIN_ROOT:-~/.claude/plugins/rootcause-brain-dev}/scripts

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
