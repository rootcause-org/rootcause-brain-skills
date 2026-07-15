---
name: brain-publish
description: "Publish, ship, deploy, sync, or promote rootcause brain changes from a brain checkout using public rc/API surfaces. Use when asked to make brain edits live, promote main to stable/edge, sync production to origin, publish tenant/project brain changes, ship actions, or prepare a RootCause support request when no public surface exists."
---

# brain-publish - make a brain change live

Use this as the shared final step after local brain edits from `local-brain-work`, `brain-ask`, `rc-debug`,
`rc-health`, `rc-fleet`, or manual authoring.

Public `rc` exposes the whole project-brain path: status shows the on-box `main` cache, origin
comparison, and resolved channel SHAs; sync fetches `origin/main` and expires warm console workspaces;
promote moves `stable` or `edge` to one exact tested SHA. A project-maintainer OAuth login is enough.
Tenant-scoped logins cannot move a shared project channel, and tenant brains use `main` without
channels.

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
   rc auth access
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

5. Push the exact tested commit to project-brain `main`, then sync it into the managed cache:
   ```bash
   SHA="$(git rev-parse HEAD)"
   git push origin "$SHA":main
   rc dev brain sync
   rc dev brain status -o json
   ```
   Confirm the status reports `origin/main` and the on-box `main` cache at `$SHA`. A `main` state of
   `current` does **not** prove a channel-backed project is live; always inspect the channel entries.

6. For a shared project brain that runs from `stable` or `edge`, promote that exact SHA with a
   project-level maintainer login:
   ```bash
   rc dev brain promote --channel stable --sha "$SHA" -o json
   rc dev brain status -o json
   test "$(rc dev brain status -o json | jq -r '.status.channels[] | select(.channel == "stable") | .resolved_sha')" = "$SHA"
   ```
   Substitute `edge` only when that is the intended project channel. Never omit `--sha`, derive it
   from ambient remote state, or promote a tenant brain. The result reports `project`, `channel`,
   `old_sha`, `new_sha`, `changed`, and `idempotent`; retrying the same request is safe. Treat an
   unknown/unreachable SHA, unsafe channel, push failure, tenant-scoped denial, or wrong-project denial
   as a failed publish.

7. Prove the intended ref, not merely a successful command:
   - In `rc dev brain status -o json`, select `.status.channels[]` by `channel` and confirm
     `resolved_sha` is exactly `$SHA`; inspect `origin_sha`, `main_sha`, `matches_origin`,
     `matches_main`, `state`, and `provenance` when diagnosing a mismatch.
   - When stronger end-to-end proof is warranted, run a safe `rc ask` **without** `--brain-ref`, then
     inspect `rc run debug <id>` and confirm `brain_resolved` is `channel:<channel> @ <SHA>`.
   - For direct-`main` projects, confirm the on-box and origin `main` SHAs, then use `rc dev console
     bash list` or a normal run as appropriate.

   Do not report the brain change live until channel status or a normal no-`--brain-ref` run proves
   the intended SHA.

8. Produce a RootCause support request only for gaps the public surface still cannot do:
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
   Product gap: tenant brain publish | action wiring | manual reconcile | project promotion authorization
   ```

Requested outcomes should be product-level: "grant project-maintainer promotion access", "publish
tenant brain main", "manual reconcile diverged brain cache", or "wire/verify action execution". Do
not list private RootCause commands or infrastructure mechanics. Infrastructure/operator promotion is
break-glass only and is outside this external-maintainer skill.
