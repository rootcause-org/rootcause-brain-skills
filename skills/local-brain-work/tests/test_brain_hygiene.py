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
    "description": {"skills/cases/x.md": f"---\nname: x\ndescription: \"{'w' * 151}\"\n---\nBody.\n"},
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


def test_staged_mode_judges_only_the_index(tmp_path: Path) -> None:
    """What the managed pre-commit hook runs: a staged bad file fails, an unstaged one is ignored."""
    root = make_brain(tmp_path, {})
    (root / "a.md").write_text("Run /Users/pj/x first.\n", "utf-8")
    (root / "b.md").write_text("Run /Users/pj/y first.\n", "utf-8")
    git(root, "add", "a.md")
    run = subprocess.run([sys.executable, str(SCRIPTS / "brain_hygiene.py"), "--staged",
                          "--root", str(root)], capture_output=True, text=True)
    assert run.returncode == 1
    assert "a.md:1: hygiene: abs-path" in run.stdout and "b.md" not in run.stdout


# --- chat-inspiration --------------------------------------------------------------------------

GOOD_INSPIRATION = """---
surfaces: [chat, dashboard_chat]
---
## 📝 Inschrijvingen
- **Wachtlijst** — Welke kampen hebben een wachtlijst?
- **Gezocht** - Zoek de inschrijvingen van [naam].
## 🧑‍🏫 Monitoren {#monitoren}
- **Dichtbij:** Geef me monitoren die in de buurt van [stad] wonen.
* **Rollen**: Welke rollen hebben we?
"""
HEADER = "---\nsurfaces: [chat, dashboard_chat]\n---\n"
REAL_BRAINS = Path(__file__).resolve().parents[4]


def ci(text: str) -> tuple[set[str], set[str]]:
    """(ERROR messages, NOTICE messages) of parse_chat_inspiration, rule prefix stripped."""
    _, found = brain_hygiene.parse_chat_inspiration(text)
    msgs = {sev: {f.message.removeprefix("chat-inspiration: ") for f in found if f.severity == sev}
            for sev in ("ERROR", "NOTICE")}
    return msgs["ERROR"], msgs["NOTICE"]


def test_chat_inspiration_good_file_parses_clean() -> None:
    cats, found = brain_hygiene.parse_chat_inspiration(GOOD_INSPIRATION)
    assert found == []
    assert [c["id"] for c in cats] == ["inschrijvingen", "monitoren"]
    assert [p["title"] for c in cats for p in c["prompts"]] == ["Wachtlijst", "Gezocht", "Dichtbij", "Rollen"]
    assert cats[1]["prompts"][0]["placeholders"] == ["[stad]"]


@pytest.mark.parametrize("text, needle", [
    ("---\ndescription: x\n---\n## A\n- **T** — p\n", "no frontmatter `surfaces:`"),
    ("## A\n- **T** — p\n", "no frontmatter `surfaces:`"),
    ("---\nsurfaces: [dashboard_chat]\n---\n## A\n- **T** — p\n", "lacks `chat`"),
    (HEADER + "Just prose.\n", "no prompt parsed"),
    (HEADER + "## A\n", "no prompt parsed"),
    (HEADER + "## A\n- **T** — p\n- **Empty** —\n", "bullet yields no prompt"),
    (HEADER + "## A\n- **T** — " + "x" * 501 + "\n", "cap 500"),
    (HEADER + "## A\n- **" + "t" * 81 + "** — p\n", "cap 80"),
    (HEADER + "".join(f"## Cat {i}\n- **T** — p{i}\n" for i in range(21)), "20-category cap"),
    (HEADER + "## A\n" + "".join(f"- **T{i}** — p{i}\n" for i in range(51)), "50-prompt cap"),
    (HEADER + "## A\n- **T** — p\n## B {#a}\n- **U** — q\n", "duplicate category id 'a'"),
    (HEADER + "## A\n- **T** — Monitoren in [stad\n", "unbalanced or invalid"),
    (HEADER + "## A\n- **T** — Monitoren in [a [b]]\n", "unbalanced or invalid"),
    (HEADER + "## 📝\n- **T** — p\n## B\n- **U** — q\n", "heading yields no category"),
])
def test_chat_inspiration_fails(text: str, needle: str) -> None:
    errors, _ = ci(text)
    assert any(needle in m for m in errors), errors


@pytest.mark.parametrize("text, needle", [
    (HEADER + "## A\n- **T** — " + "x" * 161 + "\n", "cards read best under 160"),
    (HEADER + "## A\n- Welke kampen hebben een wachtlijst?\n", "no bold title"),
    (HEADER + "## A\n- **T** — p\n## B\n", "category 'b' has no prompts"),
    (HEADER + "## A\n- **T** — Same text\n- **U** — same TEXT\n", "duplicate prompt text"),
    (HEADER + "## A\n- **T** — p\n### Sub\n- **U** — q\n", "under a sub-heading"),
])
def test_chat_inspiration_warns_without_blocking(text: str, needle: str) -> None:
    errors, notices = ci(text)
    assert errors == set()
    assert any(needle in m for m in notices), notices


def test_chat_inspiration_long_title_words_are_fine() -> None:
    assert ci(HEADER + "## A\n- **One two three four five six** — p\n") == (set(), set())


def test_chat_inspiration_runs_in_check_scoped_to_root_file(tmp_path: Path) -> None:
    bad = HEADER + "## A\n- **T** — Monitoren in [stad\n"
    root = make_brain(tmp_path, {"chat_inspiration.md": bad, "docs/chat_inspiration.md": bad})
    found = [f for f in brain_hygiene.check(root) if "chat-inspiration" in f.message]
    assert [(f.path, f.line) for f in found] == [("chat_inspiration.md", 5)]
    assert rules(root, files=[]) == set()  # changed-file scoped like the other rules


@pytest.mark.parametrize("brain", ["kampadmin", "dentai", "pro-backup"])
def test_real_brain_galleries_have_no_fail(brain: str) -> None:
    path = REAL_BRAINS / f"rootcause-brain-{brain}" / "chat_inspiration.md"
    if not path.is_file():
        pytest.skip(f"{path} not checked out")
    cats, found = brain_hygiene.parse_chat_inspiration(path.read_text("utf-8"))
    assert cats and [f.render() for f in found if f.severity != "NOTICE"] == []
