"""Unit tests for the offline brain description lint (lib.brain_lint)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.brain_lint import (
    DESC_MAX_LEN,
    Finding,
    _md_description,
    _manifest_description,
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
    assert any(f.path == "actions/verbose/manifest.yaml" and "front-load" in f.message
               for f in _warns(findings))


def test_lint_brain_warns_on_contains_style(tmp_path: Path) -> None:
    _write(tmp_path / "skills/x/SKILL.md",
           "---\ndescription: This file contains the backup schema\n---\n")
    findings = lint_brain(tmp_path)
    assert _fails(findings) == []  # style is WARN, not FAIL
    assert any("x/SKILL.md" in w.path for w in _warns(findings))
