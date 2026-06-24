#!/usr/bin/env bash
# Install the brain-dev kit LOCALLY into a brain repo — gitignored, never committed.
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
#   curl -fsSL .../install.sh | bash -s -- [BRAIN_DIR]      # or: ./install.sh [BRAIN_DIR]
#   RC_BRAIN_KIT=~/src/kit RC_BRAIN_KIT_TAG=v0.1.13 ./install.sh ~/code/rootcause-org/rootcause-brain-foo
set -euo pipefail

BRAIN="${1:-$PWD}"
BRAIN="$(cd "$BRAIN" && pwd)"
KIT="${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}"
TAG="${RC_BRAIN_KIT_TAG:-v0.1.13}"
REPO="https://github.com/rootcause-org/rootcause-brain-skills"
KIT_OVERRIDE="${RC_BRAIN_KIT+x}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Sanity-check this is a brain checkout. Accept all layouts: legacy (skills/), the projection-based
# PROJECT layout (playbooks/ + projection.yaml), and a nested TENANT brain — which holds only a free-form
# NL delta + sealed .env now (its values live in the rootcause DB record, no tenant.json), so its marker
# is the committed .rootcause.toml (project∪tenant binding).
[ -d "$BRAIN/skills" ] || [ -d "$BRAIN/playbooks" ] || [ -f "$BRAIN/projection.yaml" ] || [ -f "$BRAIN/.rootcause.toml" ] || {
  echo "error: $BRAIN has no skills/ or playbooks/ or projection.yaml or .rootcause.toml — not a brain checkout?" >&2; exit 1; }

# 1. One pinned clone on disk (shared by every brain). Pin the shared clone to a tag, never float main.
#    If RC_BRAIN_KIT points at the checkout running this install.sh, treat it as a developer/local
#    override and do not mutate it.
if [ -n "$KIT_OVERRIDE" ] && [ -d "$KIT" ] && [ "$(cd "$KIT" && pwd)" = "$SCRIPT_DIR" ]; then
  [ -d "$KIT/skills" ] || { echo "error: RC_BRAIN_KIT=$KIT has no skills/" >&2; exit 1; }
  echo "kit: using RC_BRAIN_KIT=$KIT"
elif [ -d "$KIT/.git" ]; then
  echo "kit: updating $KIT -> $TAG"
  git -C "$KIT" fetch -q --tags origin || true
  git -C "$KIT" checkout -q "$TAG" || echo "  (tag $TAG not found; using current checkout)" >&2
elif [ -d "$KIT/skills" ]; then
  echo "kit: using existing non-git kit at $KIT"
else
  echo "kit: cloning $REPO@$TAG -> $KIT"
  git clone -q --branch "$TAG" --depth 1 "$REPO" "$KIT"
fi

# 2. Gitignored symlinks into the brain: every shipped skill at the standard discovery paths.
#    `.agents/skills/<name>` is vendor-neutral and Codex auto-discovers it; `.claude/skills/<name>` is
#    Claude Code's skill discovery path. The engine ships INSIDE the brain-dev skill (scripts/), so the
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

# Retired legacy Claude slash-command symlinks. Remove only symlinks; leave real user files alone.
for old in "$BRAIN/.claude/commands/brain-dev.md" "$BRAIN/.claude/commands/brain-debug.md"; do
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
echo "The engine ships inside the brain-dev skill:"
echo "  SKILL=$KIT/skills/brain-dev"
echo "  uv run \"\$SKILL/scripts/brain_run.py\" --brief"
echo "  uv run \"\$SKILL/scripts/brain_test.py\" --live"
echo "Claude Code auto-discovers .claude/skills; Codex auto-discovers .agents/skills."
