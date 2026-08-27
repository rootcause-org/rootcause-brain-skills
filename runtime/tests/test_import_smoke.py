from __future__ import annotations

from pathlib import Path

from lib import import_smoke


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_discover_scope_and_opt_out(tmp_path: Path) -> None:
    _write(tmp_path, "skills/demo/scripts/good.py", "VALUE = 1\n")
    _write(tmp_path, "skills/demo/lib/helper.py", "VALUE = 2\n")
    _write(tmp_path, "skills/demo/scripts/test_ignored.py", "raise RuntimeError\n")
    _write(tmp_path, "skills/demo/tests/scripts/ignored.py", "raise RuntimeError\n")
    _write(tmp_path, "skills/demo/actions/scripts/ignored.py", "raise RuntimeError\n")
    _write(tmp_path, "skills/demo/scripts/opted.py", "# rc: no-import-smoke\nraise RuntimeError\n")

    assert [p.name for p in import_smoke.discover(tmp_path)] == ["helper.py", "good.py"]


def test_run_isolates_modules_and_reports_traceback_tail(tmp_path: Path, capsys) -> None:
    _write(tmp_path, "skills/demo/scripts/first.py", "import builtins\nbuiltins._smoke_leak = 1\n")
    _write(
        tmp_path,
        "skills/demo/scripts/second.py",
        "import builtins, os\n"
        "assert not hasattr(builtins, '_smoke_leak')\n"
        "assert os.environ['RC_LOCAL_BRAIN_RUN'] == '1'\n",
    )
    _write(tmp_path, "skills/demo/lib/broken.py", "raise ModuleNotFoundError('missing demo')\n")

    assert import_smoke.run(tmp_path) == 1
    captured = capsys.readouterr()
    assert "import smoke FAIL skills/demo/lib/broken.py: ModuleNotFoundError: missing demo" in captured.err
    assert "checked=3 failed=1" in captured.out


def test_run_preserves_package_context_for_relative_imports(tmp_path: Path) -> None:
    _write(tmp_path, "skills/demo/lib/__init__.py", "\n")
    _write(tmp_path, "skills/demo/lib/helper.py", "VALUE = 42\n")
    _write(tmp_path, "skills/demo/lib/consumer.py", "from .helper import VALUE\nassert VALUE == 42\n")

    assert import_smoke.run(tmp_path) == 0
