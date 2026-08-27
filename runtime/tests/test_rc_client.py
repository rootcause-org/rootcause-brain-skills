from __future__ import annotations

import json
import subprocess

import pytest

from lib import rc_client


def _runner(*, payload=None, returncode=0, stderr="", seen=None):
    def run(args, timeout):
        if seen is not None:
            seen.extend([list(args), timeout])
        return subprocess.CompletedProcess(args, returncode, json.dumps(payload or {}), stderr)

    return run


def test_query_builds_machine_safe_command_and_preserves_array_rows(monkeypatch):
    seen = []
    client = rc_client.Client(runner=_runner(payload={"columns": ["id", "id"], "rows": [[1, 2]], "truncated": False}, seen=seen))

    result = client.query("select * from things where name = %s", {"name": "A,B"}, all=True, database="billing")

    assert result == rc_client.Result(columns=["id", "id"], rows=[[1, 2]])
    assert seen[0] == [
        "rc", "-o", "json", "dev", "console", "database", "query", "billing",
        "select * from things where name = %s", "--all", "--format", "json", "--out", "-",
        "--param", "name=A,B",
    ]


def test_query_uses_configured_default_database(monkeypatch):
    seen = []
    monkeypatch.setenv("RC_CONSOLE_DATABASE", "warehouse")
    client = rc_client.Client(runner=_runner(payload={"columns": [], "rows": [], "truncated": False}, seen=seen))

    client.query("select 1")

    assert seen[0][7] == "warehouse"


def test_query_raises_for_truncation_unless_explicitly_allowed():
    client = rc_client.Client(runner=_runner(payload={"columns": ["id"], "rows": [[1]], "truncated": True}))

    with pytest.raises(rc_client.TruncatedError, match="all=True"):
        client.query("select 1")
    assert client.query("select 1", allow_truncated=True).truncated is True


def test_exit_codes_map_to_typed_errors():
    client = rc_client.Client(runner=_runner(payload={"error": {"message": "login expired", "status": 401}}, returncode=2))

    with pytest.raises(rc_client.AuthenticationError) as excinfo:
        client.query("select 1")
    assert excinfo.value.status == 401


def test_query_to_csv_keeps_duplicate_column_headers(tmp_path):
    client = rc_client.Client(runner=_runner(payload={"columns": ["id", "id"], "rows": [[1, 2]], "truncated": False}))
    path = tmp_path / "rows.csv"

    result = client.query_to_csv("select 1", path)

    assert result.rows == [[1, 2]]
    assert path.read_text() == "id,id\n1,2\n"


def test_bash_returns_typed_result_and_passes_timeout():
    seen = []
    client = rc_client.Client(runner=_runner(payload={"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False}, seen=seen))

    exit_code, stdout, stderr = client.bash("echo ok", timeout=30)

    assert (exit_code, stdout, stderr) == (0, "ok", "")
    assert seen[0][-4:] == ["--out", "-", "--timeout", "30"]
    assert seen[1] == 30


def test_file_get_returns_destination_after_cli_confirmation(tmp_path):
    seen = []
    destination = tmp_path / "result.csv"
    client = rc_client.Client(runner=_runner(payload={"path": str(destination)}, seen=seen))

    assert client.file_get("/tmp/rootcause-out/rows.csv", destination) == destination
    assert seen[0][-5:] == ["file", "get", "/tmp/rootcause-out/rows.csv", "--out", str(destination)]
