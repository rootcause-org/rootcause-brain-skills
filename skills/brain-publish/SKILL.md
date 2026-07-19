---
name: brain-publish
description: "Publish, ship, deploy, or promote rootcause brain changes after safely reconciling Git with origin/main. Use for `$brain dev: publish`, `$brain-publish`, making brain edits live, exact-SHA server sync, stable/edge promotion, tenant/project publish, actions, or a RootCause support request. Do not use for a pure `$brain dev: git sync` request; use brain-git-sync."
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

1. Define installed skill paths, then classify the repository and change plane:
   ```bash
   PUBLISH_SKILL="/absolute/path/to/skills/brain-publish"
   GIT_SYNC_SKILL="$(cd "$PUBLISH_SKILL/../brain-git-sync" && pwd)"
   LOCAL_SKILL="$(cd "$PUBLISH_SKILL/../local-brain-work" && pwd)"
   ```
   - `rootcause-brain-skills`: kit release, not a production brain;
   - shared project brain: `skills/`, `playbooks/`, projection templates, shared action catalog;
   - tenant brain: tenant overlay/free-form instructions;
   - action: `actions/<id>/`, with proposal/execution rules from `docs/actions.md`.

2. Run the smallest relevant local checks before publishing. Record them as repeatable
   `--verify-command` arguments so any merge tree is retested before push:
   ```bash
   VERIFY_ARGS=()
   uv run "$LOCAL_SKILL/scripts/brain_test.py"
   VERIFY_ARGS+=(--verify-command "uv run \"$LOCAL_SKILL/scripts/brain_test.py\"")
   uv run --no-project python "$LOCAL_SKILL/scripts/brain_structure.py"   # structural validation (links, frontmatter, routing, privacy lint)
   VERIFY_ARGS+=(--verify-command "uv run --no-project python \"$LOCAL_SKILL/scripts/brain_structure.py\"")
   ```
   The example is for a project/tenant brain. For the kit, use its validators and at least
   `--verify-command 'SKIP_IMAGE=1 SKIP_PROD=1 ./check-release-coherence.sh'`. Add live, projection,
   or action preflight checks only when appropriate. Missing laptop DB/network setup is not a
   mysterious failure; name what was skipped and use production validation later.

3. For `rootcause-brain-skills`, run `./refresh-brains.sh --release patch` and stop. The release
   creates the version commit, reuses `brain_git_sync.py` with coherence verification, proves that
   commit at `origin/main`, and only then creates/pushes the tag. Do **not** run the generic Git step
   first: publishing unversioned kit bytes to `main` weakens release coherence. Do **not** run
   `rc dev brain sync` or promote a brain channel for the kit.

   The remaining steps apply only to project/tenant brain repositories.

4. **Mandatory Git precondition:** run the complete sibling
   [`brain-git-sync`](../brain-git-sync/SKILL.md) workflow. Inventory and stage intended work there,
   then execute its exact primitive with JSON evidence. Do not run any `rc dev brain` command before
   it succeeds:
   ```bash
   SYNC_JSON="$(uv run --no-project python "$GIT_SYNC_SKILL/scripts/brain_git_sync.py" \
     --repo "$PWD" --max-push-attempts 4 "${VERIFY_ARGS[@]}" --json)"
   printf '%s\n' "$SYNC_JSON" | jq .
   test "$(printf '%s\n' "$SYNC_JSON" | jq -r '.ancestry_verified')" = true
   SHA="$(printf '%s\n' "$SYNC_JSON" | jq -er '.final_sha')"
   test "$(git rev-parse refs/remotes/origin/main)" = "$SHA"
   ```
   If intended changes need committing, follow `brain-git-sync` and supply `--commit-message`. Never
   derive the publish SHA from the pre-sync `HEAD`, an ambient branch, or an unverified push.

5. Confirm public access:
   ```bash
   rc auth status
   rc auth access
   ```

6. For production-infra confidence without moving live refs, push a dev branch and run:
   ```bash
   git push origin "$SHA":refs/heads/dev/<branch>
   rc ask "<customer-style prompt>" --brain-ref dev/<branch>
   rc ask "<direct investigation>" --scenario raw --brain-ref dev/<branch>
   ```
   Capture run id, status, trace URL, and `rc run brain-diff <id>` when relevant.

7. Immediately before server sync, rerun step 4 with the same verification commands and replace
   `$SHA` from its fresh JSON. This absorbs production-authored journal/consolidation commits and
   concurrent computer pushes. Then sync that already pushed, verified `origin/main` SHA:
   ```bash
   rc dev brain sync
   rc dev brain status -o json
   ```
   Confirm the status reports `origin/main` and the on-box `main` cache at `$SHA`. A `main` state of
   `current` does **not** prove a channel-backed project is live; always inspect the channel entries.

8. For a shared project brain that runs from `stable` or `edge`, promote that exact SHA with a
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

9. Prove the intended ref, not merely a successful command:
   - In `rc dev brain status -o json`, select `.status.channels[]` by `channel` and confirm
     `resolved_sha` is exactly `$SHA`; inspect `origin_sha`, `main_sha`, `matches_origin`,
     `matches_main`, `state`, and `provenance` when diagnosing a mismatch.
   - When stronger end-to-end proof is warranted, run a safe `rc ask` **without** `--brain-ref`, then
     inspect `rc run debug <id>` and confirm `brain_resolved` is `channel:<channel> @ <SHA>`.
   - For direct-`main` projects, confirm the on-box and origin `main` SHAs, then use `rc dev console
     bash list` or a normal run as appropriate.

   Do not report the brain change live until channel status or a normal no-`--brain-ref` run proves
   the intended SHA.

10. If `rc dev brain sync/status` reports a diverged managed cache or requires manual reconcile even
    though Git sync succeeded, stop before promotion and produce a RootCause support request. Also use
    support only for gaps the public surface cannot do:
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
