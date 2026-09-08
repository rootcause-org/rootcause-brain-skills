#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Validate one source-intake output directory, then assemble `intake.json`.

    uv run skills/brain-source-intake/scripts/validate.py OUT_DIR

Exit 0 plus a one-line summary and the assembled `intake.json` (all `render.py` reads), or
exit 1 plus one actionable line per problem, each prefixed with the file to fix
(`benchmark.tsv: Q3: ...`) so the next edit is a small targeted rewrite. `warn:` lines are
steering and never fail.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from intake_common import (  # noqa: E402
    check_markdown_hygiene, frontmatter_description, read_tsv, split_list,
)

STATUSES = ("found", "ambiguous", "missing", "n/a")
GROUPS = ("architecture", "where", "conventions", "db", "deploy")
MAX_DEVQUESTIONS = 40
WARN_DEVQUESTIONS = 25
MAX_PROPOSAL_FILES = 12
MAX_PROPOSAL_LINES = 150
MAX_LINE_CHARS = 400
MAX_HEADLINE_LINES = 3
Q_COLUMNS = ("id", "date", "channel", "question", "url")
B_COLUMNS = ("id", "cluster", "status", "where", "note")
D_COLUMNS = ("id", "group", "question", "proposal", "evidence")


# ------------------------------------------------------------------ locators

class Listings:
    """The `raw/listing-<name>.txt` files, one per repo in scan.json."""

    def __init__(self, out: Path, repos: list[dict], errors: list[str]) -> None:
        self.files: dict[str, set[str]] = {}
        self.dirs: dict[str, set[str]] = {}
        for repo in repos:
            name = str(repo.get("name") or "")
            path = out / "raw" / f"listing-{name}.txt"
            if not path.is_file():
                errors.append(f"raw/listing-{name}.txt: missing: scan.json lists repo {name!r} "
                              "but its listing is not there; rerun scan.py")
                continue
            rels = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()}
            self.files[name] = rels
            dirs: set[str] = set()
            for rel in rels:
                parts = rel.split("/")[:-1]
                for index in range(1, len(parts) + 1):
                    dirs.add("/".join(parts[:index]))
            self.dirs[name] = dirs

    def check(self, locator: str) -> str | None:
        """None when the locator resolves, else the reason it does not."""
        if not locator.startswith("/mirrors/"):
            return f"{locator!r} is not a /mirrors/<repo>/<path> locator"
        rest = locator[len("/mirrors/"):]
        name, _, rel = rest.partition("/")
        if name not in self.files:
            return f"{locator!r} names repo {name!r}, which scan.json does not know"
        rel = rel.split(":", 1)[0].rstrip("/")
        if not rel:
            return f"{locator!r} points at the repo root, not at a file"
        if rel in self.files[name] or rel in self.dirs[name]:
            return None
        return f"{locator!r} is not in raw/listing-{name}.txt"

    def is_dir(self, locator: str) -> bool:
        rest = locator[len("/mirrors/"):] if locator.startswith("/mirrors/") else ""
        name, _, rel = rest.partition("/")
        rel = rel.split(":", 1)[0].rstrip("/")
        return name in self.dirs and rel not in self.files.get(name, ()) and rel in self.dirs[name]


def check_locator(locator: str, listings: Listings, tables: set[str], had_tables: bool) -> str | None:
    if locator.startswith("db:"):
        table = locator[3:].split(".", 1)[0].strip()
        if not table:
            return f"{locator!r} names no table"
        if had_tables and table not in tables:
            return f"{locator!r} names table {table!r}, which is not in tables.tsv"
        return None
    return listings.check(locator)


# -------------------------------------------------------------------- checks

def check_benchmark(rows: list[dict], questions: list[dict], listings: Listings,
                    tables: set[str], had_tables: bool,
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
        status = row["status"]
        if status not in STATUSES:
            errors.append(f"benchmark.tsv: {qid}: status {status!r} is not one of "
                          f"{', '.join(STATUSES)}")
            continue
        if status == "found" and len(where) < 1:
            errors.append(f"benchmark.tsv: {qid}: status found needs at least one locator in `where`")
        if status == "ambiguous" and len(where) < 2:
            errors.append(f"benchmark.tsv: {qid}: status ambiguous needs two or more locators "
                          f"in `where`, got {len(where)}")
        if status in ("missing", "n/a") and where:
            errors.append(f"benchmark.tsv: {qid}: status {status} takes no locators, "
                          f"got {len(where)}")
        for locator in where:
            problem = check_locator(locator, listings, tables, had_tables)
            if problem:
                errors.append(f"benchmark.tsv: {qid}: {problem}")
            elif status == "found" and listings.is_dir(locator):
                warnings.append(f"benchmark.tsv: {qid}: {locator} is a directory, not one "
                                "grounding place; point at the file")
        if status == "ambiguous" and not row["note"]:
            warnings.append(f"benchmark.tsv: {qid}: ambiguous with an empty note; say what "
                            "makes the two places hard to tell apart")
    for row in questions:
        if row["id"] not in seen:
            errors.append(f"benchmark.tsv: {row['id']}: missing, every question needs a row")


def check_devquestions(rows: list[dict], benchmark: list[dict],
                       errors: list[str], warnings: list[str]) -> None:
    if len(rows) > MAX_DEVQUESTIONS:
        errors.append(f"devquestions.tsv: {len(rows)} questions, the cap is {MAX_DEVQUESTIONS}")
    if len(rows) > WARN_DEVQUESTIONS:
        warnings.append(f"devquestions.tsv: {len(rows)} questions is a lot to ask a dev; "
                        f"under {WARN_DEVQUESTIONS} gets answered")
    seen: set[str] = set()
    cited: set[str] = set()
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
                          "or a locator this question is about")
        cited.update(evidence)
    open_clusters: dict[str, set[str]] = {}
    for row in benchmark:
        if row["status"] in ("ambiguous", "missing"):
            open_clusters.setdefault(row["cluster"], set()).update(split_list(row["where"]))
    for cluster, locators in sorted(open_clusters.items()):
        if cluster not in cited and not (locators & cited):
            errors.append(f"devquestions.tsv: cluster {cluster!r}: ambiguous or missing in "
                          "benchmark.tsv but no dev question cites it")


def check_proposal(directory: Path, listings: Listings,
                   errors: list[str], warnings: list[str]) -> dict[str, str]:
    proposal: dict[str, str] = {}
    if not directory.is_dir():
        errors.append("proposal/: missing, write the proposed skills/codebase/ tree there "
                      "(INDEX.md plus one file per area)")
        return proposal
    files = sorted(p for p in directory.glob("*.md"))
    if not (directory / "INDEX.md").is_file():
        errors.append("proposal/INDEX.md: missing, it routes customer symptoms to the area files")
    if len(files) > MAX_PROPOSAL_FILES:
        errors.append(f"proposal/: {len(files)} files, the cap is {MAX_PROPOSAL_FILES}")
    for path in files:
        label = f"proposal/{path.name}"
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        proposal[path.name] = text
        if not frontmatter_description(text):
            errors.append(f"{label}: line 1: needs YAML frontmatter with a `description:` line")
        check_markdown_hygiene(label, text, errors, MAX_PROPOSAL_LINES, MAX_LINE_CHARS)
        paths = [token for token in re.findall(r"`([^`]+)`", text)
                 if token.startswith("/mirrors/")]
        if not paths:
            warnings.append(f"{label}: no /mirrors/ path at all; an area file that names no "
                            "file is a guess the dev cannot check")
        for token in paths:
            problem = listings.check(token.strip())
            if problem:
                errors.append(f"{label}: {problem}")
    return proposal


# ------------------------------------------------------------------ assembly

def assemble(out: Path, scan: dict, questions: list[dict], benchmark: list[dict],
             devquestions: list[dict], proposal: dict[str, str], headline: list[str]) -> dict:
    tally = {"found": 0, "ambiguous": 0, "missing": 0, "n_a": 0}
    for row in benchmark:
        key = "n_a" if row["status"] == "n/a" else row["status"]
        if key in tally:
            tally[key] += 1
    return {
        "project": scan.get("project") or out.parent.name,
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "scan": scan,
        "questions": [{key: row[key] for key in Q_COLUMNS} for row in questions],
        "benchmark": [{"id": row["id"], "cluster": row["cluster"], "status": row["status"],
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
    parser = argparse.ArgumentParser(description="Validate a source-intake output directory")
    parser.add_argument("out", type=Path, help="the intake output directory")
    args = parser.parse_args(argv)
    out: Path = args.out

    errors: list[str] = []
    warnings: list[str] = []
    if not out.is_dir():
        print(f"validate: not a directory: {out}", file=sys.stderr)
        return 2
    for name in ("scan.json", "questions.tsv", "benchmark.tsv", "devquestions.tsv"):
        if not (out / name).is_file():
            errors.append(f"{name}: missing")
    if errors:
        return report(errors, out)

    try:
        scan = json.loads((out / "scan.json").read_text(encoding="utf-8"))
    except ValueError as exc:
        return report([f"scan.json: not valid JSON: {exc}"], out)
    repos = scan.get("repos") or []
    tables = {str(row.get("table")) for row in (scan.get("tables") or [])}
    had_tables = bool(tables)

    listings = Listings(out, repos, errors)
    questions = read_tsv(out / "questions.tsv", Q_COLUMNS, errors)
    benchmark = read_tsv(out / "benchmark.tsv", B_COLUMNS, errors)
    devquestions = read_tsv(out / "devquestions.tsv", D_COLUMNS, errors)
    if errors:
        return report(errors, out)

    check_benchmark(benchmark, questions, listings, tables, had_tables, errors, warnings)
    check_devquestions(devquestions, benchmark, errors, warnings)
    proposal = check_proposal(out / "proposal", listings, errors, warnings)

    headline: list[str] = []
    headline_file = out / "headline.txt"
    if headline_file.is_file():
        headline = [line.strip() for line in
                    headline_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(headline) > MAX_HEADLINE_LINES:
            errors.append(f"headline.txt: {len(headline)} lines, the cap is {MAX_HEADLINE_LINES}")
    if errors:
        return report(errors, out)

    data = assemble(out, scan, questions, benchmark, devquestions, proposal, headline)
    target = out / "intake.json"
    target.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    for warning in warnings:
        print(f"  warn: {warning}", file=sys.stderr)
    tally = data["tally"]
    print(f"OK: {len(questions)} questions ({tally['found']} found, {tally['ambiguous']} ambiguous, "
          f"{tally['missing']} missing, {tally['n_a']} n/a) · {len(devquestions)} dev questions · "
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
