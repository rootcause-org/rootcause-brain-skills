#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""The same window of real customer questions as source intake, in the schema intake dir.

    uv run skills/brain-schema-intake/scripts/questions.py [--days 60]
    uv run skills/brain-schema-intake/scripts/questions.py --from FILE

Everything happens in the sibling `brain-source-intake/scripts/questions.py`: this only moves
the default output directory to `.rootcause/schema-intake/<date>` so both halves of one intake
share a directory. All its flags pass straight through.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

SIBLING = Path(__file__).resolve().parents[2] / "brain-source-intake" / "scripts"
if not (SIBLING / "questions.py").is_file():
    print(f"questions: the sibling brain-source-intake scripts are missing ({SIBLING}). "
          "Reinstall the kit with brain-dev-upgrade.", file=sys.stderr)
    raise SystemExit(2)
sys.path.insert(0, str(SIBLING))

import questions as source_questions  # noqa: E402
from scan import find_brain_root  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(arg == "--out" or arg.startswith("--out=") for arg in argv):
        out = (find_brain_root() / ".rootcause" / "schema-intake" / date.today().isoformat())
        argv += ["--out", str(out)]
    return source_questions.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
