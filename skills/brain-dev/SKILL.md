---
name: brain-dev
description: Iterate on and verify a rootcause project's BRAIN locally — run its grounding scripts (skills/*/scripts/*.py, which do `from lib import db`) and its pytest tiers the way the production workspace container does, without rootcause source. Use when working inside a `rootcause-brain-<project>` checkout to test a grounding script, debug a `from lib import db` import, run the offline/live test tiers, reproduce a run's database grounding read-only, or check a brain change before pushing. Two modes: fast `uv` (inner loop) and faithful `docker` (pre-push gate).
---

# brain-dev — run a brain locally, the way prod does

A **brain** (`rootcause-brain-<project>`) is markdown skills + Python grounding scripts that
`from lib import db` to read a customer's data read-only. In prod those scripts run inside a workspace
container; this skill reproduces that loop **on the laptop**, with the same `lib` (the pinned
`rootcause-runtime` package) and the same per-project env — so you can iterate without a real run and
without any `rootcause` source.

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
`brain_env.py` · `brain_run.py` · `brain_test.py` · `brain_action.py` · `brain_dump.py`. This is the
same on every install path (Claude Code plugin, Codex plugin, local symlink) and in both agents — no
`${CLAUDE_PLUGIN_ROOT}`, no clone path to track.

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
   uv run "$SKILL/scripts/brain_test.py"                    # offline L1 (hermetic, no DSN)
   uv run "$SKILL/scripts/brain_test.py" --live             # + L2 schema canary + L3 render-smoke (read-only prod)
   uv run "$SKILL/scripts/brain_test.py" --require-live      # gated: error if no live test ran
   uv run "$SKILL/scripts/brain_test.py" --live --tenant 103 # pin the canary to one tenant (else auto-picked)
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

## Tenant-enabled projects (two brains, channels, a private-DB live-test gap)

A project may serve many **tenants** (e.g. DentAI → dental practices). Two things change for brain-dev:

- **Two brains, two repos.** The **project (shared) brain** (`rootcause-brain-<project>`) holds the
  grounding scripts + shared playbooks a run mounts at `/brain`; each **tenant brain**
  (`rootcause-brain-<project>-<slug>`) holds only that practice's free-form NL delta + sealed `.env`,
  mounted at `/tenant`. (The practice's structured **values** are no longer a file here — they live in the
  rootcause DB settings record, edited via the operator Configuration form / `rc`.) `cd` into whichever
  repo you're editing — this kit is brain-dir-relative either way. The
  grounding scripts + their live tiers live in the **project** brain, so run `brain_test.py` from there.
- **Shared-DB RLS makes grounding tenant-blind.** When one DB holds all tenants keyed by a column, the
  host scopes each run **in the engine** (a per-run login role over filtered views) — so grounding
  scripts carry **no** `tenant_id` filter; they read "the current tenant" implicitly. The live tier's
  canary still wants a real tenant id for typing — auto-picked from `subjects_sql`, or pin it with
  `--tenant <id>` (above).
- **Private-DB live-test limitation — the honest part.** The live tiers need a **laptop-reachable**
  DSN. A tenant DB locked to the box (DentAI's RDS is SG-restricted to the prod box `/32`) is **not**
  reachable from your laptop, so `--live` **skips/fails** there. That is expected and not a brain bug.
  When the DB isn't laptop-reachable, the **faithful test is a real prod run** — `rc ask` (next
  section) or the operator's `rc_agent_run.sh` — which executes the real grounding against the real DB
  under the real RLS scope. Offline (`brain_test.py`, no `--live`) still runs everywhere.

> **Channel trap (shipping, not testing).** A run sources the shared `/brain` at the tenant's pinned
> `project_brain_ref` (default `stable`), **not `main`** — so pushing + syncing a shared-brain change is
> NOT enough; it must be **promoted** `main`→`stable`/`edge`. The one-command outer loop that does
> sync **and** promote is rootcause's `/rc-brain-ship` (see [ship-and-verify.md](ship-and-verify.md)).

## Test a brain change on real prod infra — *without* pushing `main` (`rc ask` + `brain_dump.py`)

uv/docker reproduce one **grounding script**; they do **not** reproduce the LLM loop (two-tool
orchestration, warm-start, the grounding pre-pass, system-prompt assembly, model calls, egress
gateway, KB tenant-scoping). That loop is host code in `rootcause` and — by the AGENTS.md litmus
— is **not** vendored here; running it locally would recreate the lib-drift trap as *loop-drift* (a
green local loop against a stale copy is a false green). So the high-fidelity loop test is to run on
**real prod infra against a pushed `dev/*` branch**, then dump the run here. This needs **no operator
/ SSM access** — only the [`rc` CLI](../../docs/rc-cli.md) and an OAuth token from `rc login`.

> **`rc` auto-targets the brain you're in.** A brain commits a `.rootcause.toml` (project + base_url),
> so `rc` run from inside the checkout hits *this* project — no `--profile`, no env export. One-time:
> `rc login` (OAuth browser sign-in; the token is stored 0600 under `~/.config/rootcause`, no key in any
> file); confirm with `rc whoami`. See
> [rc-cli.md → Auth](../../docs/rc-cli.md#auth--oauth-and-the-brain-checkout-selects-the-project-no---profile-no-env-wrangling).
> If you're using a superadmin/all-projects token instead of the brain's project profile, keep that
> profile explicit and select the project on the request:
> `rc --profile default ask --project <project> [--tenant <slug>] "<question>"`.

```bash
# 1) trigger a run from a customer-style question (against main HEAD):
rc ask "Hi, my account is sophie@coca-cola.com. Do I still have open invoices?"

# …or test a brain change WITHOUT touching main — push a dev branch, run against it:
git push origin dev/refund-rework            # dev branch; main stays live
rc ask "<customer question>" --brain-ref dev/refund-rework

# 2) dump the run to two local files (concise index + jq-queryable event log):
uv run "$SKILL/scripts/brain_dump.py" <run_id>        # → .rootcause/dump/<run8>-<proj>.{md,jsonl}

# 3) progressive disclosure: read the index, then jq into any step (the index prints the queries):
jq -r 'select(.disp=="3").command' .rootcause/dump/<run8>-<proj>.jsonl
```

> `brain_dump.py` writes its local run dumps under `./.rootcause/dump/` (`<run8>-<proj>.{md,jsonl}`)
> in whatever brain repo you run it from — **gitignored, never committed**. All rc/kit local artifacts
> live under the wholesale-ignored `.rootcause/` dir (one `/.rootcause/` rule); every `rootcause-brain-*`
> repo ignores it, and the brain-repo scaffold (`brain-authoring` SKILL) bakes it in for new brains.

`brain_dump.py` shells `rc run <id> --full -o json` (the bundle) → the **shared** `run_dump` renderer
in `rootcause-runtime` → both files. It's the same renderer the operator's `rc run <id> --debug` uses,
so the output is byte-identical regardless of which side dumped the run.

Playbook beats:
- **Side-effect-free.** A `--brain-ref` run posts no callback and pushes no journal; proposed
  actions/PRs are recorded but **flagged `test`** — so "did the agent reach for the action?" (**Mode
  A**) still works against a dev ref. The dump's header echoes `brain_ref` + `trigger=test`, so a test
  run is never mistaken for a live one.
- **Boundary.** Mirrors + KB are at their current cron-synced versions; you're testing *brain*
  changes, not mirror/KB changes.
- **It does NOT replace [ship-and-verify.md](ship-and-verify.md).** That's the path to make a change
  *live* on `main`. This is the path to *gain confidence on real infra first* — the project-dev's Mode
  A without operator/SSM access and without a `main` push.

## Ship it to prod & verify (outer loop)

Done iterating locally and want the change **live on prod with feedback** — push → force a brain sync
(no waiting on the cron) → trigger a real run → read the result? That's a different surface (it drives
host-owned `rootcause` commands, not this read-only local engine). The lean playbook, with the
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
  it. **Project devs self-serve it: `rc env pull`** from inside the brain — fetches that project's
  PRODUCTION grounding `.env` over your `rc login` OAuth token and writes a `0600 ./.env` (no operator/SSM
  access needed; tenant-enabled projects pass `--tenant <slug>`). See
  [`rc-cli.md`](../../docs/rc-cli.md#sync-the-grounding-env-rc-env). Operators can still recover it the
  privileged way with rootcause's `rc_env.py <project> --pull` (SSM).
- **Run from inside the brain** (cwd), or pass `--brain <dir>` to target another checkout.
