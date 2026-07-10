# Action/run triage after `rc ask`

Use this when a prod run appears to have booked, moved, deleted, or otherwise changed customer state.
Goal: tell whether the run only drafted text, proposed an action, or an action actually executed.

## First rule

By default a run only **proposes** `reply.actions`; execution happens later when a reviewer clicks the
confirm link (or `rc dev console action run` fires it). The exception is an **autonomy: auto / policy** action, which a
run can execute **mid-loop** via the `action` tool — a real mutation *during* the run, with no reviewer
confirm. Check the run's autonomy path before assuming "proposed ≠ executed": an `action` tool event plus an
`action_run` whose `approved_by` is `autonomy:auto` or `policy:<digest>` means the write already happened.

## Quick checks

```bash
rc run events <run_id>
rc run brain-diff <run_id>
```

Read the events around `preflight`, `policy`, `action`, `reply`, and any proposed actions.

| What you see | What it means | Report it as |
|---|---|---|
| Draft claims a mutation, but there is no proposed action | No mutation path existed. Brain/playbook drafted unsafely. | "Draft unsafe; no action was proposed/executed." |
| Action preflight returned `ok:false`, crashed, or was unparseable | Proposal was blocked. No `action_run`; no mutation. | "Preflight blocked proposal; draft must not claim success." |
| `reply.actions` / proposed action exists | Human still has to confirm. No mutation yet. | "Action proposed, pending reviewer confirm." |
| `action` tool event + `action_run` `approved_by = autonomy:auto` or `policy:<digest>`, status `succeeded` | The run **auto-executed mid-loop** — a real mutation with no reviewer confirm. Draft is factual. | "Action auto-executed in-run; result says ..." |
| `action` tool event but the tool result says "not executed / propose via reply.actions" | The policy gate **denied** (or the action was human-level) → it escalated to a normal proposal. No mutation yet. | "Auto-run denied/escalated; pending reviewer confirm." |
| Action status `succeeded` / result note after confirm | Post-loop execution happened. | "Action executed; result says ..." |
| Action status `failed` / error result | Execution was attempted and failed (post-confirm OR mid-loop). | "Action execution failed; hold/adapt draft." |
| `rc dev console action run` output | Dev-trigger executed the action for real, usually runless. | "Dev-trigger executed; not just a run proposal." |

## Optimistic drafts

Email runs may draft optimistically ("I moved/booked/cancelled it") only when the action proposal
survived validation and preflight, producing a confirmable action for the reviewer to run before
sending. If preflight blocks the proposal, the draft is unsafe unless it clearly says manual action is
needed. An **auto-executed** (mid-loop) action is different: the write already ran, so its draft is
**factual, not optimistic** — and an `ok:false` mid-loop result should have made the run adapt the draft or
escalate, never claim success.

## Preflight versus write body

- `preflight.py` runs read-only in the grounding plane, during the LLM run. It predicts whether the
  params are sane and gates whether a proposal can reach a human.
- `policy.py` (for `autonomy: policy` actions) runs read-only host-side in a one-shot grounding container.
  It decides, per invocation, whether the action auto-executes mid-loop (`allow`) or escalates to a human
  (`deny`) — fail-closed to `deny`. Reproduce it locally with `brain_action.py <id> --params ... --policy-only`.
- `script.py` / `script.rb` is the write body. It runs only after confirm/dev-trigger, in the action
  plane, with action credentials or the customer's app credentials.
- For hosted Python actions, `brain_action.py <id> --params ... --preflight-only` reproduces Layer 1 +
  preflight locally. Without `--preflight-only`, it also runs the body in local dry-run mode by default.
- For Embassy/gem actions, there is no local write-body dry run; use `rc dev console action preflight`
  or `rc dev console action run` against a safe target when real execution is intended.

## Common diagnosis

- "Agent did not propose the action" is usually brain content: action description, playbook altitude, or
  the draft logic after a failed preflight.
- "Preflight failed" is usually read-plane data/schema/permission/param grounding.
- "Execution failed" is usually write-body code, action credentials, tenant scope, or the customer app.

For concepts and authoring details, read `../../docs/actions.md`. For publish/support handoff, use
`../brain-publish/SKILL.md`.
