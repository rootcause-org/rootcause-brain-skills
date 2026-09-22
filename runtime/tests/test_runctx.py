"""Run-context reads over a fixture file:

    cd runtime && PYTHONPATH=$(pwd) uv run --with '.[test]' --no-project pytest tests/test_runctx.py -q

No production mount: every case points ``RC_RUN_CONTEXT_PATH`` at a tmp file, or injects the document
as ``RC_RUN_CONTEXT_JSON`` the way an action container receives it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import runctx

CHAT_RUN = {
    "schema_version": 1,
    "plane": "run",
    "run_id": "11111111-1111-1111-1111-111111111111",
    "project": {"id": "22222222-2222-2222-2222-222222222222", "name": "kampadmin"},
    "tenant": {"id": "33333333-3333-3333-3333-333333333333", "slug": "solhi", "scope_value": "org-42"},
    "kind": "chat",
    "mode": "chat",
    "scenario": "chat",
    "surface": "chat",
    "channel": None,
    "origin": None,
    "simulation": False,
    "principal": {"kind": "kampadmin_person", "external_id": "p-88", "asserted_by": "embassy", "assurance": "session"},
    "session_id": "sess-7",
    "thread_id": "thread-9",
    "brain_ref": {"requested": None, "resolved": "channel:stable @ abc123", "sha": "abc123def"},
    "action": None,
}

ACTION_PLANE = {
    "schema_version": 1,
    "plane": "action",
    "run_id": "11111111-1111-1111-1111-111111111111",
    "project": {"id": "22222222-2222-2222-2222-222222222222", "name": "kampadmin"},
    "tenant": {"id": "3", "slug": "solhi", "scope_value": "org-42"},
    "kind": None,
    "mode": None,
    "scenario": None,
    "surface": None,
    "channel": None,
    "origin": None,
    "simulation": None,
    "principal": {"kind": "kampadmin_person", "external_id": "p-88", "asserted_by": None, "assurance": None},
    "session_id": None,
    "thread_id": None,
    "brain_ref": None,
    "action": {"id": "cancel_registration", "action_run_id": "44444444-4444-4444-4444-444444444444"},
}


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    # The real env vars leak in from a dev shell otherwise and silently backstop every "absent" case.
    monkeypatch.delenv(runctx.ENV_VAR, raising=False)
    monkeypatch.delenv("RC_PRINCIPAL_SCOPED", raising=False)
    runctx._cache.clear()
    yield
    runctx._cache.clear()


def _point_at(monkeypatch, tmp_path: Path, payload: str | None) -> Path:
    path = tmp_path / "run_context.json"
    if payload is not None:
        path.write_text(payload, encoding="utf-8")
    monkeypatch.setenv("RC_RUN_CONTEXT_PATH", str(path))
    return path


def test_principal_scoped_chat_run(monkeypatch, tmp_path: Path) -> None:
    _point_at(monkeypatch, tmp_path, json.dumps(CHAT_RUN))
    assert runctx.source() == "file"
    assert runctx.plane() == "run"
    assert runctx.surface() == "chat"
    assert runctx.is_simulation() is False
    assert runctx.is_principal_scoped() is True
    assert runctx.get("principal.kind") == "kampadmin_person"
    assert runctx.get("tenant.scope_value") == "org-42"
    assert runctx.get("brain_ref.sha") == "abc123def"
    # An explicit null is "absent", not a value to hand back.
    assert runctx.get("channel") is None
    assert runctx.get("channel", "google") == "google"
    assert runctx.get("brain_ref.requested", "main") == "main"


def test_action_container_reads_the_env_twin(monkeypatch, tmp_path: Path) -> None:
    # An action/preflight container mounts the raw clone: no /brain/run_context.json, env only.
    _point_at(monkeypatch, tmp_path, None)
    monkeypatch.setenv(runctx.ENV_VAR, json.dumps(ACTION_PLANE))
    assert runctx.source() == "env"
    assert runctx.plane() == runctx.PLANE_ACTION
    # Every ingress field is null there — that IS the "I am not the run" signal.
    assert runctx.surface() is None
    assert runctx.is_simulation() is False
    assert runctx.get("action.id") == "cancel_registration"
    # The identity core survives the proposal; asserted_by/assurance do not.
    assert runctx.is_principal_scoped() is True
    assert runctx.get("principal.asserted_by") is None


def test_file_wins_over_the_env_twin(monkeypatch, tmp_path: Path) -> None:
    _point_at(monkeypatch, tmp_path, json.dumps(CHAT_RUN))
    monkeypatch.setenv(runctx.ENV_VAR, json.dumps(ACTION_PLANE))
    assert runctx.source() == "file"
    assert runctx.plane() == "run"


def test_simulation_run(monkeypatch, tmp_path: Path) -> None:
    doc = dict(CHAT_RUN, kind="prompt", mode="prompt", surface="prompt_api", simulation=True)
    _point_at(monkeypatch, tmp_path, json.dumps(doc))
    assert runctx.is_simulation() is True
    assert runctx.surface() == "prompt_api"


def test_no_document_degrades_but_keeps_the_legacy_scoped_env(monkeypatch, tmp_path: Path) -> None:
    _point_at(monkeypatch, tmp_path, None)
    assert runctx.context() == {}
    assert runctx.source() == "none"
    assert runctx.plane() is None
    assert runctx.is_principal_scoped() is False
    # A host too old to stamp the document still sets the legacy marker.
    monkeypatch.setenv("RC_PRINCIPAL_SCOPED", "1")
    assert runctx.is_principal_scoped() is True


def test_document_without_principal_does_not_fall_back_to_the_legacy_env(monkeypatch, tmp_path: Path) -> None:
    # The document is authoritative once present: a stale RC_PRINCIPAL_SCOPED must not resurrect scoping.
    _point_at(monkeypatch, tmp_path, json.dumps(dict(CHAT_RUN, principal=None)))
    monkeypatch.setenv("RC_PRINCIPAL_SCOPED", "1")
    assert runctx.is_principal_scoped() is False


def test_malformed_json_raises_instead_of_guessing(monkeypatch, tmp_path: Path) -> None:
    _point_at(monkeypatch, tmp_path, "{not json")
    with pytest.raises(runctx.RunContextError, match="not valid JSON"):
        runctx.context()


def test_non_object_document_raises(monkeypatch, tmp_path: Path) -> None:
    _point_at(monkeypatch, tmp_path, "[1, 2]")
    with pytest.raises(runctx.RunContextError, match="must be a JSON object"):
        runctx.plane()


def test_cli_prints_the_document_and_one_field(monkeypatch, tmp_path: Path, capsys) -> None:
    _point_at(monkeypatch, tmp_path, json.dumps(CHAT_RUN))
    assert runctx._main([]) == 0
    assert json.loads(capsys.readouterr().out)["surface"] == "chat"

    runctx._cache.clear()
    assert runctx._main(["get", "principal.kind"]) == 0
    assert capsys.readouterr().out.strip() == "kampadmin_person"

    runctx._cache.clear()
    assert runctx._main(["get", "channel"]) == 1
