"""Opt-in proof that the `mysql://` branch works against a REAL MySQL 8.

The mocked tests prove we emit the right SQL; only a real server proves the information_schema
queries, the read-only transaction, and `EXPLAIN FORMAT=TREE` actually parse and return what
`lib.db` claims. Runs only when ``TEST_MYSQL_DSN`` points at a throwaway MySQL (nothing is written
beyond this test's own fixture table):

    docker run --rm -d -e MYSQL_ROOT_PASSWORD=x -e MYSQL_DATABASE=t -p 33061:3306 mysql:8.4
    TEST_MYSQL_DSN=mysql://root:x@127.0.0.1:33061/t \
        uv run --with '.[test]' --no-project pytest tests/test_db_mysql_integration.py -q
"""

from __future__ import annotations

import os

import pytest

from lib import db

DSN = os.environ.get("TEST_MYSQL_DSN")

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_MYSQL_DSN to run the live MySQL proof")


@pytest.fixture(scope="module")
def fixture_table():
    """A tiny table + index, created with the raw driver (lib.db is read-only by construction)."""
    import pymysql

    kwargs = db._mysql_connect_kwargs(DSN, 30_000)
    conn = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **kwargs)
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS rc_clients")
        cur.execute(
            "CREATE TABLE rc_clients (id INT PRIMARY KEY, email VARCHAR(255), "
            "national_id VARCHAR(32), meta JSON, KEY rc_clients_email_idx (email))"
        )
        cur.execute(
            "INSERT INTO rc_clients VALUES (1, 'a@b.test', 'X1', '{\"tier\": \"pro\"}'), "
            "(2, 'c@d.test', 'X2', NULL)"
        )
    conn.commit()
    conn.close()
    yield "rc_clients"


@pytest.fixture(autouse=True)
def _dsn(monkeypatch):
    monkeypatch.setenv("PG_DSN", DSN)


def test_query_returns_dict_rows_and_binds_params(fixture_table):
    rows = db.query("select id, email from rc_clients where id = %s", [2])
    assert rows == [{"id": 2, "email": "c@d.test"}]


def test_json_column_decodes(fixture_table):
    rows = db.query("select id, meta from rc_clients order by id")
    assert rows[0]["meta"] == {"tier": "pro"}
    assert rows[1]["meta"] is None


def test_transaction_is_read_only(fixture_table):
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - PyMySQL's OperationalError
        db.query("update rc_clients set email = 'nope@x.test' where id = 1")
    assert "read only" in str(excinfo.value).lower()


def test_tables_and_columns(fixture_table):
    assert any(t["table_name"] == "rc_clients" for t in db.tables())
    cols = db.columns("rc_clients")
    assert cols.keys() == ["id", "email", "national_id", "meta"]
    assert cols["email"].type == "varchar(255)"
    assert "email" in cols


def test_tables_with_column(fixture_table):
    hits = db.tables_with_column("%mail%")
    assert [(c.table, str(c)) for c in hits] == [("rc_clients", "email")]


def test_table_stats_is_catalog_only(fixture_table):
    info = db.table_stats("rc_clients")
    assert info["table"] == "rc_clients"
    assert info["estimated_rows"] is not None
    assert info["total_size"].endswith(("bytes", "kB", "MB"))
    assert {"PRIMARY", "rc_clients_email_idx"} <= {i["name"] for i in info["indexes"]}
    assert info["seq_scan"] is None  # MySQL keeps no per-table scan counters


def test_explain_uses_format_tree(fixture_table):
    plan = db.explain("select * from rc_clients where email = 'a@b.test'")
    assert "rc_clients" in plan


def test_multi_statement_is_refused(fixture_table):
    with pytest.raises(RuntimeError, match="multiple SQL statements"):
        db.query("SET SESSION max_execution_time = 0; select 1")


def test_unknown_column_hint_is_scoping_aware(fixture_table):
    with pytest.raises(RuntimeError) as excinfo:
        db.query("select nope from rc_clients")
    assert "data-scoping" in str(excinfo.value)


def test_excluded_column_auto_heals(fixture_table, monkeypatch):
    # The heal map is keyed by the *_DSN env var name, so resolve through TEST_MYSQL_DSN itself.
    monkeypatch.delenv("PG_DSN")
    monkeypatch.setenv(
        "RC_DB_EXCLUDED_COLUMNS",
        '{"TEST_MYSQL_DSN": {"tables": {"rc_clients": {"exclude": ["national_id"]}}}}',
    )
    with pytest.warns(UserWarning, match="national_id"):
        rows = db.query("select id, national_id, email from rc_clients where id = 1")
    assert rows == [{"id": 1, "email": "a@b.test"}]


def test_statement_timeout_kills_a_runaway_query(fixture_table, monkeypatch):
    # SLEEP() is deliberately immune to max_execution_time, so burn real CPU instead.
    monkeypatch.setattr(db, "MAX_TIMEOUT_MS", 1_000)
    with pytest.raises(RuntimeError) as excinfo:
        db.query(
            "select count(*) from information_schema.columns a, information_schema.columns b, "
            "information_schema.columns c",
            timeout_ms=600_000,
        )
    assert "killed on MySQL" in str(excinfo.value)
