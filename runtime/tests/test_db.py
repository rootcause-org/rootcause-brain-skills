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
    assert "rc db" in msg
    assert "rc bash run" in msg
