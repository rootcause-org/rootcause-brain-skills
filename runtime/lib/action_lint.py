"""Offline **action-script hygiene lint** — pure, stdlib-only, wired through `lib.brain_lint`.

Why this exists: hosted Python actions are single-file by construction (only `sha256(script.py)` is
pinned and shipped), so helpers get copy-pasted between scripts and scripts grow unbounded. One brain's
`script.py` went 32 KB → 109 KB in 25 days and crossed the transport's argv limit (Linux
MAX_ARG_STRLEN, 128 KiB after base64) → action dead in prod. Three cheap, conservative checks:

  * **size budget** — `actions/*/script.py` and `preflight.py` raw bytes: WARN ≥ 64 KiB, FAIL ≥ 96 KiB.
    Per-brain override: `actions/lint.yaml` with `script_size: {warn_kb: N, fail_kb: N}`.
  * **duplicate helpers** — a module-level `_private` `def` whose AST-normalised body is identical across ≥ 3
    action scripts → WARN "hoist"; same name in ≥ 3 scripts with ≥ 2 distinct bodies → WARN "drifted"
    (this shape produced a real cross-tenant guard bug). Public names (`main`, `run`) are per-script
    entrypoints and skipped. One finding per helper, never FAIL.
  * **dead private names** — module-level `_private` functions / constants never referenced in their
    own file → WARN. Only `_`-prefixed names, so public API and harness entrypoints never trip it.

All three are deterministic and best-effort: an unparsable script is skipped (the harness/tests
surface syntax errors elsewhere).
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

SIZE_WARN_KB = 64
SIZE_FAIL_KB = 96
DUP_MIN_SCRIPTS = 3
OVERRIDE_FILE = "actions/lint.yaml"

_ARGV_HINT = ("the executor ships the whole file inline through the host's transport (argv limit "
              "~128 KiB after base64); hoist shared helpers to a project lib or split the action")


@dataclass(frozen=True)
class Finding:
    path: str
    level: str  # "FAIL" | "WARN"
    message: str


def _size_limits(root: Path) -> tuple[int, int]:
    """(warn_bytes, fail_bytes), honouring `actions/lint.yaml` → `script_size: {warn_kb, fail_kb}`."""
    warn_kb, fail_kb = SIZE_WARN_KB, SIZE_FAIL_KB
    override = root / OVERRIDE_FILE
    if override.is_file():
        try:
            import yaml

            cfg = (yaml.safe_load(override.read_text("utf-8")) or {}).get("script_size") or {}
            warn_kb = int(cfg.get("warn_kb", warn_kb))
            fail_kb = int(cfg.get("fail_kb", fail_kb))
        except Exception:  # noqa: BLE001 — a broken override must not crash the lint
            pass
    return warn_kb * 1024, fail_kb * 1024


def _action_py_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for name in ("script.py", "preflight.py"):
        out += sorted(root.glob(f"actions/*/{name}"))
    return out


def _body_key(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Whitespace/comment/docstring-insensitive fingerprint of a function's signature + body."""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    parts = [fn.args, *fn.decorator_list, *([fn.returns] if fn.returns else []), *body]
    return "|".join(ast.dump(n, annotate_fields=False) for n in parts)


def _module_privates(tree: ast.Module) -> dict[str, int]:
    """`_name` → line for module-level `def`/`class`/simple assignments (dunder excluded)."""
    out: dict[str, int] = {}
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        for n in names:
            if n.startswith("_") and not n.startswith("__"):
                out.setdefault(n, node.lineno)
    return out


def _referenced_names(tree: ast.Module) -> set[str]:
    """Every identifier used anywhere except as the *defining* module-level statement itself."""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Load, ast.Del)):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)  # `self._x` / `mod._x` style — stay conservative
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            used.add(node.value)  # getattr(obj, "_x") / __all__ strings
        elif isinstance(node, ast.Global):
            used.update(node.names)
    return used


def lint_actions(brain_root: str | Path) -> list[Finding]:
    root = Path(brain_root)
    warn_b, fail_b = _size_limits(root)
    findings: list[Finding] = []
    # helper name → body key → [rel paths]
    helpers: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    for path in _action_py_files(root):
        rel = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        size = len(raw)
        if size >= fail_b:
            findings.append(Finding(rel, "FAIL", f"{size // 1024} KiB ≥ {fail_b // 1024} KiB budget — {_ARGV_HINT}"))
        elif size >= warn_b:
            findings.append(Finding(rel, "WARN", f"{size // 1024} KiB ≥ {warn_b // 1024} KiB soft budget — {_ARGV_HINT}"))

        try:
            tree = ast.parse(raw, filename=rel)
        except SyntaxError:
            continue

        if path.name == "script.py":
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("_"):
                    helpers[node.name][_body_key(node)].append(rel)

        used = _referenced_names(tree)
        for name, line in sorted(_module_privates(tree).items(), key=lambda kv: kv[1]):
            if name not in used:
                findings.append(Finding(f"{rel}:{line}", "WARN",
                                        f"`{name}` is defined but never referenced in this file — delete it"))

    for name, variants in sorted(helpers.items()):
        scripts = sorted({p for paths in variants.values() for p in paths})
        if len(scripts) < DUP_MIN_SCRIPTS:
            continue
        actions = ", ".join(p.split("/")[1] for p in scripts)
        if len(variants) == 1:
            findings.append(Finding("actions/", "WARN",
                                    f"`{name}` is identical in {len(scripts)} scripts ({actions}) — "
                                    "hoist it to a shared project module instead of copy-pasting"))
        else:
            findings.append(Finding("actions/", "WARN",
                                    f"`{name}` exists in {len(scripts)} scripts ({actions}) with "
                                    f"{len(variants)} different bodies — drifted copies hide bugs; "
                                    "diff them and hoist one version"))
    return findings
