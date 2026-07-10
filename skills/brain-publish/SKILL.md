---
name: brain-publish
description: "Publish, ship, deploy, sync, or promote rootcause brain changes from a brain checkout using public rc/API surfaces. Use when asked to make brain edits live, promote main to stable/edge, sync production to origin, publish tenant/project brain changes, ship actions, or prepare a RootCause support request when no public surface exists."
---

# brain-publish - make a brain change live

Use this as the shared final step after local brain edits from `local-brain-work`, `brain-ask`, `rc-debug`,
`rc-health`, `rc-fleet`, or manual authoring.

Public `rc` exposes the brain-cache step directly: `rc dev brain status` shows the mounted on-box brain
SHA versus `origin/main`, and `rc dev brain sync` fetches `origin/main`, fast-forwards when safe, and
expires warm `rc dev console bash` workspaces. Channel promotion (`main` -> `stable`/`edge`) is still
separate when a project uses pinned brain refs.

## Required Context

Read:

- [docs/brain-model.md](../../docs/brain-model.md)
- [docs/side-effects.md](../../docs/side-effects.md)
- [docs/support-boundary.md](../../docs/support-boundary.md)

Also read [docs/actions.md](../../docs/actions.md) when publishing `actions/<id>/`.

## Workflow

1. Confirm checkout context:
   ```bash
   git status --short --branch
   git rev-parse HEAD
   rc auth status
   ```
   Do not publish uncommitted changes accidentally. If the user wants uncommitted work shipped, commit
   it first in the brain repo.

2. Classify the change plane:
   - shared project brain: `skills/`, `playbooks/`, projection templates, shared action catalog;
   - tenant brain: tenant overlay/free-form instructions;
   - action: `actions/<id>/`, with proposal/execution rules from `docs/actions.md`.

3. Run best-effort local checks appropriate to the change:
   ```bash
   uv run "$SKILL/scripts/brain_test.py"
   uv run "$SKILL/scripts/brain_test.py" --live
   uv run "$SKILL/scripts/brain_projection.py" --tenant <slug>
   uv run "$SKILL/scripts/brain_action.py" <id> --params '<json>' --preflight-only
   ```
   Missing laptop DB/network setup is not a mysterious failure. Say what was skipped and use a prod
   `rc ask` verification when local live checks cannot run.

4. For production-infra confidence without moving live refs, push a dev branch and run:
   ```bash
   git push origin dev/<branch>
   rc ask "<customer-style prompt>" --brain-ref dev/<branch>
   rc ask "<direct investigation>" --scenario raw --brain-ref dev/<branch>
   ```
   Capture run id, status, trace URL, and `rc run brain-diff <id>` when relevant.

5. Refresh and verify the deployed brain cache:
   ```bash
   rc dev brain --help
   rc dev brain status
   rc dev brain sync
   rc dev brain status
   rc dev console bash list
   ```
   Good state: `state: current`, `stale: false`, `local_sha == remote_sha`, and `rc dev console bash
   list` shows the expected script catalog. `rc dev brain sync` is the cache cleanup/invalidation
   command; it also makes the next `rc dev console bash run` mount the refreshed `/brain`.

6. Produce a RootCause support request only for gaps the public surface still cannot do:
   ```text
   Project/brain:
   Tenant, if any:
   Brain repo path:
   Branch/ref:
   Commit SHA:
   Change plane: shared project brain | tenant brain | action
   Requested outcome:
   Verification already run:
   Run ids / trace URLs:
   rc dev brain status/sync output:
   Product gap: channel promote | tenant brain publish | action wiring | manual reconcile
   ```

Requested outcomes should be product-level: "promote shared project brain to stable for tenant `<slug>`",
"publish tenant brain main", "manual reconcile diverged brain cache", or "wire/verify action execution".
Do not list private RootCause commands or infrastructure mechanics.
