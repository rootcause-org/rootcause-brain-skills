# Ship a brain change to prod & get feedback — the action iteration loop

`brain-dev` (this skill) is the **local, read-only** inner loop. This page is the **outer loop**: you
edited a brain file (typically an **action** under `actions/<id>/`), now you want it live on prod and
want to know *did it do what I expected* — without waiting for the brain-sync cron.

> **Boundary.** Every prod-touching command below lives in and is owned by **`rootcause`** (it
> talks to our host: SSM box, Postgres registry, the Prompt API). This page only **sequences** them —
> it ships no host-touching code. Run them from your `rootcause` checkout (needs its gitignored
> `accounts.yml`). Canonical docs: [`support/action-runbook.md`], [`commands/rc-sync-brain.md`],
> [`commands/rc-agent-run.md`] in rootcause.

> **For grounding / playbook / tenant changes (not actions), prefer the one command.** rootcause's
> **`/rc-brain-ship <project> [--tenant <slug>] [--ask "<prompt>"]`**
> (`scripts/rc_brain_ship.sh`) runs the whole non-action loop — QA → push → sync (project + tenant) →
> **promote `main`→`stable`/`edge`** → verify — and crucially does the **promote** a tenant-enabled
> project needs (a run reads the tenant's pinned channel, not `main`). This page's manual sequence is
> what it wraps; the **action** flow below (digest re-propose, `/rc-action-test`) is the part
> `/rc-brain-ship` does not cover.

## The loop

```
edit  ─▶  push   ─▶  sync prod (+ack)  ─▶  feedback         ─▶  (repeat)
.rb       git      rc_sync_brain.sh      A: does the agent reach for it?  /rc-agent-run
                   STATE adopted-origin  B: does the script work?         rc-action-run
```

`<project>` below = the `projects.name` (e.g. `kampadmin`), **not** the repo name. Commands shown as
`scripts/…` are run from the `rootcause` repo root.

---

### Precondition: is the action plane fully wired?

Actions are off by default. Without **all four sides** wired, the plane is silently dead (bare 404s):

**Side 1 — box-wide (rootcause `.env`) — ONE-TIME, not per-project:**
`ACTION_TOKEN_KEY` (+ `PUBLIC_BASE_URL`) must be set on the box. Without it the entire
`/api/v1/actions/*` + confirm surface returns 404 — no amount of per-project config fixes it.

**Side 2 — per-project Postgres `projects` row:**
`actions_enabled=true`, `action_mode='gem'`, `action_runner_url` (the customer app's mounted
endpoint, e.g. `https://admin.kampadmin.be/rootcause/action`), `action_reverse_secret`.
Check: `uv run db.py "select name, actions_enabled, action_mode, action_runner_url from projects where name='<project>'"`.

**Side 3 — customer app (the Rails app / gem host):**
Must MOUNT `RootCause::ActionRunner::RackApp` at `/rootcause/action` (the inbound receiver — separate
from `ResultRackApp` at `/rootcause/result`) AND set `ROOTCAUSE_FETCH_URL` to
`https://rootcause.probackup.io/actions/script`. Without the mount: the host's signed
invocation hits a 404. Without `ROOTCAUSE_FETCH_URL`: the gem fetches from a `rootcause.invalid`
placeholder.

**Side 4 — brain:**
The action exists in `actions/<id>/` and has been synced (`rc_sync_brain.sh <project>`).

**Operator shortcut** — `scripts/rc_action_enable.sh <project> --runner-url <url> [--generate-secret]`
sets the per-project row fields and prints the box-key/customer-app checklist. Run once per project.

**Verify the whole pipe before trusting it** — `scripts/rc_action_doctor.sh <project> <action_id> [--params '<json>']`
proves resolve works, the gem mount answers (GET → 405), and a `dry_run` validate-only invocation
returns `would_execute:true` (or a named structured error). Run this before Mode A/B below.

No plane ⇒ a run will never propose your action and there's nothing to execute.

---

### 0 · Ground in real runs first

Before editing, see what the agent *actually did* on real cases with the project's own
[`rc` CLI](../../docs/rc-cli.md): `rc runs --limit 20` (filter `--kind`/`--category`) to find relevant
runs, then `rc run <id> --events` for the full per-event trace (each tool call's command + stdout/stderr). Author the action's `params` +
`description` from that evidence, not a guess. Full loop: [`docs/rc-cli.md`](../../docs/rc-cli.md).

### 1 · Edit + verify what you can locally

An action is `actions/<id>/{manifest.yaml,script.rb}` — Ruby, **not** a `from lib import db` grounding
script, so `brain-dev`'s `uv`/`docker` runners don't apply.

**Read-only input validation (run these locally):**

```bash
# Layer-1 manifest syntax + schema check
{ printf 'lambda do |params|\n'; cat actions/<id>/script.rb; printf '\nend\n'; } | ruby -c -

# preflight (if actions/<id>/preflight.py exists) — read-only, same contract as prod propose time
tools/preflight.sh <id> --params '<json>'
# or directly: uv run "$SKILL/scripts/brain_run.py" actions/<id>/preflight.py --params '<json>'
```

These cover Layer-1 (manifest shape) and Layer-2 (preflight read-only preconditions) locally.

> ⚠️ **The gem's rspec proves nothing about the live wire.** `bundle exec rspec -q` runs against
> **mocks** — it cannot catch host↔gem contract bugs. Three real contract bugs we hit
> (schema shape mismatch, wrong `project_id` on fetch, malformed signed fetch-response) were all
> **invisible to mocked tests** and only surfaced against the live pipe. A green gem rspec does NOT
> prove the wire works. The real pre-flight is `rc_action_doctor.sh` (the validate-only `dry_run`,
> side-effect-free) — plus the wire-contract tests now in both repos.

The WRITE body has no local run for gem actions; that's `rc_action_doctor` dry-run + `rc-action-test`.

### 2 · Push the brain

The brain is **push-only**: runs fast-forward `main` with their own journal commits and `git push`.
So a human push that's behind origin is **rejected** — absorb the run commits first:

```bash
cd ~/code/rootcause-org/rootcause-brain-<project>
git pull --rebase origin main      # absorb any "run <id>: journal" commits
git push origin main
```

> **Changing `script.rb` mints a new digest** (`sha256(script.rb)`). A proposal pins the digest *at
> propose time*; the gem refuses to execute a stale one. So on the **agent-propose path** (Mode A→B
> below), **after every script edit you must re-propose** (a fresh run) — you can't re-fire an old
> proposal against new code. (`/rc-action-test` re-pins the current digest on every
> call, so it sidesteps this entirely — see step 4.)

### 3 · Sync prod to your commit + get the acknowledgment

Don't wait for the cron. Force the box's local brain clone to adopt origin:

```bash
scripts/rc_sync_brain.sh <project>
```

The **ack** is the printed `STATE` line — you want:

```
STATE adopted-origin (fast-forward) -> <sha>      # ✅ your pushed sha is now live on the box
```

Other outcomes:
- `STATE up-to-date` — box already had it (your push didn't change `main`, or you synced twice).
- `RUN_IN_FLIGHT <n>` — a run is mid-publish; it **refuses** to avoid racing the commit. Re-run when quiet.
- `STATE diverged (manual reconcile required)` — box-local and origin both moved. Reconcile by hand on
  the box (`aws ssm start-session` → `cd /srv/brain/projects/<project>/brain` → rebase deliberately → push).

Confirm the exact body is live (digest sanity), if you want belt-and-suspenders:

```bash
git -C /srv/brain/projects/<project>/brain show HEAD:actions/<id>/script.rb | shasum -a 256
```

### 4 · Get feedback (two modes — use both)

> **✅ One-shot harness collapses Mode B.** `/rc-action-test`
> (`scripts/rc_action_test.sh <project> <id> --params '{…}' [--sync]`, [command doc][rc-action-test])
> does sync → **resolve (digest ack)** → **execute by `action_id`+params** → ✅/❌ in one call —
> *runless*, no Gmail confirm, and it **re-pins the current digest each call** so the re-propose dance
> goes away for dev-triggering. It's the fast path for **Mode B** below; use **Mode A** when the
> question is whether the agent *reaches for* the action. Concept + the author→test loop:
> [`docs/actions.md`](../../docs/actions.md).

**Mode A — does the agent reach for the action, with the right params?** Trigger a real prod run from
a symptom prompt and read what it proposed:

```bash
scripts/rc_agent_run.sh <project> "<a prompt that should trigger the action>"
# or the slash command: /rc-agent-run <project> <prompt…>
```

Relay the **trace URL** (every step) + the **draft**. Then check it actually proposed *your* action:

```bash
uv run db.py "select id, action_id, params, intent, status from action_runs where status='proposed' order by proposed_at desc limit 5"
```

You're looking for `action_id=<id>` with sane `params`. (A run **proposes**; it never executes. If it
*should* have proposed but didn't, that's a brain-content/altitude problem — fix the action's
`description` or the surrounding skill, not the script.)

> **No operator/SSM access?** The `db.py` queries above are rootcause operator tools. A project
> dev does the **entire** Mode A without them — and **without a `main` push**: `rc ask "<symptom>"
> --brain-ref dev/x` triggers the real prod loop against a pushed dev branch (side-effect-free, action
> flagged `test`), then `brain_dump.py <run_id>` renders the index + jq-queryable trace locally — which
> shows whether (and with what params) the run reached for the action. See the
> [brain-dev test-run loop](SKILL.md#test-a-brain-change-on-real-prod-infra--without-pushing-main-rc-ask--brain_dumppy)
> and [`rc run <id> --events`](../../docs/rc-cli.md) for the operator-free read path.
> With a superadmin/all-projects token in `default`, run this plainly from inside the brain; the checkout
> auto-scopes the request. Outside a brain checkout, select the project explicitly:
> `rc ask --project <project> --brain-ref dev/x "<symptom>"`.
> For an explicit stronger retry, add `--effort pro` or `--effort max`; omit it for normal tier selection.

**Mode B — does the script actually work end-to-end against prod's gem?** Take the `action_runs.id`
from Mode A and execute it headlessly (same confirm→execute POST the reviewer's email button fires):

```bash
# inspect first: the pinned digest, params, intent
uv run db.py --format table "select action_id, script_digest, params, status from action_runs where id='<action_run_id>'"
# execute (mints a single-use token from the host ACTION_TOKEN_KEY, POSTs /actions/<token>)
#   → full recipe in action-runbook.md § "rc-action-run"
# then read the gem's structured outcome:
uv run db.py --format table "select status, result, error from action_runs where id='<action_run_id>'"
```

`status=succeeded` + your `{ ok: true, … }` in `result` = the script did what you expected on real
data. `error` set (or `result.ok=false`) = read it. The host now surfaces the gem's **structured
error**: `error.class` (e.g. `resolve_failed`, `schema_violation`) + `error.message` — so a failed
execute names its cause, not just "HTTP 5xx". Fix `script.rb`, go back to step 2 — **and re-propose**
(new digest, see the gotcha in step 2) before re-executing.

> **Pre-flight alternative (zero side effects):** `scripts/rc_action_doctor.sh <project> <action_id> [--params '<json>']`
> runs a `dry_run` validate-only invocation that returns `would_execute:true` (or a structured error)
> — proves the whole pipe without writing anything. Run this before committing to a real Mode B execute.

---

## One lap, copy-paste

```bash
# from the brain repo
cd ~/code/rootcause-org/rootcause-brain-<project>
{ printf 'lambda do |params|\n'; cat actions/<id>/script.rb; printf '\nend\n'; } | ruby -c -
tools/preflight.sh <id> --params '<json>'                       # (if preflight.py exists)
git pull --rebase origin main && git push origin main

# from the rootcause repo
cd ~/code/rootcause-org/rootcause
scripts/rc_sync_brain.sh <project>                              # expect: STATE adopted-origin -> <sha>
scripts/rc_action_doctor.sh <project> <id> --params '<json>'   # dry_run: proves whole pipe, zero side effects
scripts/rc_agent_run.sh  <project> "<symptom prompt>"          # Mode A: did it propose <id>?
uv run db.py "select id,action_id,params,status from action_runs where status='proposed' order by proposed_at desc limit 3"
# Mode B: execute the proposed id per action-runbook.md, then:
# uv run db.py --format table "select status,result,error from action_runs where id='<action_run_id>'"
# (error.class + error.message now named on failure, not just HTTP status)
```

## Gotchas (high-signal)

- **Digest pinning = re-propose after every script edit** *(agent-propose path)*. The #1 trap today:
  old proposal pins old bytes; the gem refuses. Always: edit → push → sync → *new* run → execute.
  `/rc-action-test` removes this for dev-triggering (it re-pins each call).
- **Push-only brain.** Never force-push. If `git push` is rejected, `git pull --rebase` (you're behind
  on journal commits), don't `--force`.
- **`rc_sync_brain` refuses mid-run** (`RUN_IN_FLIGHT`). That's the guard against corrupting a run's
  commit — wait, don't fight it.
- **A run can't fix `/brain`** (it's mounted `:ro`). Feedback from a run is a *signal*; the fix is
  always: edit on your laptop → this loop.
- **Mode A failure ≠ Mode B failure.** "Agent didn't propose it" is a brain-content problem (the
  action's `description`/altitude); "proposed but execution errored" is a `script.rb` problem. Don't
  confuse them — they live in different files.

[`support/action-runbook.md`]: ../../../rootcause/.agents/skills/support/action-runbook.md
[`commands/rc-sync-brain.md`]: ../../../rootcause/.agents/commands/rc-sync-brain.md
[`commands/rc-agent-run.md`]: ../../../rootcause/.agents/commands/rc-agent-run.md
[`action-runbook.md` → "First enable the plane"]: ../../../rootcause/.agents/skills/support/action-runbook.md
[rc-action-test]: ../../../rootcause/.agents/commands/rc-action-test.md
