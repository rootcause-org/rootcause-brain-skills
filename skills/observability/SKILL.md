---
name: observability
description: Watch and triage your project's rootcause runs from inside the brain — trigger and verify a run, inspect one run by UUID, spot rotting mirrors / dead-letters, find recurring failures, and explain why a thread got no draft. Uses the `rc` CLI over the public API (OAuth token, no operator/SSM access); the brain checkout auto-selects project context, including all-projects default-token fallback. Use when asked "did my brain change work?", "is anything broken?", "what keeps failing?", or "why no reply for this email?".
---

# observability — watch your project's runs with `rc`

The [`rc` CLI](../../docs/rc-cli.md) is your project's self-service window into its own rootcause runs.
Everything here is **read-only over the public `/api/v1`** with your `rc login` OAuth token — **no
operator/SSM access**. **Scope is automatic in a brain checkout:** `rc` uses the project profile if it
exists; otherwise it uses the `default` token and sends this brain's project as `?project=`. That means
a superadmin/all-projects token in `default` can still run plain `rc ask` here. Use explicit `--project`
only outside a brain checkout or to override it. **Every command has `-o json`** for scripting.

One short playbook per question. Start with the one that matches; each links the deeper command doc.

## Trigger & verify a brain change — `rc ask` → `rc run <id> --brain-diff`

The high-fidelity loop test: run the *real* prod loop and read back both the answer and what the run
wrote to the brain.

```bash
rc ask "Hi, my account is sophie@coca-cola.com. Do I still have open invoices?"   # waits; prints run_id
rc run <run_id> --brain-diff       # the journal commit this run wrote (SHA · files · diff)
```

Test a change **without** moving `main`: push a `dev/*` branch and run against it — the run is
side-effect-free (no callback/journal push; proposed actions flagged `test`).

```bash
git push origin dev/refund-rework
rc ask "<customer question>" --brain-ref dev/refund-rework
```

To dump the run's full reasoning/tool-trail too, use the
[`brain-debug`](../brain-debug/SKILL.md) skill / `rc run <id> --debug` (next).

## Triage one run by UUID — `rc run <id> --debug`

Decompose a run into a concise markdown **index** + a jq-able **JSONL** event log, then drill only the
step that matters (don't read the JSONL top to bottom):

```bash
rc run <uuid> --debug                              # → rc-debug/<run8>-<project>.{md,jsonl}
jq -r 'select(.disp=="23").command' rc-debug/<f>.jsonl   # full code of step 23
jq -r 'select(.disp=="23").stdout'  rc-debug/<f>.jsonl   # its output / traceback
jq -r 'select(.exit_code != null and .exit_code != 0).disp' rc-debug/<f>.jsonl   # which steps failed
```

Read the `.md` index first — it carries the timeline, flags, the system prompt, and a "Drill down"
block of ready-made jq calls. (For just the high-level shape: `rc run <uuid>`; for the raw per-event
trace inline: `rc run <uuid> --events`.)

## "Is anything rotting?" — `rc health`

Proactive check: stale/failed source **mirrors** (grounding going stale) + **dead-lettered** runs (a
draft that never reached the customer). **Exits non-zero when unhealthy** — drop it in cron/CI.

```bash
rc health                  # mirrors + dead-letters over the last 24h
rc health --hours 72       # widen the dead-letter window
```

## "What fails repeatedly?" — `rc patterns`

Clusters recent failures (failing bash calls + blocked egress) into ranked patterns, so you fix the
systemic one, not a one-off:

```bash
rc patterns                # last 14 days
rc patterns --days 30
```

Each cluster is a candidate brain fix — a missing runbook, a wrong query, a domain to allowlist. Author
the fix from the evidence, then re-verify with `rc ask` (top).

## "Why no draft for this thread?" — `rc thread <id>`

Trace one thread/session on the rootcause side: is there a run? what did the agent do? did our callback
to the email channel land?

```bash
rc thread <thread-or-session-id>
```

It explains the processor-side outcome — a real draft, a guardrail fallback (ran out of budget), a
blocked-egress step, a stalled run, or an answer whose callback delivery was rejected.

## At-a-glance fleet — `rc fleet`

Every recent run for your project, with compact flags, rates, and worst offenders. Hand a flagged UUID
to `rc run <uuid> --debug`.

```bash
rc fleet                   # human digest, 7 days
rc fleet --format agent    # token-lean shortlist + one line per run (for an agent to triage)
rc fleet --kind email --days 14
```

Flags: `ERR×n` bash-fails · `EGR×n` blocked-egress · `$!` cost-spike · `CTX·Nk` context-rot · `GD`
grounding-discarded.

## Related

- [`rc-cli.md`](../../docs/rc-cli.md) — full command reference + OAuth/profile auth.
- [`brain-dev`](../brain-dev/SKILL.md) — iterate a brain *locally* (the read-only counterpart to
  triggering real runs here).
- [`actions.md`](../../docs/actions.md) — the author→verify loop for the one state-changing plane.
