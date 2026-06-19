#!/usr/bin/env bash
# THE standard flow: cut a brain-dev release and refresh every local brain to it.
#
# Why this exists: all local brains symlink ONE shared clone (~/.rootcause-brain-skills) at a pinned
# TAG (see install.sh). The skill ships strictly tag-pinned — every skill/doc change is a release — so
# "refresh all brains" means: bump the single version line (RELEASING.md), tag+push, ensure the
# workspace image for that tag exists, then re-point the shared clone and re-symlink each brain.
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
#
# Flags:
#   --release <patch|minor|major|X.Y.Z>  cut a release before refreshing
#   --relock      regen runtime/requirements.lock and force a full image rebuild (deps changed)
#   --no-image    skip the ghcr image step entirely (uv mode still works; docker mode needs it later)
#   --no-push     commit + tag locally but do NOT push to origin (brains can't fetch it yet)
#   --dry-run     show every step without running it
#   [BRAIN_DIR...]  explicit brains to refresh (default: auto-discover siblings)
#
# Prod (rootcause-light) is a SEPARATE, deploy-gated step — this script only PRINTS the follow-up.
set -euo pipefail

# ── repo + tooling ──────────────────────────────────────────────────────────
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -d "$ROOT/skills/brain-dev" ] || { echo "error: run from the rootcause-brain-skills repo root" >&2; exit 1; }
KIT="${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}"
BRAINS_ROOT="${RC_BRAINS_ROOT:-$(dirname "$ROOT")}"   # where the sibling rootcause-brain-* repos live
IMAGE="ghcr.io/rootcause-org/workspace"

# ── args ────────────────────────────────────────────────────────────────────
RELEASE="" RELOCK=0 NO_IMAGE=0 NO_PUSH=0 DRY=0; BRAINS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --release) RELEASE="${2:?--release needs patch|minor|major|X.Y.Z}"; shift 2;;
    --relock)   RELOCK=1; shift;;
    --no-image) NO_IMAGE=1; shift;;
    --no-push)  NO_PUSH=1; shift;;
    --dry-run)  DRY=1; shift;;
    -h|--help)  sed -n '2,33p' "$0"; exit 0;;
    -*) echo "unknown flag: $1" >&2; exit 1;;
    *)  BRAINS+=("$1"); shift;;
  esac
done

run() { echo "  + $*"; [ "$DRY" = 1 ] || "$@"; }   # echo every mutating step; skip it on --dry-run
say() { echo; echo "▸ $*"; }

CUR="$(grep -E '^VERSION = ' "$ROOT/skills/brain-dev/scripts/brain_env.py" | sed -E 's/.*"([0-9.]+)".*/\1/')"

# ── 1. release (optional) ───────────────────────────────────────────────────
if [ -n "$RELEASE" ]; then
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

  # 1a. bump every literal RELEASING.md lists (all $CUR hits in these files are OUR version).
  say "Bump version literals"
  FILES=(
    skills/brain-dev/scripts/brain_env.py
    plugin.json .claude-plugin/marketplace.json
    .codex-plugin/plugin.json .agents/plugins/marketplace.json
    runtime/pyproject.toml install.sh
    README.md docs/onboarding.md docs/migration-rootcause-light.md
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

  # 1c. one release commit (includes your pending skill/doc edits), then tag.
  say "Commit + tag"
  run git -C "$ROOT" add -A
  if [ "$DRY" = 1 ]; then git -C "$ROOT" status --short; else
    git -C "$ROOT" commit -q -m "release: v$NEW" \
      -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  fi
  run git -C "$ROOT" tag "v$NEW"
  [ "$NO_PUSH" = 1 ] || { run git -C "$ROOT" push origin HEAD; run git -C "$ROOT" push origin "v$NEW"; }

  # 1d. workspace image for the tag. Rebuild only when runtime/ changed; else re-tag the prior image
  #     (byte-identical — the image bakes runtime/ only, never the skill).
  if [ "$NO_IMAGE" = 1 ]; then
    echo "  (--no-image: skipping image; docker mode needs $IMAGE:v$NEW built before pre-push)"
  elif ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "  ⚠ docker unavailable — image NOT built. Before any docker-mode run:" >&2
    echo "      docker build -f docker/Dockerfile -t $IMAGE:v$NEW $ROOT && docker push $IMAGE:v$NEW" >&2
  else
    RUNTIME_CHANGED=0
    if [ "$RELOCK" = 1 ] || ! git -C "$ROOT" diff --quiet "v$CUR" HEAD -- runtime/ 2>/dev/null; then
      RUNTIME_CHANGED=1
    fi
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
if [ "${#BRAINS[@]}" -eq 0 ]; then
  while IFS= read -r d; do BRAINS+=("$d"); done < <(
    find "$BRAINS_ROOT" -maxdepth 1 -type d -name 'rootcause-brain-*' ! -name '*-skills' | sort
  )
fi
say "Refresh ${#BRAINS[@]} brain(s) to $TARGET (shared clone: $KIT)"
for b in "${BRAINS[@]}"; do
  [ -d "$b/skills" ] || { echo "  skip $b (no skills/ — not a brain)"; continue; }
  echo "  → $(basename "$b")"
  [ "$DRY" = 1 ] || RC_BRAIN_KIT="$KIT" RC_BRAIN_KIT_TAG="$TARGET" "$ROOT/install.sh" "$b" >/dev/null
done

# ── 3. prod (rootcause-light) — separate, deploy-gated; we only remind ───────
if [ -n "$RELEASE" ] && [ "$RELOCK" = 1 ]; then
  say "Prod follow-up (deps changed → rootcause-light needs the new lib):"
  cat <<EOF
  cp runtime/requirements.lock ../rootcause-light/runtime/requirements.lock
  # bump the pin in rootcause-light/runtime/Dockerfile to @${TARGET}, then deploy (push its stable branch)
  # see docs/migration-rootcause-light.md
EOF
fi
echo; echo "✅ done — brains now run $TARGET"
