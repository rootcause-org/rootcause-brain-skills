#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""`uv run render.py OUT_DIR/intake.json` -> `report.html` next to it.

One page for the customer's developer, built by the sibling source-intake renderer with a
schema flavour: the same cards, toggles, localStorage and single copy button, worded for a
database and with one extra evidence section, the tables at a glance.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

SIBLING = Path(__file__).resolve().parents[2] / "brain-source-intake" / "scripts"
if not (SIBLING / "render.py").is_file():
    print(f"render: the sibling brain-source-intake scripts are missing ({SIBLING}). "
          "Reinstall the kit with brain-dev-upgrade.", file=sys.stderr)
    raise SystemExit(2)
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _sibling(name: str, filename: str):
    """Load a sibling skill module under its own name; both skills have a `render.py`."""
    spec = importlib.util.spec_from_file_location(name, SIBLING / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_source_render = _sibling("source_intake_render", "render.py")
build, e = _source_render.build, _source_render.e

from collect import table_facts  # noqa: E402

GROUPS = [
    ("tenant", "The tenant key"),
    ("entities", "Core entities"),
    ("settings", "Settings and flags"),
    ("queues", "Queues, jobs and logs"),
    ("conventions", "Conventions"),
    ("ownership", "Ownership: legacy or new"),
]
TILES = [
    ("scanned", "questions scanned",
     "Real customer questions from the window we read, one row each in questions.tsv."),
    ("db", "from data",
     "Support can answer this from the database alone, from the tables named below."),
    ("both", "data plus knowledge",
     "The row tells us the fact, but the customer still needs an explanation we keep outside "
     "the database."),
    ("kb", "from knowledge",
     "A static answer, no lookup needed. Nothing to ask you about the schema here."),
    ("human", "human",
     "A write, money or a judgement call. Support prepares it, a person decides."),
]
STATUSES = {
    "db": ("from data", "s-found"),
    "both": ("data plus kb", "s-ambiguous"),
    "kb": ("from kb", "s-na"),
    "human": ("human", "s-missing"),
}
INTRO = ("<p>We mapped your database to build the support brain that answers your customers. "
         "Below are the things the schema alone could not tell us: what a table really holds, "
         "which one is still live, and what support is allowed to read. Roughly fifteen "
         "minutes of your time, and one button at the end copies every answer in one go.</p>")
MAX_GLANCE_ROWS = 400


def glance_html(data: dict) -> str:
    """Tables at a glance: the reduction schema.md shows, as a collapsed table."""
    schema = data.get("schema") or {}
    rows = table_facts(schema)[:MAX_GLANCE_ROWS]
    if not rows:
        return ""
    body = ["<table><tr><th>table</th><th>rows</th><th>MB</th><th>role</th>"
            "<th>tenant column</th><th>flags</th></tr>"]
    for row in rows:
        rows_text = f"{row['rows']}" + (" exact" if row["exact"] else "")
        tenant = e(row["tenant"]) or '<span class="muted">none</span>'
        body.append(f"<tr><td class=\"loc\">{e(row['table'])}</td><td>{e(rows_text)}</td>"
                    f"<td>{e(row['mb'])}</td><td>{e(row['role'])}</td><td>{tenant}</td>"
                    f"<td class=\"loc\">{e(' · '.join(row['flags']))}</td></tr>")
    body.append("</table>")
    return (f"<details><summary>Tables at a glance ({len(rows)})</summary>"
            + "".join(body) + "</details>")


def flavour(data: dict) -> dict:
    db = str(data.get("db") or "")
    return {
        "kind": "schema-intake",
        "title": "Schema intake",
        "intro": INTRO,
        "groups": GROUPS,
        "tiles": TILES,
        "statuses": STATUSES,
        "benchmark_title": "What support would answer from data (benchmark)",
        "benchmark_headers": ("cluster", "verdict", "tables and columns", "note"),
        "proposal_title": f"Proposed database map (skills/databases/{db}.md)",
        "stamp": lambda payload: stamp(payload),
        "heading": lambda payload, date: (f"Schema intake · {payload.get('project')} "
                                          f"· {db} · {date}"),
        "md_subject": lambda payload: f"{payload.get('project')}/{db}",
        "extra_sections": [glance_html(data)],
    }


def stamp(data: dict) -> str:
    schema = data.get("schema") or {}
    tables = [t for t in schema.get("tables") or []
              if "VIEW" not in str(t.get("type") or "").upper()]
    candidates = schema.get("tenant_candidates") or []
    tenant = f", tenant key {candidates[0]['column']}" if candidates else ""
    return (f"{data.get('engine')} {schema.get('version', '')}, database "
            f"{schema.get('database')}: {len(tables)} tables, "
            f"{len(schema.get('columns') or [])} columns{tenant}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render intake.json into report.html")
    parser.add_argument("intake", type=Path, help="the assembled intake.json")
    args = parser.parse_args(argv)
    if not args.intake.is_file():
        print(f"render: no such file: {args.intake}", file=sys.stderr)
        return 2
    data = json.loads(args.intake.read_text(encoding="utf-8"))
    target = args.intake.parent / "report.html"
    target.write_text(build(data, flavour(data)), encoding="utf-8")
    print(f"{len(data.get('devquestions') or [])} dev questions -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
