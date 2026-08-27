"""`lib.mirror_skip`: a collection ModuleNotFoundError explained by the absent /mirrors mount skips."""

from __future__ import annotations

import textwrap

import pytest

from lib import mirror_skip

pytest_plugins = ["pytester"]


def _brain(pytester: pytest.Pytester, module_body: str) -> None:
    skill = pytester.mkdir("skills").joinpath("support")
    (skill / "lib").mkdir(parents=True)
    (skill / "lib" / "ext.py").write_text(textwrap.dedent(module_body))
    (skill / "scripts" / "tests").mkdir(parents=True)
    (skill / "scripts" / "tests" / "test_x.py").write_text(
        textwrap.dedent(
            """
            import sys, os
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lib"))
            import ext  # noqa
            def test_ok():
                assert True
            """
        )
    )


def test_missing_mirror_import_becomes_skip(pytester: pytest.Pytester, monkeypatch) -> None:
    monkeypatch.setattr(mirror_skip, "PROD_MIRRORS", "/definitely-absent-mirrors")
    _brain(
        pytester,
        """
        import sys
        sys.path.insert(0, "/definitely-absent-mirrors/proj/skills/records/scripts")
        from ka_absent_mirror_mod import rows  # noqa
        """,
    )
    result = pytester.runpytest("-p", "lib.mirror_skip", "skills", "-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*needs the prod source-mirror mount*"])


def test_unrelated_import_error_stays_error(pytester: pytest.Pytester) -> None:
    _brain(pytester, "import no_such_module_xyz  # noqa\n")
    result = pytester.runpytest("-p", "lib.mirror_skip", "skills")
    result.assert_outcomes(errors=1)


def test_reason_none_when_mirrors_present(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mirror_skip, "PROD_MIRRORS", str(tmp_path))
    try:
        raise ModuleNotFoundError("No module named 'ka'")
    except ModuleNotFoundError:
        excinfo = pytest.ExceptionInfo.from_current()
    assert mirror_skip.mirror_skip_reason(tmp_path / "t.py", excinfo) is None
