"""Offline brain description + action hygiene lint.

Every routable brain file must carry a `description:` the bootstrap tree can render on its line, or
it is **invisible to the grounding pre-step**: retrieval is `rg`-driven and lexical, so a file whose
tree line has no customer-vocabulary gloss never gets grepped. This lint holds the brain content up to
that contract:

  * **FAIL** — a `skills/*/SKILL.md` or `skills/cases/*.md` with no renderable `description:`
    frontmatter, or one whose whitespace-collapsed length exceeds 90 chars (the tree truncates
    there, so the tail never reaches the model; nothing else consumes a long md description);
    an `actions/*/manifest.yaml` with no top-level `description:`.
  * **WARN** — an overlong action-manifest description (the SAME field is injected full-length into
    the per-run action catalog prompt, so rich copy is load-bearing there — the rule is "front-load
    the first 90 chars", not "shorten"); "what this file contains"-style phrasing (`This file…`,
    `Contains…`, `Dit bestand…`). Deterministic, best-effort; never fails a run.

It mirrors `rootcause/internal/brain/bootstrap.go` so lint and tree **agree**: the same bounded
head-read frontmatter parse for markdown (block scalars / multi-line values are *not* rendered, so
they count as missing here), real YAML for action manifests, and the same `tidyDesc` whitespace
collapse before the 90-char measure.

This module stays stdlib + PyYAML only so the standalone developer entrypoint and the production
publish gate can import it without pytest. `lib.brain_lint_pytest` owns the optional pytest wiring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .action_lint import lint_actions

# Mirror bootstrap.go's descMaxLen / descHeadBytes so the lint's verdict matches what the tree renders.
DESC_MAX_LEN = 90
DESC_HEAD_BYTES = 2048

# Leading-phrase patterns that describe *contents* ("what this holds") instead of *when to open this*.
# WARN-only and deliberately small — a few high-precision openers in English + Dutch, matched at the
# very start after whitespace/quote trim. Not an exhaustive style grader; just the common tells.
_CONTAINS_STYLE = re.compile(
    r"^\s*(this\s+(file|doc|document|page|skill|runbook)|contains\b|describes\b|documentation\s+for"
    r"|dit\s+(bestand|document)|deze\s+(pagina|file))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    """One lint result. `level` is "FAIL" (blocks the tier) or "WARN" (reported, never fails)."""

    path: str  # brain-relative path, e.g. "skills/backups/SKILL.md"
    level: str
    message: str
    rule: str = "other"


def _tidy(val: str) -> str:
    """Collapse internal whitespace like bootstrap.go's tidyDesc (strings.Fields + join) — no truncation.

    Length is then measured on the collapsed form, so a description that only *looks* long because of
    wrapping/indentation isn't penalised, matching what the tree actually renders.
    """
    return " ".join(val.split())


def _md_description(path: Path) -> str | None:
    """The renderable frontmatter `description:` of a markdown file, or None when the tree renders none.

    Faithful port of bootstrap.go's `mdDescription`: a bounded head-read + line scan (NOT a YAML
    parser). Returns None for missing frontmatter, no `description:` key, an empty value, or a block
    scalar (`|`/`>`) / multi-line value — every case where the tree line would carry no gloss, so the
    lint treats them all as "no description" exactly as the model would see it.
    """
    try:
        head = path.read_bytes()[:DESC_HEAD_BYTES]
    except OSError:
        return None
    lines = head.decode("utf-8", "replace").split("\n")
    if len(lines) < 2 or lines[0].rstrip("\r") != "---":
        return None
    for raw in lines[1:]:
        line = raw.rstrip("\r")
        if line == "---":
            return None  # end of frontmatter, no description
        rest = _strip_prefix(line, "description:")
        if rest is None:
            continue
        val = rest.strip()
        # Strip only a MATCHED surrounding quote pair (embedded quotes stay intact) — as bootstrap does.
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        if val == "" or val.startswith("|") or val.startswith(">"):
            return None  # block scalar or empty: the tree drops it
        return _tidy(val)
    return None


def _strip_prefix(line: str, prefix: str) -> str | None:
    return line[len(prefix):] if line.startswith(prefix) else None


def _manifest_description(path: Path) -> str | None:
    """The top-level `description:` of an action manifest, or None when missing/empty/unparseable.

    Real YAML (mirrors bootstrap.go's `manifestGloss`) — action descriptions are routinely `>-` block
    scalars, which a line scan would drop; here they are legitimately present, so parse them properly.
    """
    try:
        data = yaml.safe_load(path.read_text("utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    desc = data.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return None
    return _tidy(desc)


def _check(path: Path, rel: str, desc: str | None, kind: str) -> list[Finding]:
    """Turn one file's extracted description into findings (missing/overlong + style WARN)."""
    if desc is None:
        return [Finding(rel, "FAIL",
                        f"missing renderable `description:` in {kind} "
                        "(absent, empty, or a block-scalar/multi-line value the tree drops)",
                        "description-missing")]
    out: list[Finding] = []
    if len(desc) > DESC_MAX_LEN:
        if kind == "action manifest":
            # Manifest descriptions double as the full-length action-catalog prompt entry, so
            # length is legitimate — only the first 90 chars reach the tree gloss.
            out.append(Finding(rel, "WARN",
                               f"description is {len(desc)} chars; the tree gloss truncates at "
                               f"{DESC_MAX_LEN} — front-load the when-to-use signal in the first "
                               f"{DESC_MAX_LEN} chars (do NOT shorten the catalog copy)",
                               "description-length"))
        else:
            out.append(Finding(rel, "FAIL",
                               f"description is {len(desc)} chars (>{DESC_MAX_LEN}); the tree truncates "
                               f"at {DESC_MAX_LEN}, so the tail is invisible to the model",
                               "description-length"))
    if _CONTAINS_STYLE.match(desc):
        out.append(Finding(rel, "WARN",
                           "description reads as \"what this contains\"; prefer \"when to open this\" "
                           "phrasing in the customer's own words", "description-style"))
    return out


def lint_brain(brain_root: str | Path) -> list[Finding]:
    """Lint every routable file under `brain_root` for a renderable, in-budget `description:`.

    Targets, mirroring the authoring mandate: `skills/*/SKILL.md`, `skills/cases/*.md`, and
    `actions/*/manifest.yaml`. Pure + deterministic (stdlib + PyYAML): no network, no DSN, no model.
    """
    root = Path(brain_root)
    findings: list[Finding] = []

    for skill_md in sorted(root.glob("skills/*/SKILL.md")):
        findings += _check(skill_md, _rel(root, skill_md), _md_description(skill_md), "SKILL.md")

    for case_md in sorted(root.glob("skills/cases/*.md")):
        findings += _check(case_md, _rel(root, case_md), _md_description(case_md), "runbook")

    for manifest in sorted(root.glob("actions/*/manifest.yaml")):
        findings += _check(manifest, _rel(root, manifest), _manifest_description(manifest),
                           "action manifest")

    # Bind the sibling lint when this plugin loads, before collected brain tests can replace the
    # top-level ``lib`` module in ``sys.modules`` with a test double.
    findings += [Finding(f.path, f.level, f.message, f.rule) for f in lint_actions(root)]
    return findings


def _rel(root: Path, p: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


_RULE_LABELS = {
    "description-missing": "missing descriptions",
    "description-length": "description length",
    "description-style": "description style",
    "script-size": "script size",
    "helper-duplicate": "duplicate helpers",
    "helper-drift": "drifted helpers",
    "private-dead": "dead private names",
    "other": "other",
}


def format_report(findings: list[Finding]) -> str:
    """Render one deterministic compact block: FAIL groups first, then WARN groups by rule."""
    fails = sum(f.level == "FAIL" for f in findings)
    warns = sum(f.level == "WARN" for f in findings)
    if not findings:
        return "brain lint: clean"

    lines = [f"brain lint: {fails} FAIL, {warns} WARN"]
    for level in ("FAIL", "WARN"):
        rules = sorted({f.rule for f in findings if f.level == level},
                       key=lambda rule: (_RULE_LABELS.get(rule, rule), rule))
        for rule in rules:
            rows = sorted((f for f in findings if f.level == level and f.rule == rule),
                          key=lambda f: (f.path, f.message))
            lines.append(f"{level} {_RULE_LABELS.get(rule, rule)} ({len(rows)})")
            lines.extend(f"  {f.path} — {f.message}" for f in rows)
    return "\n".join(lines)
