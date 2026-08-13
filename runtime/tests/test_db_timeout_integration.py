"""Opt-in integration proof that the timeout cap really KILLS the query server-side.

The mock tests prove we emit the right SQL; only a real server proves the effect. That distinction
matters here because the dbproxy in front of production rejects CancelRequest — if the cap merely
made the client stop listening, a runaway query would keep burning the customer's database while we
reported a timeout. So (b) below re-checks from a SECOND connection that the sleeping backend is
actually gone.

Runs only when ``TEST_PG_DSN`` points at a throwaway Postgres (nothing is written; it just sleeps):

    TEST_PG_DSN=postgresql://postgres@localhost:5432/postgres \
        uv run --with . --with pytest --no-project pytest tests/test_db_timeout_integration.py -q
"""

from __future__ import annotations

import os
import time
import warnings

import pytest

from lib import db

DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_PG_DSN to run the live timeout proof")


@pytest.fixture
def capped(monkeypatch):
    """A 2s cap instead of 120s, so the proof takes seconds rather than minutes."""
    monkeypatch.setattr(db, "MAX_TIMEOUT_MS", 2_000)
    monkeypatch.setenv("PG_DSN", DSN)
    return 2.0


def test_oversized_timeout_is_capped_and_query_dies(capped):
    started = time.monotonic()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(Exception) as excinfo:  # noqa: PT011 - psycopg QueryCanceled
            db.query("select pg_sleep(30)", timeout_ms=600_000)
    elapsed = time.monotonic() - started

    assert "canceling statement due to statement timeout" in str(excinfo.value)
    assert elapsed < 10, f"took {elapsed:.1f}s — the 600s request was not clamped"
    assert any("hard cap" in str(w.message) for w in caught)


def test_killed_backend_is_gone_from_pg_stat_activity(capped):
    """The 'really killed server-side' proof: no backend is left running our pg_sleep afterwards."""
    marker = "rc_timeout_proof_marker"
    sql = f"select pg_sleep(30) /* {marker} */"

    with pytest.raises(Exception):  # noqa: B017, PT011 - cancellation shape is psycopg's
        db.query(sql, timeout_ms=600_000)

    # Fresh connection (the killed one is closed): poll until the sleeper is gone, fail if it lingers.
    deadline = time.monotonic() + 5
    while True:
        rows = db.query(
            "select count(*) as n from pg_stat_activity "
            "where query like %s and pid <> pg_backend_pid() and state = 'active'",
            [f"%{marker}%"],
        )
        if rows[0]["n"] == 0:
            break
        assert time.monotonic() < deadline, "the sleeping backend survived the statement_timeout"
        time.sleep(0.2)


def test_multi_statement_bypass_is_refused_client_side(capped):
    # The attack the one-statement rule exists for: the simple protocol would run BOTH, lifting the
    # cap and returning [] while pg_sleep ran unbounded.
    with pytest.raises(RuntimeError, match="multiple SQL statements"):
        db.query("SET statement_timeout = 0; select pg_sleep(30)")


def test_normal_query_still_works(capped):
    assert db.query("select 1 as n") == [{"n": 1}]
