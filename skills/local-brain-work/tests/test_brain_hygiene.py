from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SYNC = SCRIPTS.parents[1] / "brain-git-sync" / "scripts" / "brain_git_sync.py"
sys.path.insert(0, str(SCRIPTS))
import brain_hygiene  # noqa: E402

# The 2026-09-25 DentAI failure shape: an unresolved merge committed into a published SKILL.md.
CONFLICTED_SKILL = """---
name: scheduling
description: Plan appointments.
---
<<<<<<< HEAD
Boek maximaal 3 momenten.
=======
Boek maximaal 5 momenten.
>>>>>>> origin/main
"""

FIXTURES = {
    "conflict": {"skills/scheduling/SKILL.md": CONFLICTED_SKILL},
    "placeholder": {"playbooks/a.md": "Bel {{ contact_phone }} of {{ nope }}.\n",
                    "projection.yaml": "placeholders:\n  contact_phone: { type: string }\n"
                                       "templated_globs:\n  - \"playbooks/**/*.md\"\n"},
    "abs-path": {"skills/x/SKILL.md": "Run /Users/pj/code/x.py first.\n"},
    "links": {"AGENTS.md": "See [gone](playbooks/missing.md).\n"},
    "em-dash": {"actions/refund/manifest.yaml": "id: refund\ndescription: Refund\n"
                                                "display_name: \"Terugbetaling — snel\"\n"},
    "mermaid": {"skills/x/SKILL.md": "```mermaid\ngraph TD\n  A-->>>((B\n```\n"},
}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def make_brain(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    for rel, text in {"AGENTS.md": "# Router\n", "skills/.keep": "", **files}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, "utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fixture")
    return root


def rules(root: Path, files: list[str] | None = None) -> set[str]:
    return {f.message.split(":", 1)[0] for f in brain_hygiene.check(root, files)
            if f.severity != "NOTICE"}


def test_clean_brain_passes(tmp_path: Path) -> None:
    root = make_brain(tmp_path, {
        "playbooks/a.md": "Placeholder docs use `{{ key }}` in code. See [router](../AGENTS.md).\n",
        "docs/flow.md": "Tom — plain prose may use em dashes.\n",
        "_internal/notes.md": "/Users/pj/code and {{ nope }} are maintainer-only.\n",
    })
    assert rules(root) == set()


@pytest.mark.parametrize("rule", sorted(FIXTURES))
def test_each_failure_is_rejected(tmp_path: Path, rule: str) -> None:
    root = make_brain(tmp_path, FIXTURES[rule])
    findings = brain_hygiene.check(root)
    if rule == "mermaid" and brain_hygiene._mermaid_renderer() is None:
        assert any("mermaid: skipped" in f.message for f in findings)
        pytest.skip("no mermaid renderer on PATH")
    if rule == "mermaid" and any("renderer failed" in f.message for f in findings):
        pytest.skip("mermaid renderer cannot start here")
    assert rule in rules(root)


def test_changed_scope_keeps_conflicts_whole_tree(tmp_path: Path) -> None:
    root = make_brain(tmp_path, {**FIXTURES["abs-path"], **FIXTURES["conflict"]})
    assert rules(root, files=[]) == {"conflict"}


def test_git_sync_refuses_to_push_conflict_markers(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    brain = make_brain(tmp_path / "brain", {})
    git(brain, "remote", "add", "origin", str(origin))
    git(brain, "push", "-q", "origin", "main")
    before = git(origin, "rev-parse", "main")
    (brain / "skills" / "scheduling").mkdir(parents=True)
    (brain / "skills" / "scheduling" / "SKILL.md").write_text(CONFLICTED_SKILL, "utf-8")
    git(brain, "add", "-A")

    run = subprocess.run([sys.executable, str(SYNC), "--repo", str(brain),
                          "--commit-message", "scheduling: merge"],
                         capture_output=True, text=True)

    assert run.returncode == 2, run.stderr
    assert "brain hygiene gate failed" in run.stderr
    assert "skills/scheduling/SKILL.md:5: hygiene: conflict" in run.stderr
    assert git(origin, "rev-parse", "main") == before
