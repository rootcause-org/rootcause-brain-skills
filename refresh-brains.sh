#!/usr/bin/env bash
# THE standard flow: cut a Brain Dev kit release and refresh every local brain to it.
#
# Why this exists: all local brains symlink ONE shared clone (~/.rootcause-brain-skills) at a pinned
# TAG (see install.sh). The skill ships strictly tag-pinned — every skill/doc change is a release — so
# "refresh all brains" means: bump the single version line (RELEASING.md), commit on main, reconcile
# and verify origin/main through brain-git-sync, then create/push the tag, ensure its workspace image
# exists, and refresh each brain.
#
# Usage:
#   ./refresh-brains.sh                         # refresh-only: re-point shared clone to the newest
#                                               #   existing tag + re-symlink every local brain
#   ./refresh-brains.sh --release patch         # cut vX.Y.(Z+1) from pending changes, then refresh
#   ./refresh-brains.sh --release minor|major   # bump that field instead
#   ./refresh-brains.sh --release 0.2.0         # explicit version
#   ./refresh-brains.sh --release patch --relock  # ALSO regen requirements.lock + REBUILD the image
#                                               #   (use whenever runtime/ dependencies changed)
#   ./refresh-brains.sh --release patch --dry-run # print the whole plan, mutate NOTHING
#   ./refresh-brains.sh --classify              # print runtime_changed=0|1 + digest, no side effects
#
# Flags:
#   --release <patch|minor|major|X.Y.Z>  cut a release before refreshing
#   --relock      regen runtime/requirements.lock and force a full image rebuild (deps changed)
#   --no-image    skip the ghcr image step entirely (uv mode still works; docker mode needs it later)
#   --no-push     commit locally but do NOT sync/tag/push/image (brains can't fetch it yet)
#   --dry-run     show every step without running it
#   --classify    print whether the two newest tags differ in runtime content, then exit
#   [BRAIN_DIR...]  explicit brains to refresh (default: auto-discover siblings)
#
# Prod (rootcause) is a separate public/support-gated step; this script verifies coherence when possible.
set -euo pipefail

# ── repo + tooling ──────────────────────────────────────────────────────────
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -d "$ROOT/skills/local-brain-work" ] || { echo "error: run from the rootcause-brain-skills repo root" >&2; exit 1; }
KIT="${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}"
BRAINS_ROOT="${RC_BRAINS_ROOT:-$(dirname "$ROOT")}"   # where the sibling rootcause-brain-* repos live
IMAGE="ghcr.io/rootcause-org/workspace"

# ── args ────────────────────────────────────────────────────────────────────
RELEASE="" RELOCK=0 NO_IMAGE=0 NO_PUSH=0 DRY=0 CLASSIFY=0; BRAINS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --release) RELEASE="${2:?--release needs patch|minor|major|X.Y.Z}"; shift 2;;
    --relock)   RELOCK=1; shift;;
    --no-image) NO_IMAGE=1; shift;;
    --no-push)  NO_PUSH=1; shift;;
    --dry-run)  DRY=1; shift;;
    --classify) CLASSIFY=1; shift;;
    -h|--help)  sed -n '2,34p' "$0"; exit 0;;
    -*) echo "unknown flag: $1" >&2; exit 1;;
    *)  BRAINS+=("$1"); shift;;
  esac
done

run() { echo "  + $*"; [ "$DRY" = 1 ] || "$@"; }   # echo every mutating step; skip it on --dry-run
say() { echo; echo "▸ $*"; }

# The single content-based digest of runtime/ at a working tree or ref (version literals canonicalized).
runtime_digest() { uv run --no-project python "$ROOT/scripts/runtime_digest.py" "$@" --root "$ROOT"; }

# Side-effect-free classification of the most recent release: do the two newest tags carry different
# committed RUNTIME_DIGEST values? A single tag (or a tag missing the file) reads as changed.
if [ "$CLASSIFY" = 1 ]; then
  latest="$(git -C "$ROOT" tag -l 'v*' | sort -V | tail -1)"
  prev="$(git -C "$ROOT" tag -l 'v*' | sort -V | tail -2 | head -1)"
  latest_d="$(git -C "$ROOT" show "$latest:RUNTIME_DIGEST" 2>/dev/null || true)"
  prev_d="$(git -C "$ROOT" show "$prev:RUNTIME_DIGEST" 2>/dev/null || true)"
  if [ -z "$latest" ] || [ "$latest" = "$prev" ] || [ -z "$latest_d" ] || [ "$latest_d" != "$prev_d" ]; then
    echo "runtime_changed=1"
  else
    echo "runtime_changed=0"
  fi
  echo "digest=$latest_d"
  exit 0
fi

verify_origin_main() {
  local head_sha origin_main_sha
  head_sha="$(git -C "$ROOT" rev-parse HEAD)"
  origin_main_sha="$(git -C "$ROOT" rev-parse refs/remotes/origin/main)"
  if [ "$origin_main_sha" != "$head_sha" ]; then
    echo "error: origin/main is $origin_main_sha, expected release HEAD $head_sha; refusing to push tag" >&2
    exit 1
  fi
  echo "  verified origin/main == HEAD ($head_sha)"
}

# ── 1. release (optional) ───────────────────────────────────────────────────
if [ -n "$RELEASE" ]; then
  BRANCH="$(git -C "$ROOT" symbolic-ref --quiet --short HEAD || true)"
  if [ "$BRANCH" != "main" ]; then
    echo "error: releases must be cut from main (current: ${BRANCH:-detached HEAD})" >&2
    exit 1
  fi

  if [ -n "$(git -C "$ROOT" status --porcelain=v1 --untracked-files=all)" ]; then
    echo "error: release requires a clean main; commit intended work first and leave unrelated WIP out" >&2
    exit 1
  fi

  # A stale computer must observe an already-cut release before choosing the next version.
  say "Reconcile main before choosing the release version"
  run uv run --no-project python "$ROOT/skills/brain-git-sync/scripts/brain_git_sync.py" \
    --repo "$ROOT" --max-push-attempts 4 \
    --verify-command 'SKIP_IMAGE=1 SKIP_PROD=1 ./check-release-coherence.sh'
  CUR="$(grep -E '^VERSION = ' "$ROOT/skills/local-brain-work/scripts/brain_env.py" | sed -E 's/.*"([0-9.]+)".*/\1/')"

  case "$RELEASE" in
    patch|minor|major)
      IFS=. read -r MA MI PA <<<"$CUR"
      case "$RELEASE" in
        patch) PA=$((PA+1));;
        minor) MI=$((MI+1)); PA=0;;
        major) MA=$((MA+1)); MI=0; PA=0;;
      esac
      NEW="$MA.$MI.$PA";;
    [0-9]*.[0-9]*.[0-9]*) NEW="$RELEASE";;
    *) echo "error: --release must be patch|minor|major|X.Y.Z" >&2; exit 1;;
  esac
  say "Release v$CUR → v$NEW"
  git -C "$ROOT" rev-parse "v$NEW" >/dev/null 2>&1 && { echo "error: tag v$NEW already exists" >&2; exit 1; }
  git -C "$ROOT" ls-remote --exit-code --tags origin "refs/tags/v$NEW" >/dev/null 2>&1 && {
    echo "error: remote tag v$NEW already exists; refetch before choosing a version" >&2; exit 1; }

  # 1a. bump every literal RELEASING.md lists (all $CUR hits in these files are OUR version).
  say "Bump version literals"
  FILES=(
    skills/local-brain-work/scripts/brain_env.py
    plugin.json .claude-plugin/marketplace.json
    .codex-plugin/plugin.json .agents/plugins/marketplace.json
    runtime/pyproject.toml install.sh
    README.md docs/onboarding.md docs/migration-rootcause.md
  )
  for f in "${FILES[@]}"; do
    [ -f "$ROOT/$f" ] || continue
    grep -qF "$CUR" "$ROOT/$f" && run sed -i '' "s/${CUR//./\\.}/$NEW/g" "$ROOT/$f" || true
  done

  # 1b. lockfile: only when deps changed (--relock). Skill/doc releases keep lib identical → identical
  #     lock → the image is re-tagged, not rebuilt (see 1d).
  if [ "$RELOCK" = 1 ]; then
    say "Regenerate requirements.lock (deps changed)"
    run uv pip compile "$ROOT/runtime/pyproject.toml" --universal --python-version 3.12 \
        -o "$ROOT/runtime/requirements.lock"
  fi

  # 1b2. Record runtime/'s content digest (version literals canonicalized) into the repo-root
  #      RUNTIME_DIGEST so the release commit itself carries the rebuild-vs-retag fact both sides read.
  #      After --relock so a regenerated lock counts; the file lives outside runtime/ so it never
  #      perturbs its own input.
  say "Record runtime digest"
  DIGEST="$(runtime_digest --worktree)"
  echo "  + RUNTIME_DIGEST=$DIGEST"
  [ "$DRY" = 1 ] || printf '%s\n' "$DIGEST" > "$ROOT/RUNTIME_DIGEST"

  # 1c. local coherence before commit/tag. Image existence is checked after push/build; prod pin drift
  #     is a separate follow-up unless the sibling rootcause checkout is already updated.
  say "Check local release coherence"
  if [ "$NO_IMAGE" = 1 ]; then
    run env SKIP_IMAGE=1 SKIP_PROD=1 "$ROOT/check-release-coherence.sh"
  else
    run env SKIP_IMAGE=1 SKIP_PROD=1 "$ROOT/check-release-coherence.sh"
  fi

  # 1d. One release commit (includes pending skill/doc edits). Do not create even a local release tag
  #     until brain-git-sync has reconciled and verified this commit at origin/main.
  say "Commit release on main"
  run git -C "$ROOT" add -A
  if [ "$DRY" = 1 ]; then git -C "$ROOT" status --short; else
    git -C "$ROOT" commit -q -m "release: v$NEW" \
      -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  fi

  # Classify rebuild-vs-retag ONCE (reused by the image + prod-reminder steps). --relock forces a
  # rebuild; else compare the recorded digest to the prior release's. DIGEST equals HEAD's committed
  # RUNTIME_DIGEST here, so this is the committed-state diff even in --dry-run (no commit). Bootstrap:
  # v$CUR predates RUNTIME_DIGEST → PREV empty → changed once, then steady state.
  RUNTIME_CHANGED=0
  PREV_DIGEST="$(git -C "$ROOT" show "v$CUR:RUNTIME_DIGEST" 2>/dev/null || true)"
  if [ "$RELOCK" = 1 ] || [ "$DIGEST" != "$PREV_DIGEST" ]; then
    RUNTIME_CHANGED=1
  fi

  if [ "$NO_PUSH" = 1 ]; then
    echo "  (--no-push: release commit remains local; no tag created; origin/main is unchanged)"
  else
    say "Reconcile and publish release commit through brain-git-sync"
    run uv run --no-project python "$ROOT/skills/brain-git-sync/scripts/brain_git_sync.py" \
      --repo "$ROOT" --max-push-attempts 4 \
      --verify-command 'SKIP_IMAGE=1 SKIP_PROD=1 ./check-release-coherence.sh'
    run verify_origin_main

    say "Create and publish tag only after verified origin/main"
    if git -C "$ROOT" ls-remote --exit-code --tags origin "refs/tags/v$NEW" >/dev/null 2>&1; then
      echo "error: remote tag v$NEW appeared during release; refusing to replace it" >&2
      exit 1
    fi
    run git -C "$ROOT" tag -a -m "v$NEW" "v$NEW"   # annotated: repo config may force signed tags
    # Include main in the atomic request so a normal concurrent advance rejects tag publication.
    run git -C "$ROOT" -c push.followTags=false push --atomic origin \
      HEAD:refs/heads/main "refs/tags/v$NEW"
  fi

  # 1e. workspace image for the tag. Rebuild only when runtime/ changed; else re-tag the prior image
  #     (byte-identical — the image bakes runtime/ only, never the skill).
  if [ "$NO_IMAGE" = 1 ]; then
    echo "  (--no-image: skipping image; docker mode needs $IMAGE:v$NEW built before pre-push)"
  elif ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "  ⚠ docker unavailable — image NOT built. Before any docker-mode run:" >&2
    echo "      docker build -f docker/Dockerfile -t $IMAGE:v$NEW $ROOT && docker push $IMAGE:v$NEW" >&2
  else
    if [ "$RUNTIME_CHANGED" = 1 ]; then
      say "Build + push image $IMAGE:v$NEW (runtime changed)"
      run docker build -f "$ROOT/docker/Dockerfile" -t "$IMAGE:v$NEW" "$ROOT"
    else
      say "Re-tag image $IMAGE:v$CUR → :v$NEW (runtime identical)"
      docker image inspect "$IMAGE:v$CUR" >/dev/null 2>&1 || run docker pull "$IMAGE:v$CUR"
      run docker tag "$IMAGE:v$CUR" "$IMAGE:v$NEW"
    fi
    [ "$NO_PUSH" = 1 ] || run docker push "$IMAGE:v$NEW"
  fi

  TARGET="v$NEW"
else
  # refresh-only: target the newest existing tag.
  TARGET="$(git -C "$ROOT" tag -l 'v*' | sort -V | tail -1)"
  say "Refresh-only → newest tag $TARGET"
fi

# ── 2. fan out to every local brain (install.sh is the per-brain primitive) ──
if [ -n "$RELEASE" ] && [ "$NO_PUSH" = 1 ] && [ "$DRY" != 1 ]; then
  say "Skip fan-out because --no-push leaves $TARGET unfetchable by the shared clone"
  echo; echo "done — release commit created locally for $TARGET; follow RELEASING.md's manual sync/verify/tag steps"
  exit 0
fi

# Verify the single version line after the release image/tag exists, before local brains fan out.
if [ -n "$RELEASE" ]; then
  say "Check release image coherence"
  if [ "$NO_IMAGE" = 1 ]; then
    run env SKIP_IMAGE=1 SKIP_PROD=1 "$ROOT/check-release-coherence.sh"
  else
    run env SKIP_PROD=1 "$ROOT/check-release-coherence.sh"
  fi
fi
if [ "${#BRAINS[@]}" -eq 0 ]; then
  while IFS= read -r d; do BRAINS+=("$d"); done < <(
    find "$BRAINS_ROOT" -maxdepth 1 -type d -name 'rootcause-brain-*' ! -name '*-skills' | sort
  )
fi
say "Refresh ${#BRAINS[@]} brain(s) to $TARGET (shared clone: $KIT)"
for b in "${BRAINS[@]}"; do
  # accept all brain layouts: project (skills/ | playbooks/ | projection.yaml) + nested tenant
  # (.rootcause.toml — its committed project∪tenant binding; a tenant brain holds only a free-form NL
  # delta + sealed .env now, no tenant.json, so .rootcause.toml is its marker).
  [ -d "$b/skills" ] || [ -d "$b/playbooks" ] || [ -f "$b/projection.yaml" ] || [ -f "$b/.rootcause.toml" ] || {
    echo "  skip $b (no skills/ | playbooks/ | projection.yaml | .rootcause.toml — not a brain)"; continue; }
  echo "  → $(basename "$b")"
  [ "$DRY" = 1 ] || RC_BRAIN_KIT="$KIT" RC_BRAIN_KIT_TAG="$TARGET" "$ROOT/install.sh" "$b" >/dev/null
done

# ── 3. prod (rootcause) — separate, public/support-gated; we only remind ───────
if [ -n "$RELEASE" ]; then
  if [ "$RUNTIME_CHANGED" = 1 ]; then
    say "Prod follow-up (runtime changed; public/support publish step required):"
  cat <<EOF
  cp runtime/requirements.lock ../rootcause/runtime/requirements.lock
  # bump the pin in rootcause/runtime/Dockerfile to @${TARGET}, then follow the RootCause app deploy path
  # see docs/migration-rootcause.md
EOF
  fi
fi
echo; echo "✅ done — brains now run $TARGET"
