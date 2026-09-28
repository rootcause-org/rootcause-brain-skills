# /// script
# requires-python = ">=3.11"
# ///
"""Deterministic pre-publish hygiene gate for a brain checkout — the mechanical mistakes a human
otherwise finds by eye on the live brain (2026-09-25: DentAI shipped `<<<<<<<` markers in
skills/scheduling/SKILL.md). `brain_git_sync.py` runs it before every push; `brain_structure.py`
runs it as its `hygiene` check. Rules (run-hidden `_internal/`/`.rcignore`/`.replypenignore` paths
and dev-tooling dot-dirs are judged for conflict markers only):

  * conflict     — `<<<<<<<` / `|||||||` / `>>>>>>>` line markers. Whole tree, always.
  * placeholder  — in a file `projection.yaml` templates: a `{{ key }}` it does not declare, or a
                   residual `{{`/`}}` (rootcause's projection fails the whole run on both).
                   Untemplated files may quote `{{ }}` (customer merge fields, journals).
  * abs-path     — a `/Users/<name>` laptop path.
  * links        — a relative Markdown link that resolves to no tracked path.
  * em-dash      — U+2014 in customer-facing copy: action manifest `display_name`/`customer_*`,
                   `projection.yaml` placeholder defaults, Markdown with `customer_facing: true`
                   frontmatter.
  * mermaid      — a ```mermaid block `mmdc` rejects as a syntax error (`mmdc` on PATH, else
                   `pnpm dlx @mermaid-js/mermaid-cli`). No renderer, or a renderer that cannot
                   start (e.g. no headless Chrome), skips the rule with a NOTICE.

Everything but `conflict` is scoped to changed files (vs `--base`, default origin/main) so a mature
tree's legacy debt cannot block an unrelated publish; `--all` judges every tracked file.

    uv run --no-project python brain_hygiene.py            # changed files vs origin/main
    uv run --no-project python brain_hygiene.py --all      # whole tree
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_structure as bs  # noqa: E402

CONFLICT_RE = re.compile(r"^(<{7}|\|{7}|>{7})(?: |$)")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")  # rootcause internal/projection
ABS_PATH_RE = re.compile(r"/Users/[A-Za-z0-9._-]+")
MANIFEST_COPY_RE = re.compile(r"^\s*(display_name|customer_[a-z_]+)\s*:(.*)$")
DEFAULT_VALUE_RE = re.compile(r"\bdefault:\s*(\"[^\"]*\"|'[^']*'|[^#,}]*)")
DEFAULT_TEMPLATED_GLOBS = ["**/*.md"]  # rootcause internal/projection DefaultTemplatedGlobs
TEXT_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".json", ".toml", ".sh", ".rb", ".txt", ".sql"}
EM_DASH = "\u2014"
TOOLING_DIRS = (".agents/", ".claude/", ".github/")
MERMAID_TIMEOUT_S = 30
MERMAID_SYNTAX_RE = re.compile(r"(Parse|Lexical|Syntax) error|No diagram type detected", re.I)


def _glob_re(glob: str) -> re.Pattern[str]:
    out = ""
    for part in re.split(r"(\*\*/|\*\*|\*|\?)", glob):
        out += {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(part, re.escape(part))
    return re.compile(out + r"\Z")


def _projection(root: Path) -> tuple[set[str], list[re.Pattern[str]]] | None:
    """(declared placeholder keys, templated-glob matchers) from projection.yaml, or None."""
    spec = root / "projection.yaml"
    if not spec.is_file():
        return None
    keys: set[str] = set()
    globs: list[str] = []
    section = None
    for line in spec.read_text("utf-8", errors="replace").splitlines():
        if top := re.match(r"^([A-Za-z_]+):", line):
            section = top.group(1)
        elif section == "placeholders" and (m := re.match(r"^  ([A-Za-z0-9_.]+):", line)):
            keys.add(m.group(1))
        elif section == "templated_globs" and (m := re.match(r"^\s*-\s*[\"']?([^\"'#]+?)[\"']?\s*(#.*)?$", line)):
            globs.append(m.group(1))
    return keys, [_glob_re(g) for g in globs or DEFAULT_TEMPLATED_GLOBS]


def _frontmatter_flag(text: str, key: str) -> bool:
    fm = bs.parse_frontmatter(text) or {}
    return str(fm.get(key, "")).strip().lower() in {"true", "yes"}


def _mermaid_blocks(text: str):
    lines, block, start = text.splitlines(), None, 0
    for lineno, line in enumerate(lines, start=1):
        if block is None and re.match(r"^\s*```\s*mermaid\s*$", line):
            block, start = [], lineno
        elif block is not None and re.match(r"^\s*```\s*$", line):
            yield start, "\n".join(block)
            block = None
        elif block is not None:
            block.append(line)


def _mermaid_renderer() -> list[str] | None:
    if shutil.which("mmdc"):
        return ["mmdc"]
    if shutil.which("pnpm"):
        return ["pnpm", "--silent", "--package=@mermaid-js/mermaid-cli", "dlx", "mmdc"]
    return None


def _mermaid_render(mmdc: list[str], source: str) -> tuple[bool, str]:
    """(is a diagram syntax error, detail). Renderer/infra failures (no headless Chrome, timeout)
    are not the author's fault: they come back False and become a NOTICE, never a FAIL."""
    with tempfile.TemporaryDirectory(prefix="brain-hygiene-mmd-") as tmp:
        src, out = Path(tmp) / "in.mmd", Path(tmp) / "out.svg"
        src.write_text(source, "utf-8")
        try:
            proc = subprocess.run([*mmdc, "-q", "-i", str(src), "-o", str(out)],
                                  capture_output=True, text=True, timeout=MERMAID_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return False, f"mmdc timed out after {MERMAID_TIMEOUT_S}s"
        if proc.returncode == 0:
            return False, ""
        lines = [ln.strip() for ln in (proc.stderr or proc.stdout).splitlines()
                 if ln.strip() and not ln.strip().startswith("at ")]
        syntax = next((ln for ln in lines if MERMAID_SYNTAX_RE.search(ln)), None)
        return (True, syntax) if syntax else (False, lines[0] if lines else f"exit {proc.returncode}")


def check(root: Path, files: list[str] | None = None) -> list[bs.Finding]:
    """Hygiene findings for a brain at `root`. `files` scopes every rule but `conflict`;
    None judges the whole tracked tree."""
    tracked = bs.git_tracked(root)
    ctx = bs.Ctx(root=root, tracked=tracked, tracked_set=set(tracked),
                 md_files=[p for p in tracked if p.lower().endswith(".md")])
    hidden = bs.hidden_paths(ctx)
    text_files = [p for p in tracked
                  if (root / p).is_file() and Path(p).suffix.lower() in TEXT_SUFFIXES]
    visible = {p for p in text_files if p not in hidden and not p.startswith(TOOLING_DIRS)}
    scoped = visible if files is None else set(files) & visible
    projection = _projection(root)
    mmdc = _mermaid_renderer()
    findings: list[bs.Finding] = []
    mermaid_seen, renderer_failure = False, ""

    def add(rule: str, message: str, rel: str, line: int | None = None) -> None:
        findings.append(bs.Finding("hygiene", f"{rule}: {message}", path=rel, line=line))

    for rel in text_files:
        text = (root / rel).read_text("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if CONFLICT_RE.match(line):
                add("conflict", f"git conflict marker {line[:7]!r}", rel, lineno)
        if rel not in scoped:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if m := ABS_PATH_RE.search(line):
                add("abs-path", f"absolute laptop path {m.group(0)!r}", rel, lineno)
        if rel.endswith(".md"):
            templated = projection is not None and any(g.match(rel) for g in projection[1])
            if templated:
                for lineno, line in enumerate(text.splitlines(), start=1):
                    for key in PLACEHOLDER_RE.findall(line):
                        if key not in projection[0]:
                            add("placeholder", f"{{{{ {key} }}}} is not declared in projection.yaml "
                                               "placeholders", rel, lineno)
                    if "{{" in (rest := PLACEHOLDER_RE.sub("", line)) or "}}" in rest:
                        add("placeholder", "residual `{{`/`}}` in a projection-templated file",
                            rel, lineno)
            if _frontmatter_flag(text, "customer_facing"):
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if EM_DASH in line:
                        add("em-dash", "em dash in customer-facing copy", rel, lineno)
            for lineno, source in _mermaid_blocks(text):
                mermaid_seen = True
                if not mmdc:
                    continue
                syntax, detail = _mermaid_render(mmdc, source)
                if syntax:
                    add("mermaid", f"block does not parse: {detail}", rel, lineno)
                elif detail:
                    renderer_failure = detail
        elif rel.startswith("actions/") and rel.endswith(("/manifest.yaml", "/manifest.yml")):
            for lineno, line in enumerate(text.splitlines(), start=1):
                if (m := MANIFEST_COPY_RE.match(line)) and EM_DASH in m.group(2):
                    add("em-dash", f"em dash in customer-facing `{m.group(1)}`", rel, lineno)
        elif rel == "projection.yaml":
            for lineno, line in enumerate(text.splitlines(), start=1):
                if (m := DEFAULT_VALUE_RE.search(line)) and EM_DASH in m.group(1):
                    add("em-dash", "em dash in a placeholder default (lands in customer replies)",
                        rel, lineno)

    for f in bs.check_links(ctx):
        if f.path in scoped:
            findings.append(bs.Finding("hygiene", f"links: {f.message}", path=f.path, line=f.line))
    if mermaid_seen and not mmdc:
        findings.append(bs.Finding("hygiene", "mermaid: skipped, no renderer (`mmdc` or `pnpm`) on "
                                   "PATH; diagrams unchecked",
                                   severity="NOTICE"))
    if renderer_failure:
        findings.append(bs.Finding("hygiene", f"mermaid: renderer failed, diagrams unchecked: "
                                   f"{renderer_failure}", severity="NOTICE"))
    return findings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain_hygiene.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", help="brain checkout root (default: cwd's git toplevel)")
    p.add_argument("--base", default="origin/main", help="scope rules to files changed vs this ref")
    p.add_argument("--all", action="store_true", help="judge every tracked file")
    args = p.parse_args(argv)
    try:
        root = bs.git_toplevel(Path(args.root).resolve() if args.root else Path.cwd())
    except bs.StructureError as exc:
        print(f"brain-hygiene: {exc}", file=sys.stderr)
        return 2
    files = None if args.all else bs.git_changed_paths(root, args.base)
    findings = check(root, files)
    for f in findings:
        print(f.render())
    blocking = [f for f in findings if f.severity != "NOTICE"]
    scope = "all" if files is None else f"{len(files)} changed file(s) vs {args.base}"
    print(f"brain hygiene: {len(blocking)} FAIL ({scope}; conflict markers: whole tree)")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
