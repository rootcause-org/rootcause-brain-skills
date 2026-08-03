#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

KIT="$TMP/kit"
mkdir -p "$KIT/skills/alpha" "$KIT/skills/beta"
printf '%s\n' '---' 'name: alpha' 'description: Alpha.' '---' >"$KIT/skills/alpha/SKILL.md"
printf '%s\n' '---' 'name: beta' 'description: Beta.' '---' >"$KIT/skills/beta/SKILL.md"

new_brain() {
  local name="$1"
  BRAIN="$TMP/$name"
  mkdir -p "$BRAIN"
  git init -q -b main "$BRAIN"
  git -C "$BRAIN" config user.name "Installer Test"
  git -C "$BRAIN" config user.email "installer-test@example.com"
  printf '%s\n' 'project = "fixture"' >"$BRAIN/.rootcause.toml"
  printf '%s\n' '# tracked rules stay unchanged' >"$BRAIN/.gitignore"
  git -C "$BRAIN" add .rootcause.toml .gitignore
  git -C "$BRAIN" commit -q -m initial
}

run_install() {
  RC_BRAIN_KIT="$KIT" "$ROOT/install.sh" "$BRAIN"
}

assert_clean_local_install() {
  test -z "$(git -C "$BRAIN" status --short)"
  test "$(cat "$BRAIN/.gitignore")" = "# tracked rules stay unchanged"
  local exclude
  exclude="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path info/exclude)"
  grep -qxF '/.rootcause/' "$exclude"
  grep -qxF '/.agents/skills/alpha' "$exclude"
  grep -qxF '/.agents/skills/beta' "$exclude"
  grep -qxF '/.claude/skills' "$exclude"
}

# Clean install: one Codex-native tree and one relative Claude compatibility alias.
new_brain clean
run_install >"$TMP/clean.out"
test "$(readlink "$BRAIN/.agents/skills/alpha")" = "$KIT/skills/alpha"
test "$(readlink "$BRAIN/.agents/skills/beta")" = "$KIT/skills/beta"
test "$(readlink "$BRAIN/.claude/skills")" = '../.agents/skills'
assert_clean_local_install

# Idempotence keeps the same topology and does not duplicate local excludes.
run_install >"$TMP/idempotent.out"
test "$(readlink "$BRAIN/.claude/skills")" = '../.agents/skills'
exclude="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path info/exclude)"
test "$(grep -c '^/.agents/skills/alpha$' "$exclude")" = 1
assert_clean_local_install

# Previous installer-created per-skill Claude links migrate to the single alias.
new_brain migrate
mkdir -p "$BRAIN/.claude/skills"
ln -s "$KIT/skills/alpha" "$BRAIN/.claude/skills/alpha"
ln -s "$KIT/skills/beta" "$BRAIN/.claude/skills/beta"
run_install >"$TMP/migrate.out"
test "$(readlink "$BRAIN/.claude/skills")" = '../.agents/skills'
assert_clean_local_install

# Unrelated Claude content prevents aliasing but is preserved beside compatibility links.
new_brain fallback
mkdir -p "$BRAIN/.claude/skills/custom"
printf '%s\n' 'user-owned' >"$BRAIN/.claude/skills/custom/SKILL.md"
ln -s "$KIT/skills/alpha" "$BRAIN/.claude/skills/alpha"
run_install >"$TMP/fallback.out"
test -d "$BRAIN/.claude/skills"
test ! -L "$BRAIN/.claude/skills"
test "$(cat "$BRAIN/.claude/skills/custom/SKILL.md")" = 'user-owned'
test "$(readlink "$BRAIN/.claude/skills/alpha")" = "$KIT/skills/alpha"
grep -q 'preserved user-owned .claude/skills directory' "$TMP/fallback.out"
exclude="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path info/exclude)"
grep -qxF '/.agents/skills/alpha' "$exclude"
grep -qxF '/.claude/skills/alpha' "$exclude"
if grep -qxF '/.claude/skills' "$exclude"; then
  echo "error: fallback directory was ignored wholesale" >&2
  exit 1
fi
git -C "$BRAIN" check-ignore -q .agents/skills/alpha
git -C "$BRAIN" check-ignore -q .claude/skills/alpha
if git -C "$BRAIN" check-ignore -q .claude/skills/custom/SKILL.md; then
  echo "error: installer hid unrelated Claude user content" >&2
  exit 1
fi
test "$(cat "$BRAIN/.gitignore")" = "# tracked rules stay unchanged"

# A shipped-name collision is user content: fail closed and preserve it.
new_brain refusal
mkdir -p "$BRAIN/.agents/skills/alpha"
printf '%s\n' 'do not overwrite' >"$BRAIN/.agents/skills/alpha/SKILL.md"
if run_install >"$TMP/refusal.out" 2>&1; then
  echo "error: installer overwrote a user-owned canonical skill" >&2
  exit 1
fi
grep -q 'refusing to overwrite user content' "$TMP/refusal.out"
test "$(cat "$BRAIN/.agents/skills/alpha/SKILL.md")" = 'do not overwrite'

# A foreign Claude symlink is also preserved and refused.
new_brain claude-refusal
mkdir -p "$BRAIN/.claude"
ln -s ../somewhere-else "$BRAIN/.claude/skills"
if run_install >"$TMP/claude-refusal.out" 2>&1; then
  echo "error: installer overwrote a user-managed Claude alias" >&2
  exit 1
fi
grep -q 'user-managed symlink' "$TMP/claude-refusal.out"
test "$(readlink "$BRAIN/.claude/skills")" = '../somewhere-else'

# Retired names are removed only when their target proves installer ownership.
new_brain retired-user-link
mkdir -p "$BRAIN/.agents/skills"
ln -s "$TMP/user-skill" "$BRAIN/.agents/skills/brain-debug"
run_install >"$TMP/retired-user-link.out"
test "$(readlink "$BRAIN/.agents/skills/brain-debug")" = "$TMP/user-skill"

# Alias-to-fallback transitions replace the managed ignore block; new user content stays visible.
new_brain transition
run_install >"$TMP/transition-alias.out"
rm "$BRAIN/.claude/skills"
mkdir -p "$BRAIN/.claude/skills/custom"
printf '%s\n' 'new user content' >"$BRAIN/.claude/skills/custom/SKILL.md"
run_install >"$TMP/transition-fallback.out"
exclude="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path info/exclude)"
if grep -qxF '/.claude/skills' "$exclude"; then
  echo "error: alias ignore survived transition to fallback directory" >&2
  exit 1
fi
if git -C "$BRAIN" check-ignore -q .claude/skills/custom/SKILL.md; then
  echo "error: alias-to-fallback transition hid user content" >&2
  exit 1
fi

# Root-anchored excludes include the brain prefix when the brain is nested in a larger checkout.
REPO="$TMP/nested"
BRAIN="$REPO/tenant"
mkdir -p "$BRAIN"
git init -q -b main "$REPO"
git -C "$REPO" config user.name "Installer Test"
git -C "$REPO" config user.email "installer-test@example.com"
printf '%s\n' 'project = "fixture"' >"$BRAIN/.rootcause.toml"
git -C "$REPO" add tenant/.rootcause.toml
git -C "$REPO" commit -q -m initial
run_install >"$TMP/nested.out"
exclude="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path info/exclude)"
grep -qxF '/tenant/.agents/skills/alpha' "$exclude"
grep -qxF '/tenant/.claude/skills' "$exclude"
test -z "$(git -C "$REPO" status --short)"

# A second nested brain keeps the first brain's independently managed excludes.
FIRST_BRAIN="$BRAIN"
mkdir -p "$FIRST_BRAIN/.rootcause" "$REPO/other"
printf '%s\n' 'sensitive local artifact' >"$FIRST_BRAIN/.rootcause/dump"
printf '%s\n' 'project = "fixture"' >"$REPO/other/.rootcause.toml"
git -C "$REPO" add other/.rootcause.toml
git -C "$REPO" commit -q -m 'add second brain'
BRAIN="$REPO/other"
run_install >"$TMP/nested-other.out"
exclude="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path info/exclude)"
grep -qxF '/tenant/.agents/skills/alpha' "$exclude"
grep -qxF '/tenant/.claude/skills' "$exclude"
grep -qxF '/other/.agents/skills/alpha' "$exclude"
grep -qxF '/other/.claude/skills' "$exclude"
git -C "$REPO" check-ignore -q tenant/.rootcause/dump
git -C "$REPO" check-ignore -q tenant/.agents/skills/alpha
git -C "$REPO" check-ignore -q other/.agents/skills/alpha
test -z "$(git -C "$REPO" status --short)"

echo "install tests passed"
