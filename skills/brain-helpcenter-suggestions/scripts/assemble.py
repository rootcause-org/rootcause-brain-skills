#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2", "pyyaml"]
# ///
"""Read the small files the agent writes into `$OUT` and assemble one `Suggestions` dict.

    classification.tsv   # evidence_sha256 <sha> + one line per conversation
    suggestions/S1.md    YAML frontmatter + `## Why` + `## Edit`
    learnings.md         `- target: observation -> proposed change`
    headline.txt         one sentence

Parsing only: every rule lives in `schema.py`. Errors are `file:line: message` so the
agent rewrites one small file instead of regenerating everything.
"""

from __future__ import annotations

import re
import sys
import typing
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schema import Verdict  # noqa: E402

VERDICTS = typing.get_args(Verdict)
TARGETS = ("rubric", "recipe", "normaliser", "validator", "render")
SCHEMA_VERSION = 2
_SHA = re.compile(r"^#\s*evidence_sha256\s+([0-9a-f]{64})\s*$")
_HEADING = re.compile(r"^##\s+(Why|Edit)\s*$", re.MULTILINE)


class Assembled:
    """data = the Suggestions dict; labels/sources map error prefixes back to files."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "evidence_sha256": "", "headline": "", "classification": [], "suggestions": [], "learnings": []}
        self.labels: dict[str, str] = {}
        self.sources: dict[str, str] = {}
        self.errors: list[str] = []


def _cells(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip() and v.strip() != "-"]


def _classification(path: Path, a: Assembled) -> None:
    if not path.exists():
        a.errors.append(f"{path.name}: missing (write one line per conversation: id, verdict, topics, article_ids)")
        return
    lines = path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    seen_sha = False
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue
        if not seen_sha:
            hit = _SHA.match(line)
            if not hit:
                a.errors.append(f"classification.tsv:{n}: first line must be '# evidence_sha256 <sha>' (the sha collect printed)")
                seen_sha = True  # keep parsing the rest so the agent sees every problem at once
            else:
                a.data["evidence_sha256"] = hit.group(1)
                seen_sha = True
                continue
        if line.startswith("#") or line.startswith("conversation_id"):
            continue
        cols = [c.strip() for c in raw.split("\t")]
        cols += [""] * (4 - len(cols))
        if len(cols) > 4:
            a.errors.append(f"classification.tsv:{n}: {len(cols)} tab-separated columns, expected 4 (id, verdict, topics, article_ids)")
            continue
        cid, verdict = cols[0], cols[1]
        if not cid or not verdict:
            a.errors.append(f"classification.tsv:{n}: needs at least a conversation id and a verdict, got {line!r}")
            continue
        if verdict not in VERDICTS:
            a.errors.append(f"classification.tsv:{n}: unknown verdict {verdict!r} (one of: {', '.join(VERDICTS)})")
            continue
        i = len(a.data["classification"])
        a.labels[f"classification[{i}]"] = f"classification.tsv:{n}"
        a.data["classification"].append(
            {"conversation_id": cid, "verdict": verdict, "topics": _cells(cols[2]), "article_ids": _cells(cols[3])}
        )
    if not seen_sha:
        a.errors.append("classification.tsv:1: first line must be '# evidence_sha256 <sha>' (the sha collect printed)")


def _body_sections(text: str) -> dict[str, str]:
    """`## Why` / `## Edit` bodies. The Edit text is verbatim: one leading/trailing newline goes."""
    out: dict[str, str] = {}
    hits = list(_HEADING.finditer(text))
    for i, hit in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        chunk = text[hit.end() : end]
        chunk = chunk[1:] if chunk.startswith("\n") else chunk
        chunk = chunk[1:] if chunk.startswith("\n") else chunk
        chunk = chunk[:-1] if chunk.endswith("\n") else chunk
        out[hit.group(1).lower()] = chunk
    return out


def _suggestion(path: Path, i: int, a: Assembled) -> None:
    label = f"suggestions/{path.name}"
    prefix = f"suggestions[{i}]"
    a.labels[prefix] = label
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    a.sources[prefix] = text
    if not re.match(r"^S\d+$", path.stem):
        a.errors.append(f"{label}: file name must be S<number>.md (S1.md, S2.md, ...)")
        return
    if not text.startswith("---\n"):
        a.errors.append(f"{label}:1: must start with a YAML frontmatter block ('---' on the first line)")
        return
    end = text.find("\n---", 3)
    if end == -1:
        a.errors.append(f"{label}: the frontmatter block is never closed (add a '---' line before ## Why)")
        return
    try:
        front = yaml.safe_load(text[4 : end + 1])
    except yaml.YAMLError as exc:
        a.errors.append(f"{label}: frontmatter is not valid YAML ({' '.join(str(exc).split())})")
        return
    if not isinstance(front, dict):
        a.errors.append(f"{label}: frontmatter must be a block of key: value pairs")
        return
    sections = _body_sections(text[end + 4 :])
    if "why" not in sections or not sections["why"].strip():
        a.errors.append(f"{label}: '## Why' section is missing (one paragraph, the owner's reason)")
        return
    data = dict(front)
    data["id"] = path.stem
    data["why"] = sections["why"].strip()
    if "edit" in sections:
        data["text"] = sections["edit"]
    a.data["suggestions"].append(data)


def _learnings(path: Path, a: Assembled) -> None:
    for n, raw in enumerate(path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(("- ", "* ")):
            a.errors.append(f"learnings.md:{n}: expected a bullet '- <target>: <observation> -> <change>', got {line[:60]!r}")
            continue
        body = line[2:]
        target, _, rest = body.partition(":")
        target = target.strip()
        if target not in TARGETS:
            a.errors.append(f"learnings.md:{n}: unknown target {target!r} (one of: {', '.join(TARGETS)})")
            continue
        parts = re.split(r"\s(?:->|→)\s", rest.strip(), maxsplit=1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            a.errors.append(f"learnings.md:{n}: needs '<observation> -> <proposed change>'")
            continue
        i = len(a.data["learnings"])
        a.labels[f"learnings[{i}]"] = f"learnings.md:{n}"
        a.sources[f"learnings[{i}]"] = raw
        a.data["learnings"].append({"target": target, "observation": parts[0].strip(), "proposed_change": parts[1].strip()})


def assemble(out_dir: str | Path) -> Assembled:
    out = Path(out_dir)
    a = Assembled()
    _classification(out / "classification.tsv", a)
    files = sorted((out / "suggestions").glob("S*.md"), key=lambda p: (len(p.stem), p.stem))
    for i, path in enumerate(files):
        _suggestion(path, i, a)
    headline = out / "headline.txt"
    if headline.exists():
        a.data["headline"] = " ".join(headline.read_text(encoding="utf-8").split())
        a.labels["headline"] = "headline.txt"
        a.sources["headline"] = headline.read_text(encoding="utf-8")
    elif a.data["suggestions"]:
        a.errors.append("headline.txt: missing (one sentence naming the gaps the suggestions close)")
    if (out / "learnings.md").exists():
        _learnings(out / "learnings.md", a)
    return a
