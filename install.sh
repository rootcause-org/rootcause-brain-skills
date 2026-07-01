#!/usr/bin/env bash
# Install the Brain Dev kit LOCALLY into a brain repo — gitignored, never committed.
#
# Why local + gitignored (not a global plugin, not committed):
#   - Prod builds /brain with `git worktree --detach HEAD` (a checkout of committed `main`) and the
#     grounding agent rg/find/ls's across the WHOLE /brain tree. So a committed harness would be
#     run-time pollution; an untracked one can never reach /brain. Gitignored = guaranteed safe.
#   - Committing the kit into each brain re-creates the multi-copy skill-drift this repo kills.
#
# Model: ONE pinned clone on disk, SYMLINKED into each brain's gitignored `.agents/skills/` +
# `.claude/skills/` discovery dirs. One source of truth, per-repo discovery, zero /brain footprint.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/main/install.sh) [BRAIN_DIR]
#   RC_BRAIN_KIT=~/src/kit RC_BRAIN_KIT_TAG=v0.1.38 ./install.sh ~/code/rootcause-org/rootcause-brain-foo
#   ./install.sh --latest-version
set -euo pipefail

KIT="${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}"
TAG="${RC_BRAIN_KIT_TAG:-v0.1.38}"
REPO="https://github.com/rootcause-org/rootcause-brain-skills"
LATEST_TAG_ENDPOINT="https://api.github.com/repos/rootcause-org/rootcause-brain-skills/git/matching-refs/tags/v"
KIT_OVERRIDE="${RC_BRAIN_KIT+x}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
usage: install.sh [--tag vX.Y.Z] [BRAIN_DIR]
       install.sh --latest-version

No BRAIN_DIR: auto-detect the current brain checkout from \$PWD or its parents.
Outside a brain checkout, pass BRAIN_DIR explicitly.
EOF
}

latest_tag() {
  ENDPOINT="$LATEST_TAG_ENDPOINT" python3 - <<'PY'
import json
import os
import re
import sys
import urllib.request

url = os.environ["ENDPOINT"]
try:
    with urllib.request.urlopen(url, timeout=10) as response:
        refs = json.load(response)
except Exception as exc:
    print(f"error: could not fetch {url}: {exc}", file=sys.stderr)
    sys.exit(1)

tags = []
for item in refs:
    tag = item.get("ref", "").rsplit("/", 1)[-1]
    if re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        tags.append(tag)

if not tags:
    print(f"error: no semver tags found at {url}", file=sys.stderr)
    sys.exit(1)

def version_key(tag):
    return tuple(int(part) for part in tag[1:].split("."))

print(max(tags, key=version_key))
PY
}

BRAIN_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --latest-version|--print-latest-version)
      latest_tag
      exit 0
      ;;
    --tag)
      TAG="${2:?--tag needs vX.Y.Z}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "error: unknown flag: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [ -n "$BRAIN_ARG" ]; then
        echo "error: expected at most one BRAIN_DIR" >&2
        usage >&2
        exit 1
      fi
      BRAIN_ARG="$1"
      shift
      ;;
  esac
done

is_brain_dir() {
  local dir="$1"
  [ -d "$dir/skills" ] || [ -d "$dir/playbooks" ] || [ -f "$dir/projection.yaml" ] || [ -f "$dir/.rootcause.toml" ]
}

find_brain_dir() {
  local dir
  dir="$(cd "${1:-$PWD}" && pwd)"
  while :; do
    if is_brain_dir "$dir"; then
      echo "$dir"
      return 0
    fi
    [ "$dir" = "/" ] && return 1
    dir="$(dirname "$dir")"
  done
}

if [ -n "$BRAIN_ARG" ]; then
  BRAIN="$(cd "$BRAIN_ARG" && pwd)"
else
  BRAIN="$(find_brain_dir "$PWD" || true)"
fi

if [ -z "${BRAIN:-}" ]; then
  echo "error: not inside a brain checkout; pass BRAIN_DIR explicitly" >&2
  usage >&2
  exit 1
fi

# Sanity-check this is a brain checkout. Accept all layouts: legacy (skills/), the projection-based
# PROJECT layout (playbooks/ + projection.yaml), and a nested TENANT brain — which holds only a free-form
# NL delta + sealed .env now (its values live in the rootcause DB record, no tenant.json), so its marker
# is the committed .rootcause.toml (project∪tenant binding).
is_brain_dir "$BRAIN" || {
  echo "error: $BRAIN has no skills/ or playbooks/ or projection.yaml or .rootcause.toml — not a brain checkout?" >&2; exit 1; }

# 1. One pinned clone on disk (shared by every brain). Pin the shared clone to a tag, never float main.
#    If RC_BRAIN_KIT points at the checkout running this install.sh, treat it as a developer/local
#    override and do not mutate it.
if [ -n "$KIT_OVERRIDE" ] && [ -d "$KIT" ] && [ "$(cd "$KIT" && pwd)" = "$SCRIPT_DIR" ]; then
  [ -d "$KIT/skills" ] || { echo "error: RC_BRAIN_KIT=$KIT has no skills/" >&2; exit 1; }
  echo "kit: using RC_BRAIN_KIT=$KIT"
elif [ -d "$KIT/.git" ]; then
  echo "kit: updating $KIT -> $TAG"
  if ! git -C "$KIT" fetch -q --tags origin; then
    if git -C "$KIT" rev-parse -q --verify "$TAG^{commit}" >/dev/null; then
      echo "  (warning: fetch failed; using already-local $TAG)" >&2
    else
      echo "error: could not fetch tags and $TAG is not available locally" >&2
      exit 1
    fi
  fi
  git -C "$KIT" checkout -q "$TAG" || {
    echo "error: tag $TAG not found in $KIT" >&2
    exit 1
  }
  INSTALLED_TAG="$(git -C "$KIT" describe --tags --exact-match 2>/dev/null || true)"
  if [ "$INSTALLED_TAG" != "$TAG" ]; then
    echo "error: expected $KIT to be at $TAG, got ${INSTALLED_TAG:-non-tag checkout}" >&2
    exit 1
  fi
elif [ -d "$KIT/skills" ]; then
  echo "kit: using existing non-git kit at $KIT"
else
  echo "kit: cloning $REPO@$TAG -> $KIT"
  git clone -q "$REPO" "$KIT"
  git -C "$KIT" checkout -q "$TAG" || {
    echo "error: tag $TAG not found in $KIT" >&2
    exit 1
  }
  INSTALLED_TAG="$(git -C "$KIT" describe --tags --exact-match 2>/dev/null || true)"
  if [ "$INSTALLED_TAG" != "$TAG" ]; then
    echo "error: expected $KIT to be at $TAG, got ${INSTALLED_TAG:-non-tag checkout}" >&2
    exit 1
  fi
fi

# 2. Gitignored symlinks into the brain: every shipped skill at the standard discovery paths.
#    `.agents/skills/<name>` is vendor-neutral and Codex auto-discovers it; `.claude/skills/<name>` is
#    Claude Code's skill discovery path. The engine ships INSIDE the Local Brain Work skill (scripts/), so the
#    symlink carries it along; the canonical runtime/ stays in the shared clone (resolved via the link).
mkdir -p "$BRAIN/.agents/skills" "$BRAIN/.claude/skills"

link_skill() {
  src="$1"
  name="$(basename "$src")"
  for base in "$BRAIN/.agents/skills" "$BRAIN/.claude/skills"; do
    dst="$base/$name"
    if [ -L "$dst" ]; then
      rm "$dst"
    elif [ -e "$dst" ]; then
      echo "error: $dst exists and is not a symlink; refusing to overwrite user content" >&2
      exit 1
    fi
    ln -s "$src" "$dst"
  done
}

for src in "$KIT"/skills/*; do
  [ -d "$src" ] || continue
  [ -f "$src/SKILL.md" ] || continue
  link_skill "$src"
done

# Retired symlinks. Remove only symlinks; leave real user files alone.
for old in \
  "$BRAIN/.claude/commands/brain-dev.md" \
  "$BRAIN/.agents/skills/brain-dev" \
  "$BRAIN/.claude/skills/brain-dev" \
  "$BRAIN/.claude/commands/brain-debug.md" \
  "$BRAIN/.agents/skills/brain-debug" \
  "$BRAIN/.claude/skills/brain-debug" \
  "$BRAIN/.agents/skills/observability" \
  "$BRAIN/.claude/skills/observability" \
  "$BRAIN/.agents/skills/rc-inspect" \
  "$BRAIN/.claude/skills/rc-inspect" \
  "$BRAIN/.agents/skills/rc-thread" \
  "$BRAIN/.claude/skills/rc-thread" \
  "$BRAIN/.agents/skills/rc-run" \
  "$BRAIN/.claude/skills/rc-run"
do
  [ -L "$old" ] && rm "$old"
done

# 3. Ignore rules (idempotent). Committing the RULE is fine — it's tiny, documents intent, and blocks
#    an accidental `git add` of the symlinks. They stay untracked → never reach /brain.
GI="$BRAIN/.gitignore"
# `/.rootcause/` = the wholesale-ignored local-artifact dir: rc CLI debug dumps, brain_dump run dumps,
# and any future rc/kit subfolder. One rule, future-proof — never edit the ignore per new tool again.
grep -qxF "/.rootcause/" "$GI" 2>/dev/null || echo "/.rootcause/" >> "$GI"
for src in "$KIT"/skills/*; do
  [ -d "$src" ] || continue
  [ -f "$src/SKILL.md" ] || continue
  name="$(basename "$src")"
  for rule in "/.agents/skills/$name" "/.claude/skills/$name"; do
  grep -qxF "$rule" "$GI" 2>/dev/null || echo "$rule" >> "$GI"
  done
done

echo
echo "installed skills (gitignored):"
for src in "$KIT"/skills/*; do
  [ -d "$src" ] || continue
  [ -f "$src/SKILL.md" ] || continue
  echo "  $(basename "$src")"
done
echo "The engine ships inside the Local Brain Work skill:"
echo "  SKILL=$KIT/skills/local-brain-work"
echo "  uv run \"\$SKILL/scripts/brain_run.py\" --brief"
echo "  uv run \"\$SKILL/scripts/brain_test.py\" --live"
echo "Claude Code auto-discovers .claude/skills; Codex auto-discovers .agents/skills."
