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

> **Diagnosis is read-only and runs locally; actions are the one state-changing plane.** A **gem**
> action executes on the project's *own production app* (via the customer Ruby gem), test-triggered
> with `/rc-action-test` — no local or dry run. A **hosted Python** action
> (`actions/<id>/script.py`) DOES run locally, faithfully: `scripts/brain_action.py` mirrors
> `HostedExecutor` (Layer-1 validation → `preflight.py` → the write body fed only the sealed
> `.env.action`), **dry-run by default** (the body rolls back). See
> [the local action loop](#testing-a-hosted-python-action-locally-brain_actionpy) below and
> [docs/actions.md](../../docs/actions.md).

## The two modes (fidelity vs. speed)

| Mode | What it is | Benefit | Use it for |
|---|---|---|---|
| **`uv`** (default) | `uv run` with `lib` + its **lockfile-pinned** deps on **Python 3.12** (the image's interpreter), env from `./.env` only. | Near-zero friction: no Docker/colima daemon, no image pull, warm runs in ~1s. Needs only `uv` — uv fetches the pinned Python, so **no mise/pyenv setup**. | The tight inner loop — writing/fixing a grounding script or chasing an import. Iterate here. |
| **`docker`** | `docker run` the published workspace image — brain + mirrors `:ro`, full prod isolation. | Byte-faithful to the box: same deps, mounts, egress firewall, container isolation. | The honest "does it work in the box?" gate. Run it **once before pushing**. |

**What uv mode now matches prod on:** the import surface — deps are pinned by `runtime/requirements.lock`
(the full transitive closure, the *same* lock the workspace image builds under), and the interpreter is
pinned to Python 3.12. The brain script sees **only** the project's `./.env` (plus the few host vars uv
needs to launch), exactly like prod injects only the project's secrets — so a host-exported key can't
mask a missing `.env` entry into a false green.

> **uv-mode fidelity gap — surface it, don't over-trust it.** What uv mode still does **NOT** reproduce:
> the egress allowlist (you have open internet locally — a call that passes here can be `EGRESS_BLOCKED`
> in prod), the `:ro` mounts (`EROFS`), container isolation, and the OS (it runs on your host — e.g.
> macOS — not the image's Linux; same arm64 arch, but macOS wheels ≠ the manylinux wheels prod
> installs, so native deps and OS behaviour can differ).
> **A green `uv` run is not a guaranteed-green prod run.** The runner prints this caveat on every uv
> run; repeat it when you report a uv-mode result. The honest pre-push gate is `--mode docker`.

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

## Testing a hosted Python action locally (`brain_action.py`)

The one state-changing plane. A **hosted** Python action (`actions/<id>/{manifest.yaml, script.py,
preflight.py?}`) runs locally through `scripts/brain_action.py`, which mirrors prod's `HostedExecutor`
and surfaces the same feedback at the same points — **dry-run by default** (the write body rolls back,
since prod offers no dry run of a write):

```bash
uv run "$SKILL/scripts/brain_action.py" --list                                   # actions in the brain
uv run "$SKILL/scripts/brain_action.py" <id> --params '<json>' --preflight-only  # Layer-1 + preflight only
uv run "$SKILL/scripts/brain_action.py" <id> --params '<json>'                   # + body, DRY-RUN (rollback)
uv run "$SKILL/scripts/brain_action.py" <id> --params '<json>' --commit          # REAL write (safe target only)
```

Three phases, each faithful to prod:

1. **Layer-1 manifest validation** — the same `type`/`format`/`pattern`/`enum`/`required` the host runs
   at propose time. A mis-shaped param fails here; the body never runs.
2. **Preflight** (`preflight.py`, if present) — read-only against the grounding `./.env`, fail-closed
   (`ok:false`/crash/unparseable stops the run), exactly as the host's in-loop Layer-2.
3. **Write body** (`script.py`) — fed the sealed **`./.env.action` ONLY** (never the grounding `.env`),
   via the `RC_ACTION_PARAMS`/`RC_ACTION_RESULT` file contract. So the action container's env isolation
   is reproduced: a read DSN the body needs but that's missing from `.env.action` fails locally just as
   it would in prod. `--commit` writes for real — point `.env.action` at a local/staging DB, never a
   live customer.

> This is a **local faithful reproduction**, not the prod path. Authoring against a real run still goes
> push → `/rc-sync-brain` → `/rc-action-test` (the operator dev-trigger). Full contract +
> credential-plane rules: [docs/actions.md](../../docs/actions.md).

## Ship it to prod & verify (outer loop)

Done iterating locally and want the change **live on prod with feedback** — push → force a brain sync
(no waiting on the cron) → trigger a real run → read the result? That's a different surface (it drives
host-owned `rootcause-light` commands, not this read-only local engine). The lean playbook, with the
action-iteration gotchas (digest re-propose, push-only brain, the two feedback modes), is here:
[ship-and-verify.md](ship-and-verify.md).

## Reaching the database — the `lib` way (no raw DSNs)

A brain script reads the customer DB **only through `lib.db`** — never by reading a `*_DSN` env var
itself or opening its own `psycopg` connection. `lib.db` is the single source of truth: it resolves
the DSN, opens a `READ ONLY` transaction with a `statement_timeout` (so a stray write or runaway
query fails loudly), and parses enum/other arrays psycopg leaves as raw literals. Re-implementing any
of that silently drops those guarantees.

- **Name a database, not a connection string.** Pick the DB with `db=` — a short name (`db="prod"`),
  the exact env-var name (`db="KAMPADMIN_PROD_DSN"`), or a raw DSN. With a single DB configured it can
  be omitted. `db.databases()` (or `python -m lib.db --list`) shows what this run has.
  ```python
  from lib import db
  rows  = db.query("select … where tenant_id = %s and deleted_at is null", [tid], db="prod")
  one   = db.query_one("select … ", [id], db="prod")
  cols  = db.columns("subscriptions", db="prod")        # introspect when the schema's unsure
  ```
- **The DSNs *are* injected — as `*_DSN` env vars — but that's `lib.db`'s business, not the script's.**
  There are no libpq-style `PGHOST`/`PGUSER`/`DATABASE_URL` vars to read (`DATABASE_URL` is the host's
  own store and is deliberately excluded from discovery). So "use the env var" is never the answer;
  the script names a `db=`, `lib.db` maps it to the right `*_DSN`.
- **A thin per-project wrapper is the norm — re-implementing the connection is not.** Brains add a small
  module that *calls* `lib.db` and bakes in project gotchas (tenant scoping, money formatting, short
  `db=` defaults) — e.g. kampadmin's `skills/records/scripts/ka.py` `rows()`/`row_one()`, momentum's
  `scripts/_db.py`. Copy that shape; don't write a new connector.
- **`%s` placeholders + a params list, never f-string SQL** — `lib.db` passes them through to psycopg.

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
