---
name: brain-dev
description: Iterate on and verify a rootcause project's BRAIN locally — run its grounding scripts (skills/*/scripts/*.py, which do `from lib import db`) and its pytest tiers the way the production workspace container does, without rootcause-light source. Use when working inside a `rootcause-brain-<project>` checkout to test a grounding script, debug a `from lib import db` import, run the offline/live test tiers, reproduce a run's database grounding read-only, or check a brain change before pushing. Two modes: fast `uv` (inner loop) and faithful `docker` (pre-push gate).
---

# brain-dev — run a brain locally, the way prod does

A **brain** (`rootcause-brain-<project>`) is markdown skills + Python grounding scripts that
`from lib import db` to read a customer's data read-only. In prod those scripts run inside a workspace
container; this skill reproduces that loop **on the laptop**, with the same `lib` (the pinned
`rootcause-runtime` package) and the same per-project env — so you can iterate without a real run and
without any `rootcause-light` source.

**Brain-dir-relative and zero-config.** You `cd` into a brain checkout and invoke; everything operates
on `.` — it reads `./.env`, `./skills/*/scripts/`, `./skills` for tests. No `accounts.yml`, no project
name, no `code_root`. The engine ships *inside this skill* (`scripts/`), **never copied into the brain**.

**Read-only, no side effects** — exactly like a real run. It never writes the brain, never posts a
callback, never touches our host. Grounding queries run in a `READ ONLY` Postgres transaction.

## The two modes (fidelity vs. speed)

| Mode | What it is | Use |
|---|---|---|
| **`uv`** (default) | `uv run` with the bundled `lib` + its pinned deps, env from `./.env`. | Tight inner loop while writing/fixing a script. |
| **`docker`** | `docker run` the published workspace image — brain + mirrors `:ro`, prod isolation. | "Does it actually work in the box?" before pushing. |

> **uv-mode fidelity gap — surface it, don't over-trust it.** uv mode reproduces the import surface,
> the per-project env, read-only DB grounding, and the pytest tiers. It does **NOT** reproduce the
> egress allowlist (you have open internet locally — a call that passes here can be `EGRESS_BLOCKED`
> in prod), the `:ro` mounts (`EROFS`), container isolation, or the exact pinned dep set. **A green
> `uv` run is not a guaranteed-green prod run.** The runner prints this caveat on every uv run; repeat
> it when you report a uv-mode result. The honest pre-push gate is `--mode docker`.

## Locate the engine

The engine ships **inside this skill**, in `scripts/` next to this `SKILL.md`:
`brain_env.py` · `brain_run.py` · `brain_test.py`. This is the same on every install path (Claude
Code plugin, Codex plugin, local symlink) and in both agents — no `${CLAUDE_PLUGIN_ROOT}`, no clone
path to track.

Set `SKILL` to the directory you loaded this `SKILL.md` from, then call the scripts under it:

```bash
SKILL=<the absolute directory of this SKILL.md>   # e.g. …/skills/brain-dev
```

All commands below use `"$SKILL/scripts/…"`. (`lib` is resolved automatically — from the kit's sibling
`runtime/` when present, else the tag-pinned `rootcause-runtime` git spec; override with `RC_RUNTIME_SPEC`.)

## Workflow

1. **Brief — map the brain first.** Don't guess at script paths or DB names:
   ```bash
   uv run "$SKILL/scripts/brain_run.py" --brief
   ```
   Lists the `.env` key names (values redacted), the project databases (`*_DSN`), the mirrors the
   runner can see, and each skill + its scripts. Also read the brain's `AGENTS.md` and the relevant
   `skills/<name>/SKILL.md` for intent.

2. **Run a grounding script** (everything after the path passes through to the script):
   ```bash
   uv run "$SKILL/scripts/brain_run.py" skills/databases/scripts/lookup_customer.py --email a@b.com
   uv run "$SKILL/scripts/brain_run.py" -m lib.db --list          # ad-hoc DB query CLI
   uv run "$SKILL/scripts/brain_run.py" -m lib.db "select count(*) from accounts"
   ```

3. **Run the test tiers:**
   ```bash
   uv run "$SKILL/scripts/brain_test.py"                 # offline L1 (hermetic, no DSN)
   uv run "$SKILL/scripts/brain_test.py" --live          # + L2 schema canary + L3 render-smoke (read-only prod)
   uv run "$SKILL/scripts/brain_test.py" --require-live   # gated: error if no live test ran
   ```

4. **Pre-push gate — re-run in docker** once it's green in uv:
   ```bash
   uv run "$SKILL/scripts/brain_run.py"  --mode docker skills/databases/scripts/lookup_customer.py --email a@b.com
   uv run "$SKILL/scripts/brain_test.py" --mode docker --live
   ```

5. **Report** the grounded result, the mode used, and — for a uv-mode result — the fidelity caveat.

## Gotchas

- **`import lib.db` preflight.** Both runners hard-fail up front if `lib.db` won't import in the child
  env. This guards the footgun where a brain's `ka.py` wraps `from lib import db` in try/except →
  `db = None`, so a broken import would otherwise fail *silently* at call time. A preflight failure
  means the env/PYTHONPATH is wrong, not that the brain logic is broken.
- **DSN reachability.** uv/docker only reach a DB the laptop can reach. The runner does **not** manage
  tunnels: if a `*_DSN` host is firewalled, open an SSH tunnel and override that `*_DSN` env var. Live
  tests *skip with a printed reason* when the DSN is unreachable (or error under `--require-live`).
- **Mirrors may be absent.** `lib.fs` reads source mirrors at `/mirrors` in prod. Locally, pass
  `--mirrors-root <dir>` (each immediate subdir is a mirror) or `--mirror name=path`; uv mode then
  sets `RC_MIRRORS_ROOT` so `lib.fs` finds them. Without mirrors, `fs` helpers report which mirror is
  missing — degrade gracefully, don't treat it as a brain bug.
- **No `.env`?** uv-offline tests and `--brief` run without one; a script run and the live tier need
  it. Operators recover it with rootcause-light's `rc_env.py <project> --pull`.
- **Run from inside the brain** (cwd), or pass `--brain <dir>` to target another checkout.
