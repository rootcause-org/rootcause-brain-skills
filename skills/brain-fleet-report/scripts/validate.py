#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2", "psycopg[binary]>=3"]
# ///
"""Validate a fleet `report.json`.

    uv run scripts/validate.py report.json [--kpis kpis.json] [--manifest manifest.json]
                                           [--evidence evidence.json]

Exit 0 + a one-line summary, or exit 1 + one actionable line per problem
(`json.path: message (hint)`) so the next edit is a small targeted patch instead
of a regenerated report. Soft rules print as `warn:` lines and never fail.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from report_schema import (  # noqa: E402
    evidence_errors,
    load_report,
    soft_warnings,
    validation_errors,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate report.json")
    parser.add_argument("report", type=Path)
    parser.add_argument(
        "--kpis", type=Path, help="collector kpis.json to compare against"
    )
    parser.add_argument(
        "--manifest", type=Path, help="collector manifest.json to compare against"
    )
    parser.add_argument(
        "--evidence", type=Path, help="collector evidence.json (run-id check)"
    )
    parser.add_argument("--dsn", help="Postgres DSN; otherwise use the operator SSM tunnel")
    args = parser.parse_args()

    errors = validation_errors(args.report, dsn=args.dsn)
    report = None
    if not errors:
        report = load_report(args.report, dsn=args.dsn)
        if args.evidence and args.evidence.exists():
            errors = evidence_errors(report, args.evidence)

    if errors:
        print(f"{len(errors)} error(s) in {args.report}:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    assert report is not None
    kpis = args.kpis if args.kpis and args.kpis.exists() else None
    manifest = args.manifest if args.manifest and args.manifest.exists() else None
    for warning in soft_warnings(report, kpis, manifest):
        print(f"  warn: {warning}", file=sys.stderr)

    prompts = sum(bool(report.effective(f).prompt) for f in report.findings)
    unchanged = sum(f.status == "unchanged" for f in report.findings)
    owner = sum(
        f.audience in ("owner", "both") and f.status != "unchanged"
        for f in report.findings
    )
    print(
        f"OK — {len(report.findings)} findings / {prompts} with prompt / "
        f"{unchanged} unchanged / {owner} owner cards"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
