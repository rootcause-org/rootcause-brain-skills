#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

REMOTE="$TMP/origin.git"
CHECKOUT="$TMP/kit"
mkdir -p "$TMP/brains"
mkdir -p "$TMP/not-a-brain"
git init -q --bare "$REMOTE"
git clone -q "$ROOT" "$CHECKOUT"
git -C "$CHECKOUT" remote set-url origin "$REMOTE"
git -C "$CHECKOUT" config user.name "Release Test"
git -C "$CHECKOUT" config user.email "release-test@example.com"
git -C "$CHECKOUT" config tag.gpgSign false
git -C "$CHECKOUT" config push.followTags false
git -C "$CHECKOUT" push -q -u origin main
# A publisher may enable this globally. The release flow must still keep the annotated tag out of
# the main push until it has fetched and verified origin/main.
git -C "$CHECKOUT" config push.followTags true
cp "$ROOT/refresh-brains.sh" "$CHECKOUT/refresh-brains.sh"
mkdir -p "$CHECKOUT/skills/brain-git-sync/scripts"
cp "$ROOT/skills/brain-git-sync/scripts/brain_git_sync.py" \
  "$CHECKOUT/skills/brain-git-sync/scripts/brain_git_sync.py"
git -C "$CHECKOUT" add refresh-brains.sh skills/brain-git-sync/scripts/brain_git_sync.py
if ! git -C "$CHECKOUT" diff --cached --quiet; then
  git -C "$CHECKOUT" commit -q -m "test: install release sync primitive"
fi
git -C "$CHECKOUT" -c push.followTags=false push -q origin main

# Reject a version tag unless main already points at the tag's commit. This makes publication order
# observable rather than merely checking that both refs eventually converge.
cat >"$REMOTE/hooks/pre-receive" <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
while read -r _old new ref; do
  case "$ref" in
    refs/tags/v*)
      main="$(git rev-parse refs/heads/main)"
      tagged="$(git rev-parse "$new^{commit}")"
      if [ "$main" != "$tagged" ]; then
        echo "release tag arrived before main" >&2
        exit 1
      fi
      ;;
  esac
done
HOOK
chmod +x "$REMOTE/hooks/pre-receive"

# A release from any other branch must fail before changing files or remote refs.
git -C "$CHECKOUT" switch -q -c release-test
before="$(git -C "$CHECKOUT" rev-parse HEAD)"
if RC_BRAINS_ROOT="$TMP/brains" "$CHECKOUT/refresh-brains.sh" --release patch --no-image \
    "$TMP/not-a-brain" >"$TMP/non-main.out" 2>&1; then
  echo "error: non-main release unexpectedly succeeded" >&2
  exit 1
fi
grep -q "releases must be cut from main" "$TMP/non-main.out"
test "$(git --git-dir="$REMOTE" rev-parse refs/heads/main)" = "$before"
test -z "$(git --git-dir="$REMOTE" tag -l 'v*')"

# A normal publish updates main before exposing the tag, and both resolve to the release commit.
git -C "$CHECKOUT" switch -q main
# Simulate another computer publishing after this checkout's last fetch. The release must merge this
# commit through the same brain-git-sync primitive before it tags anything.
PEER="$TMP/peer"
git clone -q "$REMOTE" "$PEER"
git -C "$PEER" config user.name "Peer Test"
git -C "$PEER" config user.email "peer-test@example.com"
echo "remote peer work" >"$PEER/peer-note.txt"
git -C "$PEER" add peer-note.txt
git -C "$PEER" commit -q -m "peer: concurrent work"
peer_commit="$(git -C "$PEER" rev-parse HEAD)"
git -C "$PEER" push -q origin main
if ! RC_BRAINS_ROOT="$TMP/brains" "$CHECKOUT/refresh-brains.sh" --release patch --no-image \
    "$TMP/not-a-brain" >"$TMP/release.out" 2>&1; then
  cat "$TMP/release.out" >&2
  exit 1
fi
released="$(git -C "$CHECKOUT" rev-parse HEAD)"
tag="$(git -C "$CHECKOUT" describe --tags --exact-match HEAD)"
test "$(git --git-dir="$REMOTE" rev-parse refs/heads/main)" = "$released"
test "$(git --git-dir="$REMOTE" rev-parse "refs/tags/$tag^{commit}")" = "$released"
git -C "$CHECKOUT" merge-base --is-ancestor "$peer_commit" "$released"
test "$(cat "$CHECKOUT/peer-note.txt")" = "remote peer work"
grep -q "verified origin/main == HEAD ($released)" "$TMP/release.out"
grep -q "Reconcile and publish release commit through brain-git-sync" "$TMP/release.out"

# --no-push creates the next release commit locally but creates no tag and leaves remote refs untouched.
remote_main_before="$(git --git-dir="$REMOTE" rev-parse refs/heads/main)"
if ! RC_BRAINS_ROOT="$TMP/brains" "$CHECKOUT/refresh-brains.sh" --release patch --no-image --no-push \
    "$TMP/not-a-brain" >"$TMP/no-push.out" 2>&1; then
  cat "$TMP/no-push.out" >&2
  exit 1
fi
test "$(git --git-dir="$REMOTE" rev-parse refs/heads/main)" = "$remote_main_before"
if git -C "$CHECKOUT" describe --tags --exact-match HEAD >/dev/null 2>&1; then
  echo "error: --no-push unexpectedly created a local release tag" >&2
  exit 1
fi
grep -q -- "--no-push: release commit remains local; no tag created" "$TMP/no-push.out"

# A peer advancing main after the release commit was verified but before tag publication must make
# the atomic main+tag push fail with no remote tag.
RACE_REMOTE="$TMP/race-origin.git"
RACE_CHECKOUT="$TMP/race-kit"
RACE_PEER="$TMP/race-peer"
git clone -q --bare "$REMOTE" "$RACE_REMOTE"
git clone -q "$RACE_REMOTE" "$RACE_CHECKOUT"
git clone -q "$RACE_REMOTE" "$RACE_PEER"
for repo in "$RACE_CHECKOUT" "$RACE_PEER"; do
  git -C "$repo" config user.name "Release Race Test"
  git -C "$repo" config user.email "release-race@example.com"
  git -C "$repo" config tag.gpgSign false
done
cp "$REMOTE/hooks/pre-receive" "$RACE_REMOTE/hooks/pre-receive"
chmod +x "$RACE_REMOTE/hooks/pre-receive"

REAL_GIT="$(command -v git)"
RACE_BIN="$TMP/race-bin"
RACE_COUNT="$TMP/race-ls-remote-count"
mkdir -p "$RACE_BIN"
cat >"$RACE_BIN/git" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *" ls-remote "* && " $* " == *" refs/tags/v"* ]]; then
  count=0
  test ! -f "$RACE_COUNT" || count="$(cat "$RACE_COUNT")"
  count=$((count + 1))
  printf '%s\n' "$count" >"$RACE_COUNT"
  if [ "$count" = 2 ]; then
    "$REAL_GIT" -C "$RACE_PEER" fetch -q origin main
    "$REAL_GIT" -C "$RACE_PEER" merge -q --ff-only origin/main
    printf '%s\n' "peer moved after release verification" >"$RACE_PEER/tag-race.txt"
    "$REAL_GIT" -C "$RACE_PEER" add tag-race.txt
    "$REAL_GIT" -C "$RACE_PEER" commit -q -m "peer: move main before tag"
    "$REAL_GIT" -C "$RACE_PEER" push -q origin main
  fi
fi
exec "$REAL_GIT" "$@"
WRAPPER
chmod +x "$RACE_BIN/git"

race_tag="v0.1.87"
if PATH="$RACE_BIN:$PATH" REAL_GIT="$REAL_GIT" RACE_COUNT="$RACE_COUNT" RACE_PEER="$RACE_PEER" \
    RC_BRAINS_ROOT="$TMP/brains" "$RACE_CHECKOUT/refresh-brains.sh" --release patch --no-image \
    "$TMP/not-a-brain" >"$TMP/tag-race.out" 2>&1; then
  echo "error: release unexpectedly tagged after origin/main moved" >&2
  exit 1
fi
test "$(cat "$RACE_COUNT")" = 2
test "$(git --git-dir="$RACE_REMOTE" show main:tag-race.txt)" = "peer moved after release verification"
if git --git-dir="$RACE_REMOTE" rev-parse "refs/tags/$race_tag" >/dev/null 2>&1; then
  echo "error: atomic release race published $race_tag" >&2
  exit 1
fi

echo "refresh-brains release tests passed"
