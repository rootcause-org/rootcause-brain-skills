from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "brain_lint.py"


def _brain(tmp_path: Path, script: str) -> Path:
    (tmp_path / "skills").mkdir()
    action = tmp_path / "actions" / "example"
    action.mkdir(parents=True)
    (action / "manifest.yaml").write_text("id: example\ndescription: Run example\n", "utf-8")
    (action / "script.py").write_text(script, "utf-8")
    return tmp_path


def test_entrypoint_warns_without_failing_unless_strict(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "_DEAD = 1\n")
    command = [sys.executable, str(SCRIPT), "--brain", str(brain)]

    normal = subprocess.run(command, text=True, capture_output=True, check=False)
    strict = subprocess.run([*command, "--strict"], text=True, capture_output=True, check=False)

    assert normal.returncode == 0
    assert strict.returncode == 1
    assert "brain lint: 0 FAIL, 1 WARN" in normal.stdout
    assert "WARN dead private names (1)" in normal.stdout


def test_entrypoint_fails_on_blocking_finding(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "# " + "x" * (96 * 1024) + "\n")

    run = subprocess.run(
        [sys.executable, str(SCRIPT), "--brain", str(brain)],
        text=True, capture_output=True, check=False,
    )

    assert run.returncode == 1
    assert "brain lint: 1 FAIL" in run.stdout
    assert "FAIL script size (1)" in run.stdout


def test_entrypoint_reports_missing_pyyaml_as_dependency_error(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "x = 1\n")

    run = subprocess.run(
        [sys.executable, "-S", str(SCRIPT), "--brain", str(brain)],
        text=True, capture_output=True, check=False,
    )

    assert run.returncode == 1
    assert run.stdout == ""
    assert "error: PyYAML is required" in run.stderr
