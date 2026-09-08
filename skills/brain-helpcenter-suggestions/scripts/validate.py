#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2"]
# ///
"""Validate a `suggestions.json` against its `evidence.json`.

    uv run validate.py DIR/suggestions.json [--evidence DIR/evidence.json]

Exit 0 + a one-line summary, or exit 1 + one actionable line per problem
(`json.path: message (hint)`) so the next edit is a small targeted patch instead
of a regenerated file. `warn:` lines are steering and never fail.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schema import load, soft_warnings, summary, validation_errors  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate suggestions.json")
    parser.add_argument("suggestions", type=Path)
    parser.add_argument("--evidence", type=Path, help="default: evidence.json next to suggestions.json")
    args = parser.parse_args()
    evidence = args.evidence or args.suggestions.parent / "evidence.json"

    errors = validation_errors(args.suggestions, evidence)
    if errors:
        print(f"{len(errors)} error(s) in {args.suggestions}:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    sug, ev = load(args.suggestions, evidence)
    for warning in soft_warnings(sug, ev):
        print(f"  warn: {warning}", file=sys.stderr)
    tiles = summary(ev, sug)
    print(
        f"OK — {len(sug.suggestions)} suggestions over {tiles['scanned']} conversations "
        f"({tiles['missing']} missing, {tiles['partial']} partial, {tiles['wrong_title']} wrong-title, "
        f"{tiles['not_kb']} not-KB), {len(sug.learnings)} learnings"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
