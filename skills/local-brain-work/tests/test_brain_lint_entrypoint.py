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


def test_entrypoint_accepts_all_and_explicit_paths(tmp_path: Path) -> None:
    """brain_structure.py drives this entrypoint as `--all [--strict]` and `[--strict] <path>…`."""
    brain = _brain(tmp_path, "_DEAD = 1\n")
    command = [sys.executable, str(SCRIPT), "--brain", str(brain)]

    whole = subprocess.run([*command, "--all"], text=True, capture_output=True, check=False)
    scoped = subprocess.run([*command, "actions/example/script.py"],
                            text=True, capture_output=True, check=False)
    elsewhere = subprocess.run([*command, "actions/example/manifest.yaml", "notes/thing.txt"],
                               text=True, capture_output=True, check=False)

    assert whole.returncode == 0 and "WARN dead private names (1)" in whole.stdout
    assert scoped.returncode == 0 and "WARN dead private names (1)" in scoped.stdout
    assert elsewhere.returncode == 0 and elsewhere.stdout.strip() == "brain lint: clean"


def test_entrypoint_skips_run_hidden_paths(tmp_path: Path) -> None:
    """`.agents/` & co never reach a run, so their (machine-local) symlinks are not brain findings."""
    brain = _brain(tmp_path, "x = 1\n")
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "Test"]):
        subprocess.run(["git", "-C", str(brain), *args], check=True, capture_output=True)
    (brain / ".replypenignore").write_text("/.agents/\n", "utf-8")
    (brain / ".agents").mkdir()
    (brain / ".agents" / "docs").symlink_to("/opt/kit/docs")
    subprocess.run(["git", "-C", str(brain), "add", "-A", "-f"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(brain), "commit", "-qm", "x"], check=True, capture_output=True)

    run = subprocess.run([sys.executable, str(SCRIPT), "--brain", str(brain), "--all"],
                         text=True, capture_output=True, check=False)

    assert run.returncode == 0, run.stdout + run.stderr
    assert "symlink" not in run.stdout
