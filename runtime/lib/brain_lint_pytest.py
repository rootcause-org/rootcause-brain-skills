"""Pytest adapter for the dependency-light `lib.brain_lint` core."""

from __future__ import annotations

from pathlib import Path

import pytest

from .brain_lint import Finding, format_report, lint_brain

_REPORT = pytest.StashKey[list[Finding]]()


class BrainLintItem(pytest.Item):
    """A file-less offline item; terminal summary owns the single hygiene report."""

    def __init__(self, name: str, parent: pytest.Session, brain_root: Path) -> None:
        super().__init__(name, parent)
        self.brain_root = brain_root

    def runtest(self) -> None:
        findings = lint_brain(self.brain_root)
        self.config.stash[_REPORT] = findings
        fails = sum(f.level == "FAIL" for f in findings)
        if fails:
            raise BrainLintError(f"brain lint failed ({fails} FAIL); see compact report below")

    def repr_failure(self, excinfo, style=None):  # noqa: ANN001 — pytest signature
        if isinstance(excinfo.value, BrainLintError):
            return str(excinfo.value)
        return super().repr_failure(excinfo, style=style)

    def reportinfo(self):
        return self.brain_root, 0, "brain lint"


class BrainLintError(Exception):
    """Keeps a lint failure traceback out of pytest output."""


def _brain_root(config: pytest.Config) -> Path | None:
    for arg in config.args:
        path = Path(arg)
        if path.name == "skills":
            return path.parent
    return None


def _is_live_tier(config: pytest.Config) -> bool:
    return (config.getoption("markexpr", "") or "").strip() == "live"


def pytest_collection_modifyitems(session: pytest.Session, config: pytest.Config,
                                  items: list[pytest.Item]) -> None:
    if _is_live_tier(config):
        return
    root = _brain_root(config)
    if root is not None:
        items.append(BrainLintItem.from_parent(session, name="brain_lint", brain_root=root))


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    findings = terminalreporter.config.stash.get(_REPORT, None)
    if findings is not None:
        terminalreporter.write_line(format_report(findings))
