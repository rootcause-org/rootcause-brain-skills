#!/usr/bin/env python3
"""Instant dependency-light lint for a rootcause brain checkout."""

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain_lint.py", description=__doc__)
    parser.add_argument("--brain", help="brain dir (default: cwd)")
    parser.add_argument("--strict", action="store_true", help="exit 1 when WARN findings exist")
    args = parser.parse_args(argv)

    brain = Path(args.brain).expanduser().resolve() if args.brain else Path.cwd().resolve()
    if not (brain / "skills").is_dir() and not (brain / "actions").is_dir():
        print(f"error: no skills/ or actions/ under {brain} — is this a brain checkout?", file=sys.stderr)
        return 1

    findings = lint_brain(brain)
    print(format_report(findings))
    return int(any(f.level == "FAIL" or (args.strict and f.level == "WARN") for f in findings))


if __name__ == "__main__":
    raise SystemExit(main())
