"""Unit tests for the offline brain description lint (lib.brain_lint)."""

from __future__ import annotations

import sys
import types
import os
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.brain_lint import (
    DESC_MAX_LEN,
    Finding,
    _md_description,
    _manifest_description,
    format_report,
    lint_brain,
)


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, "utf-8")
    return p


def _fails(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == "FAIL"]


def _warns(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == "WARN"]


def test_md_description_variants(tmp_path: Path) -> None:
    good = _write(tmp_path / "good.md", "---\ndescription: When a backup job fails\n---\n# X\n")
    assert _md_description(good) == "When a backup job fails"

    quoted = _write(tmp_path / "q.md", "---\ndescription: \"Open for login errors\"\n---\n")
    assert _md_description(quoted) == "Open for login errors"

    collapsed = _write(tmp_path / "c.md", "---\ndescription:   lots   of\tspace  \n---\n")
    assert _md_description(collapsed) == "lots of space"

    # block scalar / empty / missing frontmatter / no key all render nothing → None
    assert _md_description(_write(tmp_path / "b.md", "---\ndescription: |\n  multi\n---\n")) is None
    assert _md_description(_write(tmp_path / "e.md", "---\ndescription:\n---\n")) is None
    assert _md_description(_write(tmp_path / "n.md", "# no frontmatter\n")) is None
    assert _md_description(_write(tmp_path / "k.md", "---\nname: foo\n---\n")) is None


def test_manifest_description(tmp_path: Path) -> None:
    m = _write(tmp_path / "manifest.yaml", "id: refund\ndescription: >-\n  Refund a customer\n")
    assert _manifest_description(m) == "Refund a customer"
    assert _manifest_description(_write(tmp_path / "m2.yaml", "id: x\n")) is None
    assert _manifest_description(_write(tmp_path / "m3.yaml", "id: x\ndescription: '  '\n")) is None


def _seed_brain(root: Path) -> None:
    _write(root / "skills/backups/SKILL.md", "---\ndescription: When a backup job fails\n---\n")
    _write(root / "skills/cases/login.md", "---\ndescription: Customer cannot sign in\n---\n")
    _write(root / "actions/refund/manifest.yaml", "id: refund\ndescription: Refund a customer\n")


def test_lint_brain_all_good(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    assert _fails(lint_brain(tmp_path)) == []


def test_lint_brain_flags_python_outside_supported_roots(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "notes/faq/scripts/generate_index.py", "print('index')\n")
    _write(tmp_path / "helper.py", "print('helper')\n")

    findings = [f for f in _fails(lint_brain(tmp_path)) if f.rule == "script-outside-skills"]

    assert [f.path for f in findings] == ["helper.py", "notes/faq/scripts/generate_index.py"]
    assert all("skills/<topic>/scripts/" in f.message for f in findings)
    assert all("import smoke" in f.message for f in findings)


def test_lint_brain_allows_python_in_supported_and_ignored_dirs(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    allowed = [
        "conftest.py",
        "skills/faq/scripts/generate_index.py",
        "actions/refund/script.py",
        "tests/test_faq.py",
        ".agents/skills/local/scripts/helper.py",
        ".claude/skills/local/scripts/helper.py",
        ".rootcause/cache/helper.py",
        ".git/hooks/helper.py",
        ".venv/lib/helper.py",
        "_internal/tools/helper.py",
        "node_modules/package/helper.py",
    ]
    for rel in allowed:
        _write(tmp_path / rel, "# allowed\n")

    assert not any(f.rule == "script-outside-skills" for f in lint_brain(tmp_path))


def test_lint_brain_survives_brain_test_replacing_lib_module(tmp_path: Path, monkeypatch) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "actions/refund/script.py", "_DEAD = 1\n")
    monkeypatch.setitem(sys.modules, "lib", types.SimpleNamespace())

    assert any("_DEAD" in f.message for f in lint_brain(tmp_path))


def test_lint_brain_flags_missing_and_overlong(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "skills/nodesc/SKILL.md", "# no frontmatter here\n")
    long = "x" * (DESC_MAX_LEN + 5)
    _write(tmp_path / "skills/cases/toolong.md", f"---\ndescription: {long}\n---\n")
    _write(tmp_path / "actions/broken/manifest.yaml", "id: broken\n")

    fails = _fails(lint_brain(tmp_path))
    paths = {f.path for f in fails}
    assert "skills/nodesc/SKILL.md" in paths
    assert "skills/cases/toolong.md" in paths
    assert "actions/broken/manifest.yaml" in paths
    assert all("skills/backups/SKILL.md" != f.path for f in fails)


def test_lint_brain_overlong_manifest_warns_not_fails(tmp_path: Path) -> None:
    # Manifest descriptions double as full-length action-catalog copy: overlong is WARN, never FAIL.
    _seed_brain(tmp_path)
    long = "when a refund is due " * 10
    _write(tmp_path / "actions/verbose/manifest.yaml", f"id: verbose\ndescription: {long.strip()}\n")

    findings = lint_brain(tmp_path)
    assert all(f.path != "actions/verbose/manifest.yaml" for f in _fails(findings))
    assert any(f.path == "actions/verbose/manifest.yaml" and "short when-to-use sentence" in f.message
               for f in _warns(findings))


def test_lint_brain_accepts_rich_manifest_with_frontloaded_sentence(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    rich = "Refund a settled duplicate charge. " + ("Detailed safety and verification copy. " * 8)
    _write(tmp_path / "actions/verbose/manifest.yaml", f"id: verbose\ndescription: {rich.strip()}\n")

    findings = lint_brain(tmp_path)
    assert all(f.path != "actions/verbose/manifest.yaml" for f in findings)


def test_lint_brain_warns_on_contains_style(tmp_path: Path) -> None:
    _write(tmp_path / "skills/x/SKILL.md",
           "---\ndescription: This file contains the backup schema\n---\n")
    findings = lint_brain(tmp_path)
    assert _fails(findings) == []  # style is WARN, not FAIL
    assert any("x/SKILL.md" in w.path for w in _warns(findings))


def test_format_report_groups_fail_before_warn_by_rule() -> None:
    report = format_report([
        Finding("actions/b/script.py:2", "WARN", "dead b", "private-dead"),
        Finding("skills/x/SKILL.md", "FAIL", "missing", "description-missing"),
        Finding("actions/a/script.py:1", "WARN", "dead a", "private-dead"),
        Finding("actions/", "WARN", "duplicate", "helper-duplicate"),
    ])

    assert report.splitlines() == [
        "brain lint: 1 FAIL, 3 WARN",
        "FAIL missing descriptions (1)",
        "  skills/x/SKILL.md — missing",
        "WARN dead private names (2)",
        "  actions/a/script.py:1 — dead a",
        "  actions/b/script.py:2 — dead b",
        "WARN duplicate helpers (1)",
        "  actions/ — duplicate",
    ]


def test_pytest_adapter_prints_one_compact_block_without_warning_summary(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "actions/refund/script.py", "_DEAD = 1\n")
    _write(tmp_path / "skills/test_sample.py", "def test_ok():\n    assert True\n")
    runtime = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(runtime)}

    run = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path / "skills"), "-q", "-p", "no:cacheprovider",
         "-p", "lib.brain_lint_pytest"],
        text=True, capture_output=True, check=False, env=env,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.count("brain lint: 0 FAIL, 1 WARN") == 1
    assert "WARN dead private names (1)" in run.stdout
    assert "warnings summary" not in run.stdout


def test_pytest_adapter_surfaces_script_outside_skills(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "notes/faq/scripts/generate_index.py", "print('index')\n")
    _write(tmp_path / "skills/test_sample.py", "def test_ok():\n    assert True\n")
    runtime = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(runtime)}

    run = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path / "skills"), "-q", "-p", "no:cacheprovider",
         "-p", "lib.brain_lint_pytest"],
        text=True, capture_output=True, check=False, env=env,
    )

    assert run.returncode == 1
    assert "FAIL scripts outside skills (1)" in run.stdout
    assert "notes/faq/scripts/generate_index.py" in run.stdout
