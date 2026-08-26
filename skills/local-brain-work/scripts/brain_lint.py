#!/usr/bin/env python3
"""Instant dependency-light lint for a rootcause brain checkout."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RUNTIME = (Path(__file__).resolve().parents[3] / "runtime").resolve()
if RUNTIME.is_dir():
    sys.path.insert(0, str(RUNTIME))

try:
    from lib.brain_lint import format_report, lint_brain
except ModuleNotFoundError as exc:
    if exc.name == "yaml":
        raise SystemExit("error: PyYAML is required; use a Python installation that provides `yaml`") from exc
    raise


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
