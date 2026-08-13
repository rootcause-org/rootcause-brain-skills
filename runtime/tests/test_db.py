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


# --- Timeout hard cap + one-statement rule -----------------------------------------------------
#
# The dbproxy in front of production refuses CancelRequest, so a client-side cancel does nothing:
# the SERVER-side statement_timeout is the only thing that ever stops a runaway grounding query.
# These lock down that it is always set, never disabled, and never above the cap.


class _RecordingCursor:
    description = None

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)

    def fetchall(self):
        return []


class _RecordingConnection:
    read_only = False

    def __init__(self, server_version=160004):
        self.executed = []
        self.info = SimpleNamespace(server_version=server_version)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _RecordingCursor(self)


def _executed(monkeypatch, server_version=160004, **kwargs):
    """Run `select 1` against a recording double; return (executed SQL, connect kwargs)."""
    conn = _RecordingConnection(server_version)
    seen = {}

    def connect(*_a, **kw):
        seen.update(kw)
        return conn

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setenv("PG_DSN", "postgresql://example")
    db.query("select 1", **kwargs)
    return conn.executed, seen


def _timeout_value(executed, guc="statement_timeout"):
    setsql = executed[0]
    assert setsql.lstrip().upper().startswith("SET LOCAL")
    for stmt in setsql.split(";"):
        if guc in stmt:
            return int(stmt.split("=")[1])
    return None


@pytest.mark.parametrize(
    "kwargs, expected, warns",
    [
        ({}, 30_000, False),  # omitted → default
        ({"timeout_ms": 60_000}, 60_000, False),  # under the cap → honoured
        ({"timeout_ms": 600_000}, 120_000, True),  # over the cap → clamped, loudly
        ({"timeout_ms": 0}, 30_000, False),  # 0 means "default", NOT "no timeout"
        ({"timeout_ms": None}, 30_000, False),
    ],
)
def test_statement_timeout_is_always_set_and_capped(monkeypatch, recwarn, kwargs, expected, warns):
    executed, _ = _executed(monkeypatch, **kwargs)
    assert _timeout_value(executed) == expected
    clamped = [w for w in recwarn if "hard cap" in str(w.message)]
    assert bool(clamped) is warns


def test_belt_and_suspenders_gucs(monkeypatch):
    # PG 16 has no transaction_timeout; an unknown SET LOCAL would ABORT the transaction, so it is
    # gated on the server version rather than tried and caught.
    pg16, _ = _executed(monkeypatch, server_version=160004)
    assert "idle_in_transaction_session_timeout = 30000" in pg16[0]
    assert "transaction_timeout" not in pg16[0]

    pg17, _ = _executed(monkeypatch, server_version=170004)
    assert "transaction_timeout = 32000" in pg17[0]  # +slack: statement_timeout must win the race


def test_connect_passes_tcp_keepalives(monkeypatch):
    _, kwargs = _executed(monkeypatch)
    assert kwargs["keepalives"] == 1
    assert kwargs["keepalives_idle"] == 30
    assert kwargs["keepalives_interval"] == 10
    assert kwargs["keepalives_count"] == 3


@pytest.mark.parametrize(
    "sql",
    [
        "SET statement_timeout = 0; select pg_sleep(300)",
        "select 1; select 2",
        "select 1;\n-- comment\nselect 2",
        "select 1; /* c */ select 2",
    ],
)
def test_multi_statement_sql_is_rejected(monkeypatch, sql):
    conn = _RecordingConnection()
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda *a, **k: conn))
    monkeypatch.setenv("PG_DSN", "postgresql://example")
    with pytest.raises(RuntimeError, match="multiple SQL statements"):
        db.query(sql)
    assert conn.executed == []  # rejected before we ever connect/execute


@pytest.mark.parametrize(
    "sql",
    [
        "select 'a;b'",  # ';' inside a single-quoted literal
        "select 'it''s; fine'",  # …with an escaped quote before it
        'select "weird;col" from t',  # …inside a quoted identifier
        "select $$a;b$$",  # …inside a dollar-quoted string
        "select $tag$a;b$tag$",
        "select 1;",  # a single trailing ';'
        "select 1;  \n",
        "select 1 -- comment; more",  # ';' inside a line comment
        "select 1 /* c; c */ from t",  # …inside a block comment
        "select 1; -- trailing note; still one statement",
    ],
)
def test_single_statement_sql_is_allowed(monkeypatch, sql):
    db._reject_multi_statement(sql)  # must not raise
