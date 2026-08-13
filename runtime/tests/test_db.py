from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from lib import db


class _Cursor:
    description = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        return None

    def fetchall(self):
        return []


class _Connection:
    read_only = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _Cursor()


def test_query_sets_connect_timeout(monkeypatch):
    calls = []

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return _Connection()

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setenv("PG_DSN", "postgresql://example")

    assert db.query("select 1") == []
    assert calls[0][1]["connect_timeout"] == 15
    assert calls[0][1]["autocommit"] is False


def test_local_connect_failure_points_to_rc_primitives(monkeypatch):
    def connect(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setenv("PG_DSN", "postgresql://example")
    monkeypatch.setenv("RC_LOCAL_BRAIN_RUN", "1")

    with pytest.raises(RuntimeError) as excinfo:
        db.query("select 1")

    msg = str(excinfo.value)
    assert "connect_timeout=15s" in msg
    assert "local brain_run.py live check" in msg
    assert "rc dev console database" in msg
    assert "rc dev console bash run" in msg


# --- Postgres array literals -------------------------------------------------------------------
#
# psycopg hydrates arrays of element types it knows; enum arrays (per-database OIDs) arrive as the
# raw literal string, so lib.db parses those itself. Tests cover the grammar and — separately —
# the OID-driven DECISION to parse at all.


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("{}", []),
        ("{parent}", ["parent"]),
        ("{parent,child}", ["parent", "child"]),
        ('{"a,b",c}', ["a,b", "c"]),  # embedded comma
        (r'{"x\"y"}', ['x"y']),  # escaped quote
        (r'{"a\\b"}', [r"a\b"]),  # escaped backslash
        ("{NULL}", [None]),  # NULL element
        ('{"NULL"}', ["NULL"]),  # quoted NULL is the string
        ("{a,NULL,b}", ["a", None, "b"]),
        ('{""}', [""]),  # empty-string element
        ("{{1,2},{3}}", [["1", "2"], ["3"]]),  # nested
        ("[0:1]={a,b}", ["a", "b"]),  # explicit dimension prefix
        ("{⟦pii:abc⟧,plain}", ["⟦pii:abc⟧", "plain"]),  # dbproxy tokens are ordinary elements
        ('{"⟦pii:a,b⟧"}', ["⟦pii:a,b⟧"]),
        ("{a b,c}", ["a b", "c"]),  # unquoted space
    ],
)
def test_parse_pg_array(literal, expected):
    assert db._parse_pg_array(literal) == expected


@pytest.mark.parametrize("value", ["", "plain text", "not{an}array", "⟦pii:abc⟧", "{unterminated"])
def test_parse_pg_array_passes_through_non_literals(value):
    assert db._parse_pg_array(value) == value


_TEXT_OID = 25
_ENUM_ARRAY_OID = 987654  # enum arrays get a per-database OID — nothing static to match on


class _Col(SimpleNamespace):
    """Stand-in for psycopg's cursor.description entry (name + type_code)."""


class _ResultCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)
        if "pg_type" in sql:
            self.description = [_Col(name="oid", type_code=26)]
            self._rows = [(_ENUM_ARRAY_OID,)]
        elif sql.lstrip().upper().startswith("SET"):
            pass
        else:
            self.description = self.conn.description
            self._rows = self.conn.rows

    def fetchall(self):
        return self._rows


class _ResultConnection:
    read_only = False

    def __init__(self, description, rows):
        self.description = description
        self.rows = rows
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _ResultCursor(self)


def _run(monkeypatch, description, rows, dsn="postgresql://example"):
    conn = _ResultConnection(description, rows)
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda *a, **k: conn))
    monkeypatch.setenv("PG_DSN", dsn)
    db._ARRAY_OIDS.clear()
    return db.query("select roles, note from people"), conn


def test_query_hydrates_enum_array_columns(monkeypatch):
    rows, conn = _run(
        monkeypatch,
        [_Col(name="roles", type_code=_ENUM_ARRAY_OID), _Col(name="note", type_code=_TEXT_OID)],
        [("{parent,child}", "hello"), ("{}", None), (None, "{looks,like,an,array}")],
    )
    assert rows[0]["roles"] == ["parent", "child"]
    assert rows[1]["roles"] == []  # empty array, not None
    assert rows[2]["roles"] is None  # NULL column stays None, never []
    # A plain text column is never parsed, even when its value looks exactly like a literal.
    assert rows[2]["note"] == "{looks,like,an,array}"
    assert sum("pg_type" in s for s in conn.executed) == 1  # OID set fetched once, then cached


def test_query_skips_oid_lookup_when_no_array_shaped_value(monkeypatch):
    _, conn = _run(
        monkeypatch,
        [_Col(name="roles", type_code=_ENUM_ARRAY_OID), _Col(name="note", type_code=_TEXT_OID)],
        [(None, "hello"), (None, "⟦pii:abc⟧")],
    )
    assert not any("pg_type" in s for s in conn.executed)


def test_query_leaves_whole_column_pii_token_alone(monkeypatch):
    # The dbproxy can replace an entire array value with one token; it must survive as a string
    # (parsing it would be nonsense) rather than raise.
    rows, _ = _run(
        monkeypatch,
        [_Col(name="roles", type_code=_ENUM_ARRAY_OID), _Col(name="note", type_code=_TEXT_OID)],
        [("⟦pii:abc⟧", "{a,b}")],
    )
    assert rows[0]["roles"] == "⟦pii:abc⟧"
