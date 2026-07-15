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
if ! RC_BRAINS_ROOT="$TMP/brains" "$CHECKOUT/refresh-brains.sh" --release patch --no-image \
    "$TMP/not-a-brain" >"$TMP/release.out" 2>&1; then
  cat "$TMP/release.out" >&2
  exit 1
fi
released="$(git -C "$CHECKOUT" rev-parse HEAD)"
tag="$(git -C "$CHECKOUT" describe --tags --exact-match HEAD)"
test "$(git --git-dir="$REMOTE" rev-parse refs/heads/main)" = "$released"
test "$(git --git-dir="$REMOTE" rev-parse "refs/tags/$tag^{commit}")" = "$released"
grep -q "verified origin/main == HEAD ($released)" "$TMP/release.out"

# --no-push creates the next release locally but leaves both remote refs untouched.
remote_main_before="$(git --git-dir="$REMOTE" rev-parse refs/heads/main)"
if ! RC_BRAINS_ROOT="$TMP/brains" "$CHECKOUT/refresh-brains.sh" --release patch --no-image --no-push \
    "$TMP/not-a-brain" >"$TMP/no-push.out" 2>&1; then
  cat "$TMP/no-push.out" >&2
  exit 1
fi
local_tag="$(git -C "$CHECKOUT" describe --tags --exact-match HEAD)"
test "$(git --git-dir="$REMOTE" rev-parse refs/heads/main)" = "$remote_main_before"
if git --git-dir="$REMOTE" rev-parse "refs/tags/$local_tag" >/dev/null 2>&1; then
  echo "error: --no-push unexpectedly published $local_tag" >&2
  exit 1
fi
grep -q -- "--no-push: release commit/tag remain local" "$TMP/no-push.out"

echo "refresh-brains release tests passed"
