---
name: brain-git-sync
description: "Safely reconcile and publish a brain repository's local Git work with origin/main. Use for `$brain dev: git sync`, `$brain-git-sync`, cross-computer Git synchronization, local/remote main divergence, or before brain-publish; do not use brain-publish for a pure Git-sync request."
---

# Brain Git Sync

Make `origin/main` the cross-computer rendezvous without losing local or remote work. The bundled
script owns deterministic fetch, merge, bounded push-retry, and ancestry verification. The agent owns
intent, secret hygiene, conflict resolution, and repository-specific tests.

## Workflow

1. Read the repository's `AGENTS.md` and commit rules. Inventory before mutation:
   ```bash
   git status --short --branch
   git diff --stat && git diff
   git diff --cached --stat && git diff --cached
   git ls-files --others --exclude-standard
   ```
   Inspect untracked files by content/name as appropriate. Never expose secret values in output.

2. Decide which local changes are intended repository work. Include source files such as `.go`; never
   discard them because they are inconvenient. Exclude secrets, caches, build output, dumps, and
   unrelated work. Preserve unrelated work in place. Stage intended paths explicitly and choose one
   repository-appropriate commit message. Stop only when committing safely would require guessing
   ownership or exposing/deleting data.

3. Locate this installed skill and run its primitive from the repository root:
   ```bash
   GIT_SYNC_SKILL="/absolute/path/to/skills/brain-git-sync"
   uv run --no-project python "$GIT_SYNC_SKILL/scripts/brain_git_sync.py" \
     --repo "$PWD" --commit-message '<message>' --max-push-attempts 4 \
     --verify-command '<focused test command>'
   ```
   Omit `--commit-message` when nothing is staged. Unstaged or untracked files deliberately left in
   place cause a safe stop; report them rather than hiding them. Repeat `--verify-command` for each
   relevant check: the script reruns them after every merge and before push, so merged trees are not
   published untested. Do not substitute hand-written fetch/pull/push logic.

4. If the script exits `3`, resolve every conflict with repository context. Preserve both sides'
   intent; never choose ours/theirs wholesale merely to finish. Stage each resolved path, run focused
   tests, then rerun the same script. It resumes a clean merge and re-enters the bounded race loop.
   Read [recovery.md](references/recovery.md) for conflict and blocker recovery.

5. Finish only on script exit `0`. Report its final SHA, local and remote commits integrated, files
   committed, and any files intentionally left unpublished. For machine-readable evidence, rerun or
   invoke with `--json`; require `ancestry_verified: true`.

## Invariants

- Require branch `main`, an `origin`, and a freshly fetched `origin/main`.
- Merge; never rebase divergent work. Fast-forward only when cleanly behind.
- Never reset, force-push, destructively check out, or use global stash.
- Never report success until local `main` equals freshly fetched `origin/main` and all observed local
  and remote commits remain ancestors of the final SHA.
