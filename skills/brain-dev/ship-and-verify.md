# Ship a brain change to prod & get feedback — the action iteration loop

`brain-dev` (this skill) is the **local, read-only** inner loop. This page is the **outer loop**: you
edited a brain file (typically an **action** under `actions/<id>/`), now you want it live on prod and
want to know *did it do what I expected* — without waiting for the brain-sync cron.

> **Boundary.** Every prod-touching command below lives in and is owned by **`rootcause-light`** (it
> talks to our host: SSM box, Postgres registry, the Prompt API). This page only **sequences** them —
> it ships no host-touching code. Run them from your `rootcause-light` checkout (needs its gitignored
> `accounts.yml`). Canonical docs: [`support/action-runbook.md`], [`commands/rc-sync-brain.md`],
> [`commands/rc-agent-run.md`] in rootcause-light.

## The loop

```
edit  ─▶  push   ─▶  sync prod (+ack)  ─▶  feedback         ─▶  (repeat)
.rb       git      rc_sync_brain.sh      A: does the agent reach for it?  /rc-agent-run
                   STATE adopted-origin  B: does the script work?         rc-action-run
```

`<project>` below = the `projects.name` (e.g. `kampadmin`), **not** the repo name. Commands shown as
`scripts/…` are run from the `rootcause-light` repo root.

---

### Precondition (one-time): is the action plane on for this project?

Actions are off by default. The agent can only **propose** an action, and it can only **execute**,
if the project row carries the plane fields:

```bash
uv run db.py "select name, actions_enabled, action_runner_url from projects where name='<project>'"
```

`actions_enabled=true` + a gem `action_runner_url` + a reverse secret must be set (and the gem host on
the egress allowlist). If not, enable it once per [`action-runbook.md` → "First enable the plane"].
No plane ⇒ a run will never propose your action and there's nothing to execute.

---

### 1 · Edit + verify what you can locally

An action is `actions/<id>/{manifest.yaml,script.rb}` — Ruby, **not** a `from lib import db` grounding
script, so `brain-dev`'s `uv`/`docker` runners don't apply. The honest local checks:

```bash
# syntax, exactly as the gem compiles it (lambda-wrap — see rootcause-action-gem executor.rb)
{ printf 'lambda do |params|\n'; cat actions/<id>/script.rb; printf '\nend\n'; } | ruby -c -
# the gem's own contract specs still pass
cd ~/code/rootcause-org/rootcause-action-gem && bundle exec rspec -q
```

That's as far as the laptop goes; the script only does anything real **inside the customer app**, so
true verification is Mode B below.

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
data. `error` set (or `result.ok=false`) = read it, fix `script.rb`, and go back to step 2 — **and
re-propose** (new digest, see the gotcha in step 2) before re-executing.

---

## One lap, copy-paste

```bash
# from the brain repo
cd ~/code/rootcause-org/rootcause-brain-<project>
{ printf 'lambda do |params|\n'; cat actions/<id>/script.rb; printf '\nend\n'; } | ruby -c -
git pull --rebase origin main && git push origin main

# from the rootcause-light repo
cd ~/code/rootcause-org/rootcause-light
scripts/rc_sync_brain.sh <project>                              # expect: STATE adopted-origin -> <sha>
scripts/rc_agent_run.sh  <project> "<symptom prompt>"          # Mode A: did it propose <id>?
uv run db.py "select id,action_id,params,status from action_runs where status='proposed' order by proposed_at desc limit 3"
# Mode B: execute the proposed id per action-runbook.md, then:
# uv run db.py --format table "select status,result,error from action_runs where id='<action_run_id>'"
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

[`support/action-runbook.md`]: ../../../rootcause-light/.agents/skills/support/action-runbook.md
[`commands/rc-sync-brain.md`]: ../../../rootcause-light/.agents/commands/rc-sync-brain.md
[`commands/rc-agent-run.md`]: ../../../rootcause-light/.agents/commands/rc-agent-run.md
[`action-runbook.md` → "First enable the plane"]: ../../../rootcause-light/.agents/skills/support/action-runbook.md
[rc-action-test]: ../../../rootcause-light/.agents/commands/rc-action-test.md
