# /// script
# requires-python = ">=3.11"
# ///
"""Version-independent content digest of the kit's `runtime/` tree.

`refresh-brains.sh` sed-bumps two version literals in `runtime/pyproject.toml` on every release, so a
raw `git diff -- runtime/` can never tell "runtime code actually changed" from "only the release bump
ran" — which is why the old classifier rebuilt the image on every release. This hashes the git-tracked
bytes of `runtime/` with those two literals canonicalized to `@@VERSION@@` first, so two releases that
differ only by the bump digest identical. The recorded value (repo-root `RUNTIME_DIGEST`) is what both
`refresh-brains.sh` and the host `promote.py` compare — neither recomputes.

    runtime_digest.py <ref>       digest runtime/ at a committed ref (git ls-tree + blob bytes)
    runtime_digest.py --worktree  digest the working tree (release time: HEAD isn't the commit yet)

Guard: only pyproject.toml is canonicalized, so version-independence rests on no OTHER tracked file
under runtime/ carrying the bare version literal (true today because uv.lock is untracked). Track
uv.lock, or add a second literal, and this exits non-zero — the release breaks loudly instead of the
classifier silently reverting to version-dependent.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

RUNTIME = "runtime/"
PYPROJECT = "runtime/pyproject.toml"
PLACEHOLDER = "@@VERSION@@"
VERSION_LINE_RE = re.compile(r'^version = "(\d+\.\d+\.\d+)"', re.MULTILINE)


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True
    ).stdout


def _canonicalize(pyproject: str, version: str) -> str:
    lit = re.escape(version)
    out = re.sub(rf'^version = "{lit}"', f'version = "{PLACEHOLDER}"', pyproject, flags=re.MULTILINE)
    return out.replace(f"@v{version}#subdirectory=runtime", f"@v{PLACEHOLDER}#subdirectory=runtime")


def _digest(paths: list[str], read: Callable[[str], bytes]) -> str:
    if PYPROJECT not in paths:
        sys.exit(f"runtime_digest: {PYPROJECT} is not tracked under {RUNTIME}")
    m = VERSION_LINE_RE.search(read(PYPROJECT).decode("utf-8", "replace"))
    if not m:
        sys.exit('runtime_digest: no `version = "X.Y.Z"` line in runtime/pyproject.toml')
    version = m.group(1)
    h = hashlib.sha256()
    for path in sorted(paths):
        raw = read(path)
        if path == PYPROJECT:
            raw = _canonicalize(raw.decode("utf-8", "replace"), version).encode()
        elif version.encode() in raw:
            sys.exit(
                f"runtime_digest: tracked file {path} carries the version literal {version!r}; "
                "canonicalize or untrack it — the digest must not change on the release bump"
            )
        h.update(path.encode() + b"\0")
        h.update(raw)
        h.update(b"\0")
    return h.hexdigest()


def digest_ref(root: Path, ref: str) -> str:
    names = _git(root, "ls-tree", "-r", "--name-only", ref, "--", RUNTIME).decode().splitlines()
    paths = [p for p in names if p]
    return _digest(paths, lambda p: _git(root, "cat-file", "blob", f"{ref}:{p}"))


def digest_worktree(root: Path) -> str:
    names = _git(root, "ls-files", "--", RUNTIME).decode().splitlines()
    paths = [p for p in names if p]
    return _digest(paths, lambda p: (root / p).read_bytes())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ref", nargs="?", help="git ref to digest (omit with --worktree)")
    ap.add_argument("--worktree", action="store_true", help="digest the working tree instead of a ref")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    if args.worktree == bool(args.ref):
        ap.error("pass exactly one of <ref> or --worktree")
    print(digest_worktree(root) if args.worktree else digest_ref(root, args.ref))


if __name__ == "__main__":
    main()
