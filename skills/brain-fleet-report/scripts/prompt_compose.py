#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2"]
# ///
"""Compose the copy-paste prompt of a finding from its structured fields.

    uv run scripts/prompt_compose.py report.json [--finding F3]

The LLM never writes the prompt blob — it fills `prompt.{task_kind, target_repo,
paths, skills, run_refs, conclusion, proposed_change, verification,
decision_needed}` and Python renders them the same way everywhere (HTML, e-mail,
txt, this CLI). Aim: 100–220 words; `validate.py` warns outside that band.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from report_schema import compose_prompt, load_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Render finding prompts as plain text")
    parser.add_argument("report", type=Path)
    parser.add_argument("--finding", help="one finding id (default: all findings with a prompt)")
    args = parser.parse_args()

    report = load_report(args.report)
    findings = [f for f in report.findings_sorted() if f.prompt]
    if args.finding:
        findings = [f for f in findings if f.id == args.finding]
        if not findings:
            print(f"{args.finding}: no such finding, or it has no prompt", file=sys.stderr)
            return 1

    for i, finding in enumerate(findings):
        if i:
            print("\n" + "-" * 72 + "\n")
        print(compose_prompt(finding))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
