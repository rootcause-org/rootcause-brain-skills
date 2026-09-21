"""Tenant-profile reads over a fixture file:

    cd runtime && PYTHONPATH=$(pwd) uv run --with '.[test]' --no-project pytest tests/test_tenant.py -q

No production mount: every case points ``RC_TENANT_PROFILE_PATH`` at a tmp file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import tenant


@pytest.fixture(autouse=True)
def _clear_cache():
    tenant._cache.clear()
    yield
    tenant._cache.clear()


def _point_at(monkeypatch, tmp_path: Path, payload: str | None) -> Path:
    path = tmp_path / "tenant_profile.json"
    if payload is not None:
        path.write_text(payload, encoding="utf-8")
    monkeypatch.setenv("RC_TENANT_PROFILE_PATH", str(path))
    return path


def test_missing_file_is_a_flat_project_not_an_error(monkeypatch, tmp_path: Path) -> None:
    _point_at(monkeypatch, tmp_path, None)
    assert tenant.profile() == {}
    assert tenant.has_profile() is False
    assert tenant.get("latecancel_min_hours", 24) == 24


def test_values_are_typed_copied_and_cached(monkeypatch, tmp_path: Path) -> None:
    path = _point_at(
        monkeypatch,
        tmp_path,
        json.dumps({"values": {"latecancel_min_hours": 24, "free_count": 0, "policy_active": False, "name": "De Kies"}}),
    )
    values = tenant.profile()
    assert values == {"latecancel_min_hours": 24, "free_count": 0, "policy_active": False, "name": "De Kies"}
    values["latecancel_min_hours"] = 999
    assert tenant.get("latecancel_min_hours") == 24
    assert tenant.has_profile() is True
    # A falsy value is a value: it must not fall through to the caller's default.
    assert tenant.get("free_count", 3) == 0
    assert tenant.get("policy_active", True) is False
    path.unlink()
    assert tenant.get("name") == "De Kies"  # cached per process, one read per path


def test_absent_key_falls_back_but_require_names_it(monkeypatch, tmp_path: Path) -> None:
    _point_at(monkeypatch, tmp_path, json.dumps({"values": {"free_count": 1, "nulled": None}}))
    assert tenant.get("missing") is None
    assert tenant.get("missing", "fallback") == "fallback"
    assert tenant.get("nulled", 7) == 7
    assert tenant.require("free_count") == 1
    with pytest.raises(tenant.TenantProfileError, match="latecancel_min_hours"):
        tenant.require("latecancel_min_hours")
    with pytest.raises(tenant.TenantProfileError, match="nulled"):
        tenant.require("nulled")


@pytest.mark.parametrize(
    "payload, needle",
    [("{not json", "not valid JSON"), ("[1, 2]", "must be a JSON object"), ('{"values": 3}', "non-object")],
)
def test_malformed_profile_is_loud(monkeypatch, tmp_path: Path, payload: str, needle: str) -> None:
    _point_at(monkeypatch, tmp_path, payload)
    with pytest.raises(tenant.TenantProfileError, match=needle):
        tenant.profile()


def test_cli_lists_gets_and_reports_absence(monkeypatch, tmp_path: Path, capsys) -> None:
    _point_at(monkeypatch, tmp_path, json.dumps({"values": {"min_hours": 24, "name": "De Kies", "active": True}}))

    assert tenant._main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"min_hours": 24, "name": "De Kies", "active": True}
    assert tenant._main(["--list"]) == 0
    assert json.loads(capsys.readouterr().out)["min_hours"] == 24

    assert tenant._main(["get", "name"]) == 0
    assert capsys.readouterr().out.strip() == "De Kies"  # bare scalar, shell-substitutable
    assert tenant._main(["get", "active"]) == 0
    assert capsys.readouterr().out.strip() == "true"

    assert tenant._main(["get", "missing"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "missing" in captured.err
    assert tenant._main(["get", "missing", "--default", "12"]) == 0
    assert capsys.readouterr().out.strip() == "12"


def test_cli_exits_two_on_a_broken_profile(monkeypatch, tmp_path: Path, capsys) -> None:
    _point_at(monkeypatch, tmp_path, "{oops")
    assert tenant._main(["get", "name"]) == 2
    assert "not valid JSON" in capsys.readouterr().err
