# Action/run triage after `rc ask`

Use this when a prod run appears to have booked, moved, deleted, or otherwise changed customer state.
Goal: tell whether the run only drafted text, proposed an action, or an action actually executed.

## First rule

A run never executes an action. It can only propose `reply.actions`. Execution happens later when a
reviewer clicks the confirm link, or when a public/support-gated trigger executes the action.

## Quick checks

```bash
rc run <run_id> --events
rc run <run_id> --brain-diff
```

Read the events around `preflight`, `reply`, and any proposed actions.

| What you see | What it means | Report it as |
|---|---|---|
| Draft claims a mutation, but there is no proposed action | No mutation path existed. Brain/playbook drafted unsafely. | "Draft unsafe; no action was proposed/executed." |
| Action preflight returned `ok:false`, crashed, or was unparseable | Proposal was blocked. No `action_run`; no mutation. | "Preflight blocked proposal; draft must not claim success." |
| `reply.actions` / proposed action exists | Human still has to confirm. No mutation yet. | "Action proposed, pending reviewer confirm." |
| Action status `succeeded` / result note after confirm | Post-loop execution happened. | "Action executed; result says ..." |
| Action status `failed` / error result | Post-loop execution was attempted and failed. | "Action execution failed; hold draft." |
| Public/support dev-trigger output | Dev-trigger executed the action for real, usually runless. | "Dev-trigger executed; not just a run proposal." |

## Optimistic drafts

Email runs may draft optimistically ("I moved/booked/cancelled it") only when the action proposal
survived validation and preflight, producing a confirmable action for the reviewer to run before
sending. If preflight blocks the proposal, the draft is unsafe unless it clearly says manual action is
needed.

## Preflight versus write body

- `preflight.py` runs read-only in the grounding plane, during the LLM run. It predicts whether the
  params are sane and gates whether a proposal can reach a human.
- `script.py` / `script.rb` is the write body. It runs only after confirm/dev-trigger, in the action
  plane, with action credentials or the customer's app credentials.
- For hosted Python actions, `brain_action.py <id> --params ... --preflight-only` reproduces Layer 1 +
  preflight locally. Without `--preflight-only`, it also runs the body in local dry-run mode by default.
- For Embassy/gem actions, there is no local write-body dry run; use a public/support-gated wire check
  or real dev-trigger against a safe target when available.

## Common diagnosis

- "Agent did not propose the action" is usually brain content: action description, playbook altitude, or
  the draft logic after a failed preflight.
- "Preflight failed" is usually read-plane data/schema/permission/param grounding.
- "Execution failed" is usually write-body code, action credentials, tenant scope, or the customer app.

For concepts and authoring details, read `../../docs/actions.md`. For publish/support handoff, use
`../brain-publish/SKILL.md`.
