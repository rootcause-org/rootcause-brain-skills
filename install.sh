#!/usr/bin/env bash
# Install the brain-dev kit LOCALLY into a brain repo — gitignored, never committed.
#
# Why local + gitignored (not a global plugin, not committed):
#   - Prod builds /brain with `git worktree --detach HEAD` (a checkout of committed `main`) and the
#     grounding agent rg/find/ls's across the WHOLE /brain tree. So a committed harness would be
#     run-time pollution; an untracked one can never reach /brain. Gitignored = guaranteed safe.
#   - Committing the kit into each brain re-creates the multi-copy skill-drift this repo kills.
#
# Model: ONE pinned clone on disk, SYMLINKED into each brain's gitignored `.agents/` (+ `.claude/`
# for Claude Code discovery). One source of truth, per-repo discovery, zero /brain footprint.
#
#   curl -fsSL .../install.sh | bash -s -- [BRAIN_DIR]      # or: ./install.sh [BRAIN_DIR]
#   RC_BRAIN_KIT=~/src/kit RC_BRAIN_KIT_TAG=v0.1.5 ./install.sh ~/code/rootcause-org/rootcause-brain-foo
set -euo pipefail

BRAIN="${1:-$PWD}"
BRAIN="$(cd "$BRAIN" && pwd)"
KIT="${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}"
TAG="${RC_BRAIN_KIT_TAG:-v0.1.5}"
REPO="https://github.com/rootcause-org/rootcause-brain-skills"

# Sanity-check this is a brain checkout. Accept both layouts: legacy (skills/) and the newer
# projection-based layout (playbooks/ + projection.yaml).
[ -d "$BRAIN/skills" ] || [ -d "$BRAIN/playbooks" ] || [ -f "$BRAIN/projection.yaml" ] || {
  echo "error: $BRAIN has no skills/ or playbooks/ or projection.yaml — not a brain checkout?" >&2; exit 1; }

# 1. One pinned clone on disk (shared by every brain). Pin the tag, never float main.
if [ -d "$KIT/.git" ]; then
  echo "kit: updating $KIT -> $TAG"
  git -C "$KIT" fetch -q --tags origin || true
  git -C "$KIT" checkout -q "$TAG" || echo "  (tag $TAG not found; using current checkout)" >&2
elif [ -d "$KIT/skills/brain-dev" ]; then
  echo "kit: using existing non-git kit at $KIT"
else
  echo "kit: cloning $REPO@$TAG -> $KIT"
  git clone -q --branch "$TAG" --depth 1 "$REPO" "$KIT"
fi

# 2. Gitignored symlinks into the brain: the SKILL dir at the standard discovery paths —
#    `.agents/skills/brain-dev` (vendor-neutral, Codex auto-discovers) + `.claude/skills/brain-dev`
#    (Claude Code) — plus the `/brain-dev` command. The engine ships INSIDE the skill (scripts/), so the
#    symlink carries it along; the canonical runtime/ stays in the shared clone (resolved via the link).
mkdir -p "$BRAIN/.agents/skills" "$BRAIN/.claude/skills" "$BRAIN/.claude/commands"
ln -sfn "$KIT/skills/brain-dev"      "$BRAIN/.agents/skills/brain-dev"
ln -sfn "$KIT/skills/brain-dev"      "$BRAIN/.claude/skills/brain-dev"
ln -sfn "$KIT/commands/brain-dev.md" "$BRAIN/.claude/commands/brain-dev.md"

# 3. Ignore rules (idempotent). Committing the RULE is fine — it's tiny, documents intent, and blocks
#    an accidental `git add` of the symlinks. They stay untracked → never reach /brain.
GI="$BRAIN/.gitignore"
for rule in "/.agents/skills/brain-dev" "/.claude/skills/brain-dev" "/.claude/commands/brain-dev.md"; do
  grep -qxF "$rule" "$GI" 2>/dev/null || echo "$rule" >> "$GI"
done

echo
echo "installed (gitignored). The engine ships inside the skill:"
echo "  SKILL=$KIT/skills/brain-dev"
echo "  uv run \"\$SKILL/scripts/brain_run.py\" --brief"
echo "  uv run \"\$SKILL/scripts/brain_test.py\" --live"
echo "Claude Code auto-discovers the 'brain-dev' skill + /brain-dev command; Codex auto-discovers .agents/skills."
