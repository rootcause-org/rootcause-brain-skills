#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2", "pyyaml"]
# ///
"""Validate the agent's output against its `evidence.json`, then assemble `suggestions.json`.

    uv run validate.py OUT_DIR              # classification.tsv + suggestions/*.md + learnings + headline
    uv run validate.py OUT_DIR/suggestions.json   # the assembled form (what render.py reads)

Exit 0 plus a one-line summary, or exit 1 plus one actionable line per problem, each
prefixed with the file to fix (`suggestions/S3.md: edit.old: ...`) so the next edit is a
small targeted rewrite. `warn:` lines are steering and never fail.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from assemble import assemble  # noqa: E402
from schema import Suggestions, data_errors, load, load_evidence, relabel, soft_warnings, summary, validation_errors  # noqa: E402


def _nearby(start: Path, name: str) -> Path:
    """`$OUT/<name>`, else the same name in the parent (collect's dir when $OUT is a subdir)."""
    here = start / name
    return here if here.exists() else start.parent / name


def _report(errors: list[str], where: Path) -> int:
    print(f"{len(errors)} error(s) in {where}:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


def _print_summary(sug: Suggestions, ev, warnings: list[str]) -> None:
    for warning in warnings:
        print(f"  warn: {warning}", file=sys.stderr)
    tiles = summary(ev, sug)
    if not sug.suggestions:
        topics = len({t for c in sug.classification for t in c.topics})
        print(f"pass 1: classification only ({len(sug.classification)} conversations, {topics} topics)")
        return
    print(
        f"OK: {len(sug.suggestions)} suggestions over {tiles['scanned']} conversations "
        f"({tiles['missing']} missing, {tiles['partial']} partial, {tiles['recipe']} recipe, "
        f"{tiles['wrong_title']} wrong-title, {tiles['not_kb']} not-KB), {len(sug.learnings)} learnings"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the agent's help-centre suggestions")
    parser.add_argument("target", type=Path, help="the output directory, or an assembled suggestions.json")
    parser.add_argument("--evidence", type=Path, help="default: evidence.json in the target dir or its parent")
    parser.add_argument("--articles", type=Path, help="default: raw/articles in the target dir or its parent")
    args = parser.parse_args()

    out = args.target if args.target.is_dir() else args.target.parent
    evidence = args.evidence or _nearby(out, "evidence.json")
    articles = args.articles or _nearby(out, "raw/articles")

    if not args.target.is_dir():
        errors = validation_errors(args.target, evidence, articles_dir=articles)
        if errors:
            return _report(errors, args.target)
        sug, ev = load(args.target, evidence, articles_dir=articles)
        _print_summary(sug, ev, soft_warnings(sug, ev))
        return 0

    a = assemble(out)
    if a.errors:
        return _report(a.errors, out)
    errors = relabel(data_errors(a.data, evidence, articles_dir=articles, sources=a.sources), a.labels)
    if errors:
        return _report(errors, out)

    sug = Suggestions.model_validate(a.data)
    _, ev = load_evidence(evidence)
    target = out / "suggestions.json"
    target.write_text(
        json.dumps(sug.model_dump(mode="json"), sort_keys=True, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _print_summary(sug, ev, relabel(soft_warnings(sug, ev, a.sources), a.labels))
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
