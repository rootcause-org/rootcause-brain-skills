#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Validate one schema-intake output directory, then assemble `intake.json`.

    uv run skills/brain-schema-intake/scripts/validate.py OUT_DIR

Exit 0 plus a one line summary and the assembled `intake.json` (all `render.py` reads), or
exit 1 plus one actionable line per problem, prefixed with the file to fix. Every table and
column you name is held against `schema.json`: a map that points at a column nobody has is
worse than no map. `warn:` lines are steering and never fail.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SIBLING = Path(__file__).resolve().parents[2] / "brain-source-intake" / "scripts"
if not (SIBLING / "intake_common.py").is_file():
    print(f"validate: the sibling brain-source-intake scripts are missing ({SIBLING}). "
          "Reinstall the kit with brain-dev-upgrade.", file=sys.stderr)
    raise SystemExit(2)
sys.path.insert(0, str(SIBLING))

from intake_common import (  # noqa: E402
    check_markdown_hygiene, frontmatter_description, read_tsv, split_list,
)

VERDICTS = ("db", "kb", "both", "human")
NEEDS_LOCATOR = ("db", "both")
GROUPS = ("tenant", "entities", "settings", "queues", "conventions", "ownership")
MAX_DEVQUESTIONS = 40
WARN_DEVQUESTIONS = 25
MAX_PROPOSAL_FILES = 6
MAX_PROPOSAL_LINES = 150
MAX_LINE_CHARS = 400
MAX_HEADLINE_LINES = 3
Q_COLUMNS = ("id", "date", "channel", "question", "url")
B_COLUMNS = ("id", "cluster", "verdict", "where", "note")
D_COLUMNS = ("id", "group", "question", "proposal", "evidence")
LOCATOR = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]+$")


# ------------------------------------------------------------------- the schema

class Schema:
    """Every table and column the probe actually saw."""

    def __init__(self, data: dict) -> None:
        self.tables = {str(table["table"]) for table in data.get("tables") or []}
        self.columns: dict[str, set[str]] = {}
        for column in data.get("columns") or []:
            self.columns.setdefault(str(column["table"]), set()).add(str(column["column"]))
        self.column_names = {name for names in self.columns.values() for name in names}

    def check(self, locator: str) -> str | None:
        """None when `table` or `table.column` resolves, else the reason it does not."""
        table, _, column = locator.partition(".")
        table, column = table.strip(), column.strip()
        if not table:
            return f"{locator!r} names no table"
        if table not in self.tables:
            return f"{locator!r} names table {table!r}, which is not in schema.json"
        if column and column not in self.columns.get(table, set()):
            return f"{locator!r}: table {table!r} has no column {column!r}"
        return None


# ---------------------------------------------------------------------- checks

def check_benchmark(rows: list[dict], questions: list[dict], schema: Schema,
                    errors: list[str], warnings: list[str]) -> None:
    known = {row["id"] for row in questions}
    seen: set[str] = set()
    for row in rows:
        qid, where = row["id"], split_list(row["where"])
        if qid not in known:
            errors.append(f"benchmark.tsv: {qid}: unknown question id, not in questions.tsv")
        elif qid in seen:
            errors.append(f"benchmark.tsv: {qid}: listed twice, one row per question")
        seen.add(qid)
        if not row["cluster"]:
            errors.append(f"benchmark.tsv: {qid}: empty cluster, every row needs a type slug")
        verdict = row["verdict"]
        if verdict not in VERDICTS:
            errors.append(f"benchmark.tsv: {qid}: verdict {verdict!r} is not one of "
                          f"{', '.join(VERDICTS)}")
            continue
        if verdict in NEEDS_LOCATOR and not where:
            errors.append(f"benchmark.tsv: {qid}: verdict {verdict} answers from data, so name "
                          "at least one table or table.column in `where`")
        if verdict not in NEEDS_LOCATOR and where:
            errors.append(f"benchmark.tsv: {qid}: verdict {verdict} takes no locators, "
                          f"got {len(where)}")
        for locator in where:
            problem = schema.check(locator)
            if problem:
                errors.append(f"benchmark.tsv: {qid}: {problem}")
        if verdict == "human" and not row["note"]:
            warnings.append(f"benchmark.tsv: {qid}: human with an empty note; say what makes it "
                            "a judgement call (a write, money, an exception)")
    for row in questions:
        if row["id"] not in seen:
            errors.append(f"benchmark.tsv: {row['id']}: missing, every question needs a row")


def check_devquestions(rows: list[dict], benchmark: list[dict], schema: Schema,
                       errors: list[str], warnings: list[str]) -> None:
    if len(rows) > MAX_DEVQUESTIONS:
        errors.append(f"devquestions.tsv: {len(rows)} questions, the cap is {MAX_DEVQUESTIONS}")
    if len(rows) > WARN_DEVQUESTIONS:
        warnings.append(f"devquestions.tsv: {len(rows)} questions is a lot to ask a dev; "
                        f"under {WARN_DEVQUESTIONS} gets answered")
    clusters = {row["cluster"] for row in benchmark if row["cluster"]}
    seen: set[str] = set()
    for row in rows:
        did = row["id"] or f"line {row['_line']}"
        if did in seen:
            errors.append(f"devquestions.tsv: {did}: listed twice")
        seen.add(did)
        if row["group"] not in GROUPS:
            errors.append(f"devquestions.tsv: {did}: group {row['group']!r} is not one of "
                          f"{', '.join(GROUPS)}")
        if not row["question"]:
            errors.append(f"devquestions.tsv: {did}: empty question")
        evidence = split_list(row["evidence"])
        if not evidence:
            errors.append(f"devquestions.tsv: {did}: empty evidence, cite the benchmark cluster "
                          "or the table this question is about")
        for item in evidence:
            if item in clusters:
                continue
            problem = schema.check(item)
            if problem:
                errors.append(f"devquestions.tsv: {did}: {problem}, and it is no benchmark "
                              "cluster either")


def check_proposal(directory: Path, db: str, schema: Schema,
                   errors: list[str], warnings: list[str]) -> dict[str, str]:
    proposal: dict[str, str] = {}
    if not directory.is_dir():
        errors.append(f"proposal/: missing, write the proposed skills/databases/{db}.md there")
        return proposal
    files = sorted(directory.glob("*.md"))
    if not (directory / f"{db}.md").is_file():
        errors.append(f"proposal/{db}.md: missing, it is the map a run reads first for this "
                      "database")
    if len(files) > MAX_PROPOSAL_FILES:
        errors.append(f"proposal/: {len(files)} files, the cap is {MAX_PROPOSAL_FILES}")
    for path in files:
        label = f"proposal/{path.name}"
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        proposal[path.name] = text
        if not frontmatter_description(text):
            errors.append(f"{label}: line 1: needs YAML frontmatter with a `description:` line")
        check_markdown_hygiene(label, text, errors, MAX_PROPOSAL_LINES, MAX_LINE_CHARS)
        named = 0
        for token in re.findall(r"`([^`]+)`", text):
            token = token.strip()
            if LOCATOR.match(token):
                named += 1
                problem = schema.check(token)
                if problem:
                    errors.append(f"{label}: {problem}")
            elif IDENTIFIER.match(token):
                if token in schema.tables:
                    named += 1
                elif token not in schema.column_names:
                    errors.append(f"{label}: `{token}` is not a table or column in schema.json; "
                                  "name a real one or drop the backticks")
        if not named:
            warnings.append(f"{label}: no table named in backticks; a map that names no table "
                            "is a guess the developer cannot check")
    return proposal


# -------------------------------------------------------------------- assembly

def assemble(schema_data: dict, out: Path, questions: list[dict], benchmark: list[dict],
             devquestions: list[dict], proposal: dict[str, str], headline: list[str]) -> dict:
    tally = {verdict: 0 for verdict in VERDICTS}
    for row in benchmark:
        if row["verdict"] in tally:
            tally[row["verdict"]] += 1
    return {
        "project": schema_data.get("project") or out.parent.name,
        "db": schema_data.get("db") or schema_data.get("database") or "",
        "engine": schema_data.get("engine") or "",
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "schema": schema_data,
        "questions": [{key: row[key] for key in Q_COLUMNS} for row in questions],
        "benchmark": [{"id": row["id"], "cluster": row["cluster"], "status": row["verdict"],
                       "where": split_list(row["where"]), "note": row["note"]}
                      for row in benchmark],
        "devquestions": [{"id": row["id"], "group": row["group"], "question": row["question"],
                          "proposal": row["proposal"], "evidence": split_list(row["evidence"])}
                         for row in devquestions],
        "proposal": proposal,
        "headline": headline,
        "tally": tally,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a schema-intake output directory")
    parser.add_argument("out", type=Path, help="the intake output directory")
    args = parser.parse_args(argv)
    out: Path = args.out

    errors: list[str] = []
    warnings: list[str] = []
    if not out.is_dir():
        print(f"validate: not a directory: {out}", file=sys.stderr)
        return 2
    for name in ("schema.json", "questions.tsv", "benchmark.tsv", "devquestions.tsv"):
        if not (out / name).is_file():
            errors.append(f"{name}: missing")
    if errors:
        return report(errors, out)

    try:
        schema_data = json.loads((out / "schema.json").read_text(encoding="utf-8"))
    except ValueError as exc:
        return report([f"schema.json: not valid JSON: {exc}"], out)
    schema = Schema(schema_data)
    if not schema.tables:
        return report(["schema.json: no tables; rerun collect.py"], out)

    questions = read_tsv(out / "questions.tsv", Q_COLUMNS, errors)
    benchmark = read_tsv(out / "benchmark.tsv", B_COLUMNS, errors)
    devquestions = read_tsv(out / "devquestions.tsv", D_COLUMNS, errors)
    if errors:
        return report(errors, out)

    check_benchmark(benchmark, questions, schema, errors, warnings)
    check_devquestions(devquestions, benchmark, schema, errors, warnings)
    db = str(schema_data.get("db") or schema_data.get("database") or "")
    proposal = check_proposal(out / "proposal", db, schema, errors, warnings)

    headline: list[str] = []
    headline_file = out / "headline.txt"
    if headline_file.is_file():
        headline = [line.strip() for line in
                    headline_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(headline) > MAX_HEADLINE_LINES:
            errors.append(f"headline.txt: {len(headline)} lines, the cap is {MAX_HEADLINE_LINES}")
    if errors:
        return report(errors, out)

    data = assemble(schema_data, out, questions, benchmark, devquestions, proposal, headline)
    target = out / "intake.json"
    target.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    for warning in warnings:
        print(f"  warn: {warning}", file=sys.stderr)
    tally = data["tally"]
    print(f"OK: {len(questions)} questions ({tally['db']} db, {tally['both']} both, "
          f"{tally['kb']} kb, {tally['human']} human) · {len(devquestions)} dev questions · "
          f"{len(proposal)} proposal files")
    print(f"wrote {target}")
    return 0


def report(errors: list[str], out: Path) -> int:
    print(f"{len(errors)} error(s) in {out}:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
