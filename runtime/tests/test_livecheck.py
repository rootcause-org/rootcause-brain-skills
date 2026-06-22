"""Pure-logic tests for the live-grounding kit — discovery, the two checks, skip-vs-fail behaviour.

No real DB: `lib.db.query` is monkeypatched, so these run on a bare host with

    cd runtime && uv run --with pytest --with 'psycopg[binary]>=3.2' pytest tests/test_livecheck.py -q

The plugin's DSN-probe / --require-live wiring is exercised end-to-end by `scripts/brain_test.py`
against a real brain; here we lock down the logic that decides what passes, fails, or skips.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import db  # noqa: E402
from lib.livecheck import (  # noqa: E402
    FAKE_UUID,
    LiveCheck,
    all_live_checks,
    assert_render_smoke,
    assert_schema,
    pick_tenant,
)


def _lc(**kw) -> LiveCheck:
    base = dict(
        model="Leader",
        render=lambda r: "# Leader\nok",
        subjects_sql="SELECT id, tenant_id FROM leaders WHERE deleted_at IS NULL LIMIT %(k)s",
    )
    base.update(kw)
    return LiveCheck(**base)


# ---- assert_render_smoke -----------------------------------------------------------------------


def test_render_smoke_passes_on_clean_output(monkeypatch):
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"id": "1", "tenant_id": "t"}])
    assert_render_smoke(_lc(render=lambda r: "# Leader\nall good"))


def test_render_smoke_skips_with_no_subjects(monkeypatch):
    monkeypatch.setattr(db, "query", lambda *a, **k: [])
    with pytest.raises(pytest.skip.Exception):
        assert_render_smoke(_lc())


def test_render_smoke_fails_on_error_sentinel(monkeypatch):
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"id": "1", "tenant_id": "t"}])
    bad = _lc(render=lambda r: "# Leader\n_subscriptions unavailable: relation does not exist_")
    with pytest.raises(AssertionError, match="error sentinel"):
        assert_render_smoke(bad)


def test_render_smoke_fails_on_wrong_prefix(monkeypatch):
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"id": "1", "tenant_id": "t"}])
    with pytest.raises(AssertionError, match="does not start with"):
        assert_render_smoke(_lc(render=lambda r: "Leader without a heading"))


# ---- assert_schema -----------------------------------------------------------------------------


def test_schema_passes_when_probes_dont_raise(monkeypatch):
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"id": "1", "tenant_id": "real-tenant"}])
    seen = {}

    def probes(t):
        seen["tenant"] = t  # the real tenant from subjects_sql flows into the probes
        return [("_core", lambda: None)]

    assert_schema(_lc(column_probes=probes))
    assert seen["tenant"] == "real-tenant"


def test_schema_falls_back_to_fake_uuid_when_table_empty(monkeypatch):
    monkeypatch.setattr(db, "query", lambda *a, **k: [])  # no rows → no real tenant
    seen = {}
    assert_schema(_lc(column_probes=lambda t: [("_core", lambda: seen.setdefault("t", t))]))
    assert seen["t"] == FAKE_UUID


def test_schema_fails_on_bad_column(monkeypatch):
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"id": "1", "tenant_id": "t"}])

    def boom():
        raise RuntimeError('column "renamed_at" does not exist')

    bad = _lc(column_probes=lambda t: [("_core", lambda: None), ("_person", boom)])
    with pytest.raises(AssertionError, match=r"_person.*renamed_at"):
        assert_schema(bad)


def test_schema_skips_without_column_probes(monkeypatch):
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"id": "1", "tenant_id": "t"}])
    with pytest.raises(pytest.skip.Exception):
        assert_schema(_lc(column_probes=None))


# ---- pick_tenant + RC_LIVE_TENANT override -----------------------------------------------------


def test_pick_tenant_auto_from_subjects_sql(monkeypatch):
    monkeypatch.delenv("RC_LIVE_TENANT", raising=False)
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"id": "1", "tenant_id": "auto-t"}])
    assert pick_tenant(_lc()) == "auto-t"


def test_pick_tenant_override_coerces_numeric_to_int(monkeypatch):
    monkeypatch.setenv("RC_LIVE_TENANT", "103")
    # the override wins without ever touching the DB
    monkeypatch.setattr(db, "query", lambda *a, **k: pytest.fail("subjects_sql ran despite override"))
    assert pick_tenant(_lc()) == 103


def test_pick_tenant_override_passes_uuid_through(monkeypatch):
    monkeypatch.setenv("RC_LIVE_TENANT", FAKE_UUID)
    monkeypatch.setattr(db, "query", lambda *a, **k: pytest.fail("subjects_sql ran despite override"))
    assert pick_tenant(_lc()) == FAKE_UUID


# ---- all_live_checks discovery -----------------------------------------------------------------


def test_all_live_checks_collects_only_modules_with_manifest(tmp_path):
    (tmp_path / "leader_to_md.py").write_text(textwrap.dedent("""
        from lib.livecheck import LiveCheck
        LIVE_CHECK = LiveCheck(model="Leader", render=lambda r: "# x",
                               subjects_sql="SELECT id, tenant_id FROM leaders LIMIT %(k)s")
    """))
    (tmp_path / "ka.py").write_text("HELPER = 1\n")  # sibling helper, no LIVE_CHECK
    (tmp_path / "__init__.py").write_text("")  # dunder — skipped

    checks = all_live_checks(tmp_path)
    assert [c.model for c in checks] == ["Leader"]


def test_all_live_checks_no_cross_skill_stem_collision(tmp_path):
    """Two skills shipping a same-named `dumper.py` must each yield their OWN LIVE_CHECK —
    a bare-stem `import_module` would cache-collide and return skill A's module for skill B."""
    manifest = 'from lib.livecheck import LiveCheck\nLIVE_CHECK = LiveCheck(model="Model{x}", render=lambda r: "# x", subjects_sql="SELECT id, tenant_id FROM t LIMIT %(k)s")\n'
    a = tmp_path / "skillA" / "scripts"
    b = tmp_path / "skillB" / "scripts"
    for d, x in ((a, "A"), (b, "B")):
        d.mkdir(parents=True)
        (d / "dumper.py").write_text(manifest.format(x=x))
    assert [c.model for c in all_live_checks(a)] == ["ModelA"]
    assert [c.model for c in all_live_checks(b)] == ["ModelB"]
