"""Read-only filesystem helpers over the mounted source mirrors.

The repo_tree / read_file / search_code trio, but as Python the agent can call from
inside the workspace over the read-only mounts. The v2 mount layout: customer source mirrors at
``/mirrors/<repo>`` and the project's brain at ``/brain`` (both ``:ro`` — a write returns
``EROFS``, kernel-enforced, by design), with ``/tmp`` as the writable scratch. These helpers only
read the mirrors. The host's ``internal/mirror`` package is the canonical Go implementation; this
mirrors its behaviour for convenience inside grounding code.

Containment: a repo name is a single path component and every resolved path must stay under
``/mirrors/<repo>``, so a crafted name or ``..`` can't read outside the mount. The :ro mount
and the container boundary are the real isolation; this is defense-in-depth.
"""

import os
import subprocess

# Prod (and faithful docker mode) mount the source mirrors at ``/mirrors/<repo>``. In fast ``uv``
# mode there is no such mount, so the runner can point this at a local mirror farm via
# ``RC_MIRRORS_ROOT`` (the kit sets it from ``--mirrors-root``). Unset ⇒ ``/mirrors`` exactly as the
# container sees it, so a docker-mode run stays byte-identical to prod. A trailing slash is tolerated.
MIRRORS_ROOT = os.environ.get("RC_MIRRORS_ROOT", "/mirrors").rstrip("/") or "/mirrors"


def _repo_root(repo: str) -> str:
    if not repo or "/" in repo or "\\" in repo or repo in (".", ".."):
        raise ValueError(f"invalid repo name: {repo!r} (must be a single directory component)")
    root = os.path.realpath(os.path.join(MIRRORS_ROOT, repo))
    if root != os.path.join(MIRRORS_ROOT, repo) and not root.startswith(MIRRORS_ROOT + os.sep):
        raise ValueError(f"repo {repo!r} escapes the mirrors root")
    if not os.path.isdir(root):
        raise FileNotFoundError(f"mirror {repo!r} is not mounted")
    return root


def _safe_join(root: str, rel: str) -> str:
    """Join rel under root, rejecting absolute paths and ``..`` escapes (incl. via symlink)."""
    if os.path.isabs(rel):
        raise ValueError(f"absolute paths not allowed: {rel!r}")
    target = os.path.realpath(os.path.join(root, rel))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError(f"path {rel!r} escapes repo root")
    return target


def repo_tree(repo: str, path: str = "", max_entries: int = 2000) -> list[str]:
    """List files/dirs under ``path`` in ``repo`` (recursively), skipping ``.git``.

    Returns repo-relative paths (dirs suffixed with ``/``), capped at ``max_entries``.
    """
    root = _repo_root(repo)
    base = _safe_join(root, path) if path else root
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for d in sorted(dirnames):
            out.append(os.path.relpath(os.path.join(dirpath, d), root) + "/")
            if len(out) >= max_entries:
                return out
        for f in sorted(filenames):
            out.append(os.path.relpath(os.path.join(dirpath, f), root))
            if len(out) >= max_entries:
                return out
    return out


def read_file(repo: str, path: str, start: int | None = None, end: int | None = None) -> str:
    """Read a file from ``repo``. With ``start``/``end`` return inclusive 1-based line range
    (``sed -n`` semantics); ``end`` past EOF clamps, ``start`` past EOF yields empty."""
    root = _repo_root(repo)
    target = _safe_join(root, path)
    with open(target, encoding="utf-8", errors="replace") as fh:
        if start is None:
            return fh.read()
        lines = fh.readlines()
    lo = max(start, 1) - 1
    hi = len(lines) if end is None else min(end, len(lines))
    return "".join(lines[lo:hi])


def search_code(repo: str, query: str, max_matches: int = 200) -> list[str]:
    """Regex-search ``repo`` with ripgrep, returning ``path:line:text`` matches (capped).

    ripgrep is .gitignore-aware and fast; exit 1 (no matches) yields an empty list.
    """
    root = _repo_root(repo)
    proc = subprocess.run(
        ["rg", "--no-heading", "--line-number", "--max-count", str(max_matches), query, root],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"ripgrep failed: {proc.stderr.strip()}")
    out = []
    for line in proc.stdout.splitlines():
        # strip the absolute root prefix so matches read as repo-relative
        out.append(line.replace(root + os.sep, "", 1))
        if len(out) >= max_matches:
            break
    return out
