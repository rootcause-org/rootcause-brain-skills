from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "brain_env.py"
SPEC = importlib.util.spec_from_file_location("brain_env", SCRIPT)
assert SPEC and SPEC.loader
E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E)


def _write_probe(root: Path, value: str) -> None:
    package = root / "lib"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "runtime_probe.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")


def _probe(child_env: dict[str, str]) -> str:
    result = subprocess.run(
        [sys.executable, "-c", "from lib.runtime_probe import VALUE; print(VALUE)"],
        env=child_env,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def test_uv_child_sees_local_runtime_source_edits_ahead_of_stale_install(
    tmp_path: Path, monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    stale_install = tmp_path / "site-packages"
    brain_scripts = tmp_path / "brain" / "skills"
    _write_probe(runtime, "source-before")
    _write_probe(stale_install, "stale-wheel")
    monkeypatch.setattr(E, "RUNTIME", runtime)

    child = E.uv_child_env(
        {"PYTHONPATH": str(stale_install)}, [brain_scripts], mirrors_root=None,
    )

    assert child["PYTHONPATH"].split(os.pathsep) == [
        str(brain_scripts), str(runtime), str(stale_install),
    ]
    assert _probe(child) == "source-before"

    _write_probe(runtime, "source-after-edit")
    assert _probe(child) == "source-after-edit"


def test_uv_child_does_not_shadow_explicit_runtime_override(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    _write_probe(runtime, "local-source")
    monkeypatch.setattr(E, "RUNTIME", runtime)
    monkeypatch.setenv("RC_RUNTIME_SPEC", "rootcause-runtime==99")

    child = E.uv_child_env({}, [], mirrors_root=None)

    assert str(runtime) not in child.get("PYTHONPATH", "").split(os.pathsep)
