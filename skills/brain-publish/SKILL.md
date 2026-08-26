---
name: brain-publish
description: "Publish, ship, deploy, or promote rootcause brain changes after safely reconciling Git with origin/main. Use for `$brain dev: publish`, `$brain-publish`, making brain edits live, exact-SHA server sync, stable/edge promotion, tenant/project publish, actions, or a RootCause support request. Do not use for a pure `$brain dev: git sync` request; use brain-git-sync."
---

# brain-publish - make a brain change live

Use this as the shared final step after local brain edits from `local-brain-work`, `brain-ask`, `rc-debug`,
`rc-health`, `rc-fleet`, or manual authoring.

Public `rc` exposes the whole project-brain path: status shows the on-box `main` cache, origin
comparison, and resolved channel SHAs; sync fetches `origin/main` and expires warm console workspaces;
promote moves `stable` or `edge` to one exact tested SHA; `publish` does sync → promote → verify in one
guarded call. A project-maintainer OAuth login is enough. Tenant-scoped logins cannot move a shared
project channel, and tenant brains use `main` without channels.

Requires **rc >= 1.16.5** (`publish`/`preflight`, and correct project resolution). Two flags decide
whether a command answers about the right brain — get them wrong and you verify the wrong thing:

- **`--project <project>`, always, explicitly** on every `rc dev brain` call. Older releases
  mis-resolve an implicit project and hit retired flat routes (404); only rc >= 1.16.5 is safe bare.
- **`--scope project` for anything about channels.** In a tenant context (tenant-bound login,
  `--tenant`, or a tenant brain checkout) `rc --project <p> dev brain status` answers about the
  *tenant overlay* brain, not the project channels. Channel verification MUST pass `--scope project`
  and read `.status.channels[]`.

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
   python3 "$LOCAL_SKILL/scripts/brain_lint.py"
   VERIFY_ARGS+=(--verify-command "python3 \"$LOCAL_SKILL/scripts/brain_lint.py\"")
   uv run "$LOCAL_SKILL/scripts/brain_test.py"
   VERIFY_ARGS+=(--verify-command "uv run \"$LOCAL_SKILL/scripts/brain_test.py\"")
   uv run --no-project python "$LOCAL_SKILL/scripts/brain_structure.py"   # structural validation (links, frontmatter, routing; privacy lint scoped to files changed vs origin/main)
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
   concurrent computer pushes.

8. After template/projection edits, eyeball the compiled view for 1–2 representative tenants first —
   `rc dev brain render --project <project> --tenant <slug> --sha "$SHA"` (rc >= 1.17.0) prints the
   projection exactly as `/brain` mounts it; preflight below only says pass/fail. Then run the
   promote-time canary. It dry-runs the promotion and reports which
   tenants' projections the candidate commit would break, without touching any channel:
   ```bash
   rc dev brain preflight --project <project> --scope project --sha "$SHA" --channel stable -o json
   ```
   `--channel` defaults to `stable`. Fix or accept every reported degradation before promoting.

9. **Before every `stable` publish, reconcile an actively used `edge`.** This is a consistency gate,
   not a reason to invent a canary pause after stable was already authorized:
   - Read project-channel status and run the candidate's `edge` preflight. A positive
     `canary.checked` means at least one tenant currently consumes `edge`; zero means no edge consumer
     needs alignment.
   - If active `edge` already resolves to `$SHA`, continue to `stable`.
   - If active `edge` is an ancestor of `$SHA`, publish the exact candidate to `edge` first. Observe it
     before `stable` when the change risk warrants a canary interval; when an immediate stable release
     was explicitly requested, the same verified SHA may follow immediately.
   - If `edge` is ahead of `$SHA`, do not downgrade it. If the histories diverge or ancestry cannot be
     proved, do not overwrite it. Continue only as authorized and report the exact channel SHAs and
     the human decision still needed.
   - After publishing `stable`, read status again. Never finish while stable is ahead of an actively
     used, safely fast-forwardable edge: align edge to the stable SHA and verify both channels. A
     concurrent/divergent edge remains untouched and must be called out as the next action.

10. **Preferred: one-shot publish.** `rc dev brain publish` does sync → promote → verify against one
   exact SHA in a single guarded call, exits non-zero on any mismatch, and prints a receipt with
   `-o json`:
   ```bash
   rc dev brain publish --project <project> --scope project --channel stable --sha "$SHA" -o json
   ```
   Substitute `edge` only when that is the intended project channel. Apply the stable consistency gate
   above whenever both channels are in use. Never omit `--sha`, derive it from ambient remote state,
   or publish a tenant brain. A non-zero exit is a failed publish — do not report the change live.

   <details>
   <summary>Fallback: the manual sync + promote choreography (what <code>publish</code> automates)</summary>

   Use this when `publish` is unavailable (older rc) or when a step needs to be diagnosed separately.

   ```bash
   rc dev brain sync --project <project>
   rc dev brain status --project <project> --scope project -o json
   ```
   Confirm the status reports `origin/main` and the on-box `main` cache at `$SHA`. A `main` state of
   `current` does **not** prove a channel-backed project is live; always inspect the channel entries.

   Then promote that exact SHA with a project-level maintainer login:
   ```bash
   rc dev brain promote --project <project> --scope project --channel stable --sha "$SHA" -o json
   test "$(rc dev brain status --project <project> --scope project -o json | jq -r '.status.channels[] | select(.channel == "stable") | .resolved_sha')" = "$SHA"
   ```
   The result reports `project`, `channel`, `old_sha`, `new_sha`, `changed`, and `idempotent`; retrying
   the same request is safe. Treat an unknown/unreachable SHA, unsafe channel, push failure,
   tenant-scoped denial, or wrong-project denial as a failed publish.
   </details>

11. Prove the intended ref, not merely a successful command:
   - In `rc dev brain status --project <project> --scope project -o json`, select `.status.channels[]` by `channel` and confirm
     `resolved_sha` is exactly `$SHA`; inspect `origin_sha`, `main_sha`, `matches_origin`,
     `matches_main`, `state`, and `provenance` when diagnosing a mismatch.
   - When stronger end-to-end proof is warranted, run a safe `rc ask` **without** `--brain-ref`, then
     inspect `rc run debug <id>` and confirm `brain_resolved` is `channel:<channel> @ <SHA>`.
   - For direct-`main` projects, confirm the on-box and origin `main` SHAs, then use `rc dev console
     bash list` or a normal run as appropriate.

   Do not report the brain change live until channel status or a normal no-`--brain-ref` run proves
   the intended SHA.

12. If `rc dev brain publish` fails, or `rc dev brain sync/status` reports a diverged managed cache or requires manual reconcile even
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
