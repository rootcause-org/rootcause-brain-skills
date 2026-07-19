# /// script
# requires-python = ">=3.11"
# ///
"""Default structural validator for a Markdown-only brain checkout — the check `brain_test.py` cannot
do (it exits "no tests" for docs-only brains). Used by `brain-harvest` and `brain-publish` before push.

Run it from a brain checkout root; it grounds every check in `git ls-files`, so only committed/tracked
content is judged. Checks (each independently reported, skippable with `--skip <name>`):

  * links        — every relative Markdown link/route target in tracked *.md resolves to a tracked path.
  * frontmatter  — every tracked `skills/*/SKILL.md` has a valid front-matter block (name + description).
  * reachability — routed case/notes/playbook files are reachable from the project router (AGENTS.md).
  * lint         — `brain_lint.py` passes staged and `--all --strict` (privacy/contract + control-plane).
  * raw-tracked  — no raw-harvest path is tracked now (`.rootcause/` fragments or split-file shapes).
  * raw-history  — no raw-harvest path appears in git history (deleted-but-still-in-history case).
  * scratch      — (`--expect-clean` only) no `.rootcause/harvest/` scratch root remains on disk.

    uv run --no-project python brain_structure.py                 # default checks, from a brain root
    uv run --no-project python brain_structure.py --expect-clean  # + post-cleanup scratch-root check
    uv run --no-project python brain_structure.py --skip lint     # compose inside harvest/publish flows
    uv run --no-project python brain_structure.py --json          # machine-readable report

Exit status: 1 if any active check produced a finding, else 0. Findings print grep-style:
`path:line: <check>: <message>` (or `path: …` / `<check>: …` when no line/path applies), followed by a
machine-readable `SUMMARY …` line.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ── conventions (see docs/brain-model.md) ───────────────────────────────────────────────────────────
ROUTER = "AGENTS.md"
# Directories whose Markdown leaves a run must be *routed to* — orphaning one hides it from the loop.
ROUTED_DIRS = {"cases", "notes", "playbooks"}
ROUTED_FILES = {"terminology.md"}
# Infrastructure/entry-surface trees that have their own discovery path, so they never need routing
# from the project router. `skills/` are their own entry surface; `docs/`, `actions/`, `tests/`,
# `agents/`, and dot-trees are tooling, not run-routed brain leaves.
EXEMPT_DIRS = {"skills", "docs", "actions", "tests", "agents", ".git", ".claude", ".rootcause",
               ".github", "node_modules"}

# Raw-harvest fingerprints. A split corpus lands as `<dir>/threads/<yyyy-mm>--<slug>--<idx>.md` under
# the gitignored `.rootcause/`; either shape appearing as a tracked path is a privacy leak.
RAW_ROOTCAUSE_RE = re.compile(r"(?:^|/)\.rootcause/")
RAW_SPLIT_RE = re.compile(r"(?:^|/)\d{4}-\d{2}--.+?--\d+\.md$")
SCRATCH_ROOT = ".rootcause/harvest"

# A Markdown inline link/image target: `[text](target)` or `![alt](target "title")`.
MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*([^)]+?)\s*\)")
# Non-file targets: any scheme (http:, mailto:, tel:), protocol-relative `//host`, or anchor-only `#x`.
EXTERNAL_RE = re.compile(r"^(?:[a-z][a-z0-9+.\-]*:|//|#)", re.I)

DEFAULT_LINT_SCRIPT = (Path(__file__).resolve().parent / ".." / ".." /
                       "brain-harvest" / "scripts" / "brain_lint.py").resolve()


class StructureError(RuntimeError):
    """The checkout cannot be validated at all (not a git repo, git unusable)."""


@dataclass
class Finding:
    check: str
    message: str
    path: str | None = None
    line: int | None = None

    def render(self) -> str:
        if self.path and self.line:
            return f"{self.path}:{self.line}: {self.check}: {self.message}"
        if self.path:
            return f"{self.path}: {self.check}: {self.message}"
        return f"{self.check}: {self.message}"

    def as_dict(self) -> dict[str, object]:
        return {"check": self.check, "path": self.path, "line": self.line, "message": self.message}


@dataclass
class Ctx:
    root: Path
    tracked: list[str]
    tracked_set: set[str] = field(default_factory=set)
    md_files: list[str] = field(default_factory=list)
    lint_script: Path = DEFAULT_LINT_SCRIPT
    history_limit: int = 2000

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8", errors="replace")


# ── git plumbing ────────────────────────────────────────────────────────────────────────────────────
def git_toplevel(start: Path) -> Path:
    proc = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise StructureError(f"run from a brain checkout (git repository) root: {start}")
    return Path(proc.stdout.strip()).resolve()


def git_tracked(root: Path) -> list[str]:
    proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise StructureError(f"git ls-files failed: {proc.stderr.strip()}")
    return [p for p in proc.stdout.split("\0") if p]


def git_history_paths(root: Path, limit: int) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), "log", "--all", "--name-only", "--pretty=format:", f"-n{limit}"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        # No commits yet, or unusable history — treat as nothing to scan rather than a hard error.
        return []
    return list(dict.fromkeys(p for p in (ln.strip() for ln in proc.stdout.splitlines()) if p))


# ── link parsing ────────────────────────────────────────────────────────────────────────────────────
def iter_links(text: str):
    """Yield (lineno, raw_target) for every inline Markdown link/image target."""
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in MD_LINK_RE.finditer(line):
            target = m.group(1).split()[0].strip("<>") if m.group(1).split() else ""
            if target:
                yield lineno, target


def resolve_target(root: Path, md_rel: str, target: str) -> str | None:
    """Resolve a relative link target to a posix path relative to root, or None if not resolvable."""
    path_part = target.split("#", 1)[0].split("?", 1)[0]
    if not path_part:
        return None
    if path_part.startswith("/"):
        base = root
        path_part = path_part.lstrip("/")
    else:
        base = (root / md_rel).parent
    resolved = Path(os.path.normpath(base / path_part)).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return ".."  # escapes the checkout; caller treats a sentinel as unresolved


# ── checks ──────────────────────────────────────────────────────────────────────────────────────────
def check_links(ctx: Ctx) -> list[Finding]:
    findings: list[Finding] = []
    tracked_dirs = {parent for p in ctx.tracked for parent in _ancestors(p)}
    for md in ctx.md_files:
        for lineno, target in iter_links(ctx.read(md)):
            if EXTERNAL_RE.match(target) or target.startswith("#"):
                continue
            rel = resolve_target(ctx.root, md, target)
            if rel is None:
                continue
            if rel in ctx.tracked_set or rel in tracked_dirs:
                continue
            findings.append(Finding("links", f"link target does not resolve to a tracked path: {target}",
                                    path=md, line=lineno))
    return findings


def _ancestors(posix_path: str) -> list[str]:
    parts = posix_path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """Flat `key: value` front-matter between leading `---` fences; None if absent/unterminated."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    data: dict[str, str] = {}
    for raw in lines[1:]:
        if raw.strip() == "---":
            return data
        if ":" in raw and not raw.startswith((" ", "\t", "-")):
            key, value = raw.split(":", 1)
            data[key.strip()] = value.strip()
    return None


def check_frontmatter(ctx: Ctx) -> list[Finding]:
    findings: list[Finding] = []
    for md in ctx.md_files:
        parts = md.split("/")
        if not (parts[0] == "skills" and parts[-1] == "SKILL.md"):
            continue
        data = parse_frontmatter(ctx.read(md))
        if data is None:
            findings.append(Finding("frontmatter", "missing or unterminated YAML front-matter block",
                                    path=md, line=1))
            continue
        for key in ("name", "description"):
            if not data.get(key):
                findings.append(Finding("frontmatter", f"front-matter is missing a non-empty {key!r}",
                                        path=md, line=1))
    return findings


def check_reachability(ctx: Ctx) -> list[Finding]:
    routed = [md for md in ctx.md_files if _needs_reach(md)]
    if not routed:
        return []
    md_set = set(ctx.md_files)
    adjacency: dict[str, set[str]] = {}
    for md in ctx.md_files:
        neighbours: set[str] = set()
        for _lineno, target in iter_links(ctx.read(md)):
            if EXTERNAL_RE.match(target):
                continue
            rel = resolve_target(ctx.root, md, target)
            if rel and rel in md_set:
                neighbours.add(rel)
        adjacency[md] = neighbours
    reachable: set[str] = set()
    if ROUTER in md_set:
        stack = [ROUTER]
        while stack:
            node = stack.pop()
            if node in reachable:
                continue
            reachable.add(node)
            stack.extend(adjacency.get(node, ()))
    return [Finding("reachability",
                    f"routed file is not reachable from the project router ({ROUTER})", path=md)
            for md in routed if md not in reachable]


def _needs_reach(posix_path: str) -> bool:
    parts = posix_path.split("/")
    if posix_path == ROUTER:
        return False
    if any(p in EXEMPT_DIRS for p in parts[:-1]):
        return False
    if parts[-1] in ROUTED_FILES:
        return True
    return any(p in ROUTED_DIRS for p in parts[:-1])


def check_lint(ctx: Ctx) -> list[Finding]:
    if not ctx.lint_script.is_file():
        return [Finding("lint",
                        f"privacy/contract lint script not found at {ctx.lint_script}; pass --lint-script")]
    findings: list[Finding] = []
    for label, extra in (("staged", []), ("full-tree strict", ["--all", "--strict"])):
        proc = subprocess.run([sys.executable, str(ctx.lint_script), *extra],
                              cwd=str(ctx.root), capture_output=True, text=True)
        if proc.returncode != 0:
            tail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
            reason = tail[-1] if tail else f"exit {proc.returncode}"
            findings.append(Finding("lint", f"brain_lint {label} mode failed (exit {proc.returncode}): "
                                            f"{reason}"))
    return findings


def check_raw_tracked(ctx: Ctx) -> list[Finding]:
    return [Finding("raw-tracked", "raw-harvest path is tracked; it must never be committed", path=p)
            for p in ctx.tracked if RAW_ROOTCAUSE_RE.search(p) or RAW_SPLIT_RE.search(p)]


def check_raw_history(ctx: Ctx) -> list[Finding]:
    findings: list[Finding] = []
    for p in git_history_paths(ctx.root, ctx.history_limit):
        if RAW_ROOTCAUSE_RE.search(p) or RAW_SPLIT_RE.search(p):
            findings.append(Finding(
                "raw-history",
                "raw-harvest path exists in git history (deleted from the tree but still recoverable); "
                "escalate to the operator for a deliberate history rewrite — never auto-rewrite",
                path=p))
    return findings


def check_scratch(ctx: Ctx) -> list[Finding]:
    if (ctx.root / SCRATCH_ROOT).exists():
        return [Finding("scratch", "sensitive harvest scratch root still exists on disk; delete it "
                                   "before publishing", path=SCRATCH_ROOT)]
    return []


CHECKS: list[tuple[str, Callable[[Ctx], list[Finding]]]] = [
    ("links", check_links),
    ("frontmatter", check_frontmatter),
    ("reachability", check_reachability),
    ("lint", check_lint),
    ("raw-tracked", check_raw_tracked),
    ("raw-history", check_raw_history),
]
EXPECT_CLEAN_CHECK: tuple[str, Callable[[Ctx], list[Finding]]] = ("scratch", check_scratch)
ALL_CHECK_NAMES = [name for name, _ in CHECKS] + [EXPECT_CLEAN_CHECK[0]]


def run_checks(ctx: Ctx, *, expect_clean: bool, skip: set[str]) -> list[dict[str, object]]:
    active = list(CHECKS)
    if expect_clean:
        active.append(EXPECT_CLEAN_CHECK)
    results: list[dict[str, object]] = []
    for name, fn in active:
        if name in skip:
            results.append({"name": name, "skipped": True, "ok": True, "findings": []})
            continue
        found = fn(ctx)
        results.append({"name": name, "skipped": False, "ok": not found, "findings": found})
    return results


def build_report(results: list[dict[str, object]]) -> dict[str, object]:
    ran = [r for r in results if not r["skipped"]]
    failed = [r for r in ran if not r["ok"]]
    findings = [f for r in results for f in r["findings"]]
    return {
        "ok": not failed,
        "checks": [
            {"name": r["name"], "skipped": r["skipped"], "ok": r["ok"],
             "findings": [f.as_dict() for f in r["findings"]]}
            for r in results
        ],
        "findings": [f.as_dict() for f in findings],
        "summary": {"checks": len(results), "ran": len(ran), "passed": len(ran) - len(failed),
                    "failed": len(failed), "findings": len(findings),
                    "failed_checks": [r["name"] for r in failed]},
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain_structure.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", help="brain checkout root (default: cwd's git toplevel)")
    p.add_argument("--skip", action="append", default=[], choices=ALL_CHECK_NAMES, metavar="CHECK",
                   help=f"skip a check by name ({', '.join(ALL_CHECK_NAMES)}); repeatable")
    p.add_argument("--expect-clean", action="store_true",
                   help="also require no .rootcause/harvest/ scratch root remains (post-cleanup gate)")
    p.add_argument("--lint-script", help="override path to brain_lint.py (default: sibling in the kit)")
    p.add_argument("--history-limit", type=int, default=2000,
                   help="max commits scanned for the raw-history check (default: 2000)")
    p.add_argument("--json", action="store_true", help="emit a machine-readable JSON report")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        start = Path(args.root).resolve() if args.root else Path.cwd()
        root = git_toplevel(start)
        tracked = git_tracked(root)
    except StructureError as exc:
        print(f"brain-structure: {exc}", file=sys.stderr)
        return 2

    ctx = Ctx(
        root=root,
        tracked=tracked,
        tracked_set=set(tracked),
        md_files=[p for p in tracked if p.lower().endswith(".md")],
        lint_script=Path(args.lint_script).resolve() if args.lint_script else DEFAULT_LINT_SCRIPT,
        history_limit=args.history_limit,
    )

    results = run_checks(ctx, expect_clean=args.expect_clean, skip=set(args.skip))
    report = build_report(results)
    summary = report["summary"]

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for finding in (f for r in results for f in r["findings"]):
            print(finding.render())
        failed = summary["failed_checks"]
        print(f"SUMMARY checks={summary['checks']} ran={summary['ran']} passed={summary['passed']} "
              f"failed={summary['failed']} findings={summary['findings']} "
              f"failed_checks={','.join(failed) if failed else '-'}")

    return 1 if not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
