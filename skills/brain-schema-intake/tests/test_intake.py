"""End to end checks for the schema-intake engine.

    cd skills/brain-schema-intake && uv run --with pytest --no-project pytest tests -q
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import types
from html import escape
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
FIXTURES = SKILL / "fixtures"


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                          text=True, capture_output=True, check=False)


def copy_fixture(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    shutil.copytree(FIXTURES / "out", out)
    return out


# ------------------------------------------------------------ validate, render

def test_validate_and_render_fixture(tmp_path: Path) -> None:
    out = copy_fixture(tmp_path)
    done = run("validate.py", str(out))
    assert done.returncode == 0, done.stderr
    assert "OK: 8 questions" in done.stdout

    data = json.loads((out / "intake.json").read_text())
    assert data["project"] == "salonx"
    assert data["db"] == "staging"
    assert data["engine"] == "mysql"
    assert data["tally"] == {"db": 3, "both": 2, "kb": 2, "human": 1}
    assert set(data["proposal"]) == {"staging.md"}

    done = run("render.py", str(out / "intake.json"))
    assert done.returncode == 0, done.stderr
    html = (out / "report.html").read_text()
    for question in data["devquestions"]:
        assert escape(question["question"], quote=True) in html
    assert "Copy all as Markdown" in html
    assert "Schema intake · salonx · staging" in html
    assert "Tables at a glance" in html
    assert 'type="radio"' in html
    assert "<link" not in html
    assert "<script src" not in html
    assert 'src="http' not in html and 'href="http' not in html


def test_validate_errors(tmp_path: Path) -> None:
    out = copy_fixture(tmp_path)

    rows = (out / "benchmark.tsv").read_text().splitlines()
    rows = [row.replace("agenda.status;agenda.deleted", "invoices.status")  # no such table
            if row.startswith("Q1\t") else row for row in rows]
    rows = [row.replace("\tdb\t", "\tmaybe\t") if row.startswith("Q2\t") else row for row in rows]
    rows = [row.replace("remarks;remarks.state", "") if row.startswith("Q4\t") else row
            for row in rows]
    (out / "benchmark.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    proposal = (out / "proposal" / "staging.md").read_text()
    proposal += "\nThe queue is fed nightly — nobody could confirm it, and `agenda.ghost`\n"
    proposal += "is the flag we think decides.\n\n```sql\nselect * from agenda;\n```\n"
    (out / "proposal" / "staging.md").write_text(proposal, encoding="utf-8")

    done = run("validate.py", str(out))
    assert done.returncode == 1
    for expected in (
        "benchmark.tsv: Q1:",
        "is not in schema.json",
        "benchmark.tsv: Q2: verdict 'maybe'",
        "benchmark.tsv: Q4: verdict db answers from data",
        "proposal/staging.md",
        "fenced code block",
        "em dash",
        "has no column 'ghost'",
    ):
        assert expected in done.stderr, done.stderr
    assert not (out / "intake.json").exists()


# -------------------------------------------------------------------- collect

def test_collect_reduce(tmp_path: Path) -> None:
    out = tmp_path / "out"
    done = run("collect.py", "--from", str(FIXTURES / "schema.json"), "--out", str(out))
    assert done.returncode == 0, done.stderr
    text = (out / "schema.md").read_text()
    lines = {line.split(" · ")[0]: line for line in text.splitlines()}

    assert "tenant candidates: user_id (6 tables)" in text
    assert "settings" in lines["settings"].split(" · ")[3]
    assert lines["logging"].split(" · ")[3] == "log"
    assert lines["reminder_jobs"].split(" · ")[3] == "log"
    assert "soft-delete:deleted" in lines["agenda"]
    assert "enum:status=open (70%)" in lines["agenda"]
    assert "ts:time_added" in lines["remarks"]
    assert "exact rows" in lines["settings"]  # counted, not estimated
    assert "views (1): v_agenda_today" in text
    assert "budget: 41 queries" in text


# ---------------------------------------------------------------------- probe

TABLES = [
    {"table_name": "users", "table_type": "BASE TABLE", "engine": "InnoDB", "table_rows": 100,
     "data_length": 1024, "index_length": 512, "table_comment": "", "create_time": None,
     "update_time": None},
    {"table_name": "agenda", "table_type": "BASE TABLE", "engine": "InnoDB", "table_rows": 500,
     "data_length": 4096, "index_length": 1024, "table_comment": "", "create_time": None,
     "update_time": None},
]
COLUMNS = [
    ("users", "id", 1, "int(11)"), ("users", "email", 2, "varchar(32)"),
    ("users", "active", 3, "tinyint(1)"),
    ("agenda", "id", 1, "int(11)"), ("agenda", "user_id", 2, "int(11)"),
    ("agenda", "status", 3, "varchar(16)"), ("agenda", "deleted", 4, "tinyint(1)"),
]


def fake_query(sql: str, db: str | None = None) -> list[dict]:
    low = sql.lower()
    if "version()" in low:
        return [{"version()": "5.6.51-log"}]
    if "information_schema.tables" in low:
        return list(TABLES)
    if "information_schema.columns" in low:
        return [{"table_name": t, "column_name": c, "ordinal_position": p, "column_type": k,
                 "is_nullable": "YES", "column_default": None, "column_key": "", "extra": "",
                 "column_comment": ""} for t, c, p, k in COLUMNS]
    if "key_column_usage" in low:
        return [{"table_name": "agenda", "constraint_name": "fk_agenda_user",
                 "column_name": "user_id", "ordinal_position": 1,
                 "referenced_table_name": "users", "referenced_column_name": "id"}]
    if "table_constraints" in low:
        return [{"table_name": "agenda", "constraint_name": "fk_agenda_user",
                 "constraint_type": "FOREIGN KEY"}]
    if "information_schema.statistics" in low:
        return [{"table_name": "agenda", "index_name": "PRIMARY", "non_unique": 0,
                 "seq_in_index": 1, "column_name": "id"}]
    if "count(distinct" in low:
        return [{"c": 500, "d": 42}]
    if "group by" in low:
        if "`email`" in sql:
            raise AssertionError("a PII column was sampled")
        if "`status`" in sql:  # 13 groups is one too many to be an enum
            return [{"v": f"s{n}", "c": 100 - n} for n in range(13)]
        return [{"v": "0", "c": 480}, {"v": "1", "c": 20}]
    if "database()" in low:
        return [{"database()": "salonx_staging"}]
    raise AssertionError(f"unexpected query: {sql}")


def load_probe():
    lib = types.ModuleType("lib")
    database = types.ModuleType("lib.db")
    database.query = fake_query
    lib.db = database
    sys.modules["lib"] = lib
    sys.modules["lib.db"] = database
    spec = importlib.util.spec_from_file_location("probe_under_test", SCRIPTS / "probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_offline() -> None:
    probe = load_probe()
    data = probe.collect(dict(probe.CONFIG))
    assert set(data) == {"engine", "version", "database", "db", "collected_at",
                         "tenant_candidates", "tables", "columns", "keys", "indexes",
                         "profiles", "counts", "budget"}
    assert data["engine"] == "mysql"
    assert data["tenant_candidates"][0] == {"column": "user_id", "tables": 1}
    assert {(p["table"], p["column"]) for p in data["profiles"]} == {
        ("agenda", "deleted"), ("users", "active")}  # email is PII, status has 13 groups
    assert data["counts"] == [{"table": "agenda", "rows": 500, "tenants": 42}]
    assert data["budget"]["queries"] > 0

    capped = probe.collect({**probe.CONFIG, "max_profile_per_table": 1})
    assert [(p["table"], p["column"]) for p in capped["profiles"]] == [("users", "active")]
