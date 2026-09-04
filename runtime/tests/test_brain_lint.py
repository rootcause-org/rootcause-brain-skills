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


def test_lint_brain_validates_action_surfaces_in_live_and_draft_manifests(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "actions/refund/manifest.yaml",
           "id: refund\ndescription: Refund a customer\nsurfaces: [gmail, dashboard_chat]\n")
    _write(tmp_path / "actions-drafts/bad/manifest.yaml",
           "id: bad\ndescription: Bad draft\nsurfaces: [email]\n")
    _write(tmp_path / "actions/bad_shape/manifest.yaml",
           "id: bad_shape\ndescription: Bad shape\nsurfaces: gmail\n")
    _write(tmp_path / "actions/empty/manifest.yaml",
           "id: empty\ndescription: Empty\nsurfaces: []\n")
    _write(tmp_path / "actions/null/manifest.yaml",
           "id: null\ndescription: Null\nsurfaces: null\n")

    findings = [f for f in _fails(lint_brain(tmp_path)) if f.rule == "action-surfaces"]

    assert [f.path for f in findings] == [
        "actions/bad_shape/manifest.yaml",
        "actions/empty/manifest.yaml",
        "actions/null/manifest.yaml",
        "actions-drafts/bad/manifest.yaml",
    ]
    assert "must be a list" in findings[0].message
    assert "omit it to allow all" in findings[1].message
    assert "must be a list" in findings[2].message
    assert "unknown action surface 'email'" in findings[3].message


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


def test_md_description_length_boundary(tmp_path: Path) -> None:
    # The cap mirrors bootstrap.go's descMaxLen: exactly at the cap renders, one over truncates.
    assert DESC_MAX_LEN == 150
    _seed_brain(tmp_path)
    _write(tmp_path / "skills/cases/atcap.md", f"---\ndescription: {'x' * DESC_MAX_LEN}\n---\n")
    _write(tmp_path / "skills/cases/overcap.md", f"---\ndescription: {'x' * (DESC_MAX_LEN + 1)}\n---\n")

    fails = {f.path for f in _fails(lint_brain(tmp_path))}
    assert "skills/cases/atcap.md" not in fails
    assert "skills/cases/overcap.md" in fails


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


def _git_brain(root: Path) -> None:
    """A real git repo so the symlink lint can consult the index, not just the working tree."""
    _seed_brain(root)
    _write(root / ".gitignore", ".agents/\n")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)


def _symlink_findings(root: Path, level: str) -> list[Finding]:
    picker = _fails if level == "FAIL" else _warns
    return [f for f in picker(lint_brain(root)) if f.rule == "symlink-broken"]


def test_lint_brain_warns_on_dangling_alias_symlink(tmp_path: Path) -> None:
    # The supported layout: a COMMITTED .claude/skills -> ../.agents/skills alias over a gitignored
    # local kit install. It dangles in a fresh checkout; the host skips it, so this is a WARN.
    _git_brain(tmp_path)
    _write(tmp_path / ".agents/skills/local/SKILL.md", "---\ndescription: local\n---\n")
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude/skills").symlink_to("../.agents/skills")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", ".claude/skills"], check=True)

    assert _symlink_findings(tmp_path, "FAIL") == []
    warns = _symlink_findings(tmp_path, "WARN")
    assert [f.path for f in warns] == [".claude/skills"]
    assert "not tracked" in warns[0].message
    assert "dangling in checkouts" in warns[0].message


def test_lint_brain_allows_tracked_symlink_to_tracked_dir(tmp_path: Path) -> None:
    _git_brain(tmp_path)
    (tmp_path / "shortcuts").mkdir()
    (tmp_path / "shortcuts/cases").symlink_to("../skills/cases")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)

    assert _symlink_findings(tmp_path, "FAIL") == []
    assert _symlink_findings(tmp_path, "WARN") == []


def test_lint_brain_fails_absolute_symlink(tmp_path: Path) -> None:
    _git_brain(tmp_path)
    (tmp_path / "abs").symlink_to("/etc/hosts")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)

    findings = _symlink_findings(tmp_path, "FAIL")

    assert [f.path for f in findings] == ["abs"]
    assert "absolute" in findings[0].message


def test_lint_brain_fails_symlink_escaping_root(tmp_path: Path) -> None:
    _git_brain(tmp_path)
    (tmp_path / "outside").symlink_to("../elsewhere")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)

    findings = _symlink_findings(tmp_path, "FAIL")

    assert [f.path for f in findings] == ["outside"]
    assert "escapes the brain root" in findings[0].message


def test_lint_brain_without_git_falls_back_to_disk_existence(tmp_path: Path) -> None:
    _seed_brain(tmp_path)  # no .git → existence check only
    (tmp_path / "dangling").symlink_to("skills/missing")
    (tmp_path / "ok").symlink_to("skills/cases")

    assert _symlink_findings(tmp_path, "FAIL") == []
    warns = _symlink_findings(tmp_path, "WARN")

    assert [f.path for f in warns] == ["dangling"]
    assert "does not exist" in warns[0].message
