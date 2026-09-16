#!/usr/bin/env python3
"""Instant dependency-light lint for a rootcause brain checkout.

    brain_lint.py                      # whole tree (default)
    brain_lint.py --all                # whole tree, explicitly
    brain_lint.py AGENTS.md skills/    # only these files/dirs
    brain_lint.py --strict             # WARN findings fail too
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

RUNTIME = (Path(__file__).resolve().parents[3] / "runtime").resolve()
if RUNTIME.is_dir():
    sys.path.insert(0, str(RUNTIME))

_BOOTSTRAP_FLAG = "RC_BRAIN_LINT_BOOTSTRAPPED"

# Suffixes this linter can produce findings for (SKILL.md / cases *.md, action manifests, stray
# *.py). Explicit paths with any other suffix are ignored rather than treated as clean-but-scanned.
SCAN_SUFFIXES = {".md", ".py", ".yaml", ".yml"}

# Run-hidden trees (`.replypenignore` / `.rcignore` / the conventional `_internal/`): maintainer-only
# content that is physically absent from every run, so no linter judges it. Same semantics as
# brain_structure.py's ignored-refs resolver and brain-harvest/scripts/brain_lint.py.
IGNORE_CONTROLS = (".replypenignore", ".rcignore")


def _run_hidden(brain: Path) -> tuple[set[str], tuple[str, ...]]:
    """(exact paths, directory prefixes) the production run never sees, brain-relative posix."""
    exact: set[str] = set()
    prefixes = {"_internal/"}
    for control in IGNORE_CONTROLS:
        if not (brain / control).is_file():
            continue
        proc = subprocess.run(
            ["git", "-C", str(brain), "ls-files", "-z", "-c", "-o", "-i",
             f"--exclude-from={brain / control}"], capture_output=True, text=True)
        if proc.returncode != 0:
            continue  # not a git checkout: judge everything, as before
        for entry in (e for e in proc.stdout.split("\0") if e):
            exact.add(entry)
            if (brain / entry).is_dir():
                prefixes.add(entry.rstrip("/") + "/")
    return exact, tuple(sorted(prefixes))


def _reexec_with_pyyaml() -> int:
    """The interpreter that picked us up lacks PyYAML (typical when `python3` resolves to an
    ephemeral `uv run` venv, e.g. as a `brain_git_sync.py --verify-command`). Re-exec once under
    `uv run --no-project --with pyyaml` instead of failing with an exit that aborts a sync."""
    uv = shutil.which("uv")
    if uv is None or os.environ.get(_BOOTSTRAP_FLAG):
        raise SystemExit(
            "error: PyYAML is required; run via `uv run --no-project --with pyyaml python brain_lint.py`"
        )
    cmd = [uv, "run", "--no-project", "--with", "pyyaml", "python", str(Path(__file__).resolve()), *sys.argv[1:]]
    return subprocess.run(cmd, env={**os.environ, _BOOTSTRAP_FLAG: "1"}).returncode


try:
    from lib.brain_lint import format_report, lint_brain
except ModuleNotFoundError as exc:
    if exc.name != "yaml":
        raise
    raise SystemExit(_reexec_with_pyyaml()) from None


def _select(paths: list[str], brain: Path) -> set[str]:
    """Brain-relative posix paths/dir-prefixes to keep findings for.

    Paths outside the brain, or whose suffix this linter never scans, are ignored — callers
    (brain_structure.py's changed-scope pass) hand over whatever git reported as changed.
    """
    selected: set[str] = set()
    for raw in paths:
        candidate = Path(raw)
        resolved = (candidate if candidate.is_absolute() else brain / candidate).resolve()
        try:
            rel = resolved.relative_to(brain).as_posix()
        except ValueError:
            continue
        if resolved.is_dir():
            selected.add(rel.rstrip("/") + "/")
        elif resolved.suffix.lower() in SCAN_SUFFIXES:
            selected.add(rel)
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain_lint.py", description=__doc__)
    parser.add_argument("--brain", help="brain dir (default: cwd)")
    parser.add_argument("--strict", action="store_true", help="exit 1 when WARN findings exist")
    parser.add_argument("--all", action="store_true",
                        help="lint the whole brain tree (the default when no paths are given)")
    parser.add_argument("paths", nargs="*",
                        help="lint only these files/dirs (relative to the brain dir); paths whose "
                             "suffix this linter does not scan are ignored")
    args = parser.parse_args(argv)

    brain = Path(args.brain).expanduser().resolve() if args.brain else Path.cwd().resolve()
    if not (brain / "skills").is_dir() and not (brain / "actions").is_dir():
        print(f"error: no skills/ or actions/ under {brain} — is this a brain checkout?", file=sys.stderr)
        return 1

    findings = lint_brain(brain)
    hidden_exact, hidden_dirs = _run_hidden(brain)
    findings = [f for f in findings
                if (rel := f.path.split(":", 1)[0]) not in hidden_exact
                and not rel.startswith(hidden_dirs)]
    if args.paths and not args.all:
        selected = _select(args.paths, brain)
        dirs = tuple(p for p in selected if p.endswith("/"))
        # Some rules append `:<line>` to the path they report; scope on the file part.
        findings = [f for f in findings
                    if (rel := f.path.split(":", 1)[0]) in selected or rel.startswith(dirs)]
    print(format_report(findings))
    return int(any(f.level == "FAIL" or (args.strict and f.level == "WARN") for f in findings))


if __name__ == "__main__":
    raise SystemExit(main())
