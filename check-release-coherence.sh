#!/usr/bin/env bash
# Verify the kit's single version line and the optional sibling prod runtime pin.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${RC_WORKSPACE_IMAGE_REPO:-ghcr.io/rootcause-org/workspace}"
ROOTCAUSE_DIR="${ROOTCAUSE_DIR:-$ROOT/../rootcause}"
SKIP_IMAGE="${SKIP_IMAGE:-0}"
SKIP_PROD="${SKIP_PROD:-0}"

version="$(grep -E '^VERSION = ' "$ROOT/skills/local-brain-work/scripts/brain_env.py" | sed -E 's/.*"([0-9.]+)".*/\1/')"
tag="v$version"
fail=0

err() {
  echo "error: $*" >&2
  fail=1
}

require_hit() {
  local file="$1" needle="$2"
  if ! grep -qF "$needle" "$ROOT/$file"; then
    err "$file does not contain $needle"
  fi
}

require_hit "runtime/pyproject.toml" "version = \"$version\""
require_hit "runtime/pyproject.toml" "@$tag#subdirectory=runtime"
require_hit "install.sh" "RC_BRAIN_KIT_TAG:-$tag"
require_hit "README.md" "$tag"
require_hit "README.md" "$IMAGE:$tag"
require_hit "plugin.json" "\"version\": \"$version\""
require_hit ".codex-plugin/plugin.json" "\"version\": \"$version\""
require_hit ".claude-plugin/marketplace.json" "\"version\": \"$version\""
require_hit ".agents/plugins/marketplace.json" "\"ref\": \"$tag\""

if [ "$SKIP_IMAGE" = 1 ]; then
  echo "warning: skipped image manifest check for $IMAGE:$tag" >&2
elif command -v docker >/dev/null 2>&1; then
  if ! docker manifest inspect "$IMAGE:$tag" >/dev/null 2>&1; then
    err "missing image manifest: $IMAGE:$tag"
  fi
else
  err "docker unavailable; cannot verify image manifest $IMAGE:$tag"
fi

if [ "$SKIP_PROD" = 1 ]; then
  echo "warning: skipped sibling prod pin/lock checks" >&2
elif [ -d "$ROOTCAUSE_DIR/runtime" ]; then
  prod_dockerfile="$ROOTCAUSE_DIR/runtime/Dockerfile"
  prod_lock="$ROOTCAUSE_DIR/runtime/requirements.lock"
  if [ -f "$prod_dockerfile" ]; then
    if ! grep -qF "rootcause-brain-skills@$tag#subdirectory=runtime" "$prod_dockerfile"; then
      err "$prod_dockerfile does not pin rootcause-runtime to $tag"
    fi
  fi
  if [ -f "$prod_lock" ] && ! cmp -s "$ROOT/runtime/requirements.lock" "$prod_lock"; then
    err "$prod_lock differs from runtime/requirements.lock"
  fi
else
  echo "warning: sibling rootcause repo not found at $ROOTCAUSE_DIR; skipped prod pin/lock checks" >&2
fi

prev_tag="$(git -C "$ROOT" tag -l 'v*' | grep -v "^$tag$" | sort -V | tail -1 || true)"
if [ -n "$prev_tag" ]; then
  dep_diff="$(git -C "$ROOT" diff "$prev_tag" -- runtime/pyproject.toml \
    | grep -E '^[+-][[:space:]]+"[^"]+==|^[+-]test = ' || true)"
  if [ -n "$dep_diff" ] && git -C "$ROOT" diff --quiet "$prev_tag" -- runtime/requirements.lock; then
    err "runtime dependency edits since $prev_tag require regenerating runtime/requirements.lock"
  fi
fi

if [ "$fail" = 1 ]; then
  exit 1
fi
echo "release coherence ok: $tag"
