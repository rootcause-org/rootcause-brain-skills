"""Unit tests for the offline action-script hygiene lint (lib.action_lint)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.action_lint import SIZE_FAIL_KB, SIZE_WARN_KB, lint_actions
from lib.brain_lint import lint_brain

HELPER = "def _url(host, path):\n    return f'https://{host}{path}'\n"


def _script(root: Path, action: str, body: str, name: str = "script.py") -> Path:
    p = root / "actions" / action / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, "utf-8")
    return p


def _generated_script(root: Path, action: str, artifact_body: str, source_body: str) -> Path:
    source_rel = f"actions/{action}/script.src.py"
    _script(root, action, source_body, name="script.src.py")
    banner = ("# GENERATED FILE — DO NOT EDIT.  Run: uv run scripts/bundle_actions.py\n"
              f"# Source: {source_rel}\n")
    return _script(root, action, banner + artifact_body)


def _msgs(findings, level=None):
    return [f.message for f in findings if level is None or f.level == level]


def test_size_budget_warn_fail_and_override(tmp_path: Path) -> None:
    _script(tmp_path, "small", "x = 1\n")
    _script(tmp_path, "big", "# " + "x" * (SIZE_WARN_KB * 1024) + "\n")
    _script(tmp_path, "huge", "# " + "x" * (SIZE_FAIL_KB * 1024) + "\n", name="preflight.py")
    findings = lint_actions(tmp_path)
    assert [f.path for f in findings if f.level == "FAIL"] == ["actions/huge/preflight.py"]
    assert [f.path for f in findings if f.level == "WARN"] == ["actions/big/script.py"]
    assert "argv" in findings[0].message

    (tmp_path / "actions/lint.yaml").write_text("script_size:\n  warn_kb: 200\n  fail_kb: 300\n")
    assert lint_actions(tmp_path) == []


def test_duplicate_helpers_identical_vs_drifted(tmp_path: Path) -> None:
    # Identical modulo docstring/whitespace/comments; public `main` is an entrypoint, never flagged.
    _script(tmp_path, "a", HELPER + "def main():\n    _url('h', '/')\n")
    _script(tmp_path, "b", "def _url(host, path):\n    '''doc'''\n    # comment\n    return f'https://{host}{path}'\n"
            "def main():\n    return _url('h', '/')\n")
    _script(tmp_path, "c", HELPER + "def main():\n    pass\n    _url\n")
    msgs = _msgs(lint_actions(tmp_path), "WARN")
    assert any("`_url` is identical in 3 scripts" in m for m in msgs)
    assert not any("`main`" in m for m in msgs)

    # Two copies only → below threshold, silent.
    _script(tmp_path, "c", "def _cid():\n    return 1\ndef main():\n    _cid()\n")
    _script(tmp_path, "b", "def _cid():\n    return 2\ndef main():\n    _cid()\n")
    assert not any("_cid" in m for m in _msgs(lint_actions(tmp_path)))

    # Third copy with a different body → drifted.
    _script(tmp_path, "a", "def _cid():\n    return 1\ndef main():\n    _cid()\n")
    msgs = _msgs(lint_actions(tmp_path), "WARN")
    assert any("`_cid` exists in 3 scripts" in m and "2 different bodies" in m for m in msgs)


def test_generated_bundle_helpers_are_linted_in_authoritative_sources(tmp_path: Path) -> None:
    # Repeated transport copies are unavoidable and ignored only when a strict generated banner
    # resolves to the exact adjacent source. Real duplication in those sources remains actionable.
    for action in ("a", "b", "c"):
        _generated_script(tmp_path, action, HELPER, "def run():\n    return 1\n")
    assert not any("_url" in m for m in _msgs(lint_actions(tmp_path)))

    for action in ("a", "b", "c"):
        _script(tmp_path, action, HELPER + "def run():\n    return _url('h', '/')\n",
                name="script.src.py")
    findings = lint_actions(tmp_path)
    assert any("`_url` is identical in 3 scripts" in m for m in _msgs(findings))
    assert sum(f.rule == "helper-duplicate" for f in findings) == 1


def test_generated_marker_without_exact_adjacent_source_is_not_trusted(tmp_path: Path) -> None:
    banner = ("# GENERATED FILE — DO NOT EDIT.\n"
              "# Source: actions/somewhere_else/script.src.py\n")
    for action in ("a", "b", "c"):
        _script(tmp_path, action, banner + HELPER + "def main():\n    return _url('h', '/')\n")
    assert any("`_url` is identical in 3 scripts" in m for m in _msgs(lint_actions(tmp_path)))


def test_dead_private_names(tmp_path: Path) -> None:
    _script(tmp_path, "a",
            "_USED = 1\n_DEAD = 2\n__all__ = []\n_VIA_STR = 3\n"
            "def _dead_fn():\n    pass\n"
            "def _used_fn():\n    return _USED\n"
            "def main():\n    _used_fn(); getattr(main, '_VIA_STR')\n"
            "public_unused = 1\n")
    findings = lint_actions(tmp_path)
    assert all(f.level == "WARN" for f in findings)
    flagged = sorted(m.split("`")[1] for m in _msgs(findings))
    assert flagged == ["_DEAD", "_dead_fn"]
    assert findings[0].path == "actions/a/script.py:2"


def test_unparsable_script_only_size_checked(tmp_path: Path) -> None:
    _script(tmp_path, "a", "def (:\n")
    assert lint_actions(tmp_path) == []


def test_brain_lint_includes_action_findings(tmp_path: Path) -> None:
    _script(tmp_path, "a", "_DEAD = 1\n")
    (tmp_path / "actions/a/manifest.yaml").write_text("id: a\ndescription: Do a\n")
    assert any("_DEAD" in f.message for f in lint_brain(tmp_path))
