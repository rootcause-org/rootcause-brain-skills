#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Pieces both intake validators share: TSV reading, frontmatter, markdown hygiene.

`brain-source-intake/scripts/validate.py` imports this directly (same directory);
`brain-schema-intake/scripts/validate.py` imports it by sibling path. Keep it free of
anything specific to either intake so neither validator drifts from the other.
"""

from __future__ import annotations

import re
from pathlib import Path

EMOJI = re.compile("[\U0001f300-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f]")
SECRETS = (
    (re.compile(r"AKIA[0-9A-Z]{8,}"), "an AWS access key id"),
    (re.compile(r"sk_live_[A-Za-z0-9]"), "a live secret key"),
    (re.compile(r"-----BEGIN"), "a PEM block"),
    (re.compile(r"password\s*=\s*\S"), "a password assignment"),
    (re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:]+:[^/\s@]+@"), "a DSN with credentials"),
)


def read_tsv(path: Path, columns: tuple[str, ...], errors: list[str]) -> list[dict]:
    """Rows as dicts, header enforced. Short rows are padded, long rows are an error."""
    label = path.name
    lines = path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    rows: list[dict] = []
    header_seen = False
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        cells = line.split("\t")
        if not header_seen:
            header_seen = True
            if [c.strip() for c in cells] != list(columns):
                errors.append(f"{label}: line 1: header must be exactly "
                              f"{chr(9).join(columns)!r}, got {line!r}")
            continue
        if len(cells) > len(columns):
            errors.append(f"{label}: line {number}: {len(cells)} columns, expected "
                          f"{len(columns)} ({', '.join(columns)})")
            continue
        cells = cells + [""] * (len(columns) - len(cells))
        row = {name: cells[index].strip() for index, name in enumerate(columns)}
        row["_line"] = number
        rows.append(row)
    if not header_seen:
        errors.append(f"{label}: line 1: missing the header row")
    return rows


def split_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def frontmatter_description(text: str) -> str | None:
    """The `description:` of a leading `---` block, or None when there is no usable frontmatter."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    for line in text[3:end].splitlines():
        if line.strip().startswith("description:"):
            value = line.split(":", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


def check_markdown_hygiene(label: str, text: str, errors: list[str],
                           max_lines: int, max_line_chars: int) -> list[str]:
    """The rules every proposed brain file obeys. Returns the lines, so callers can reuse them."""
    lines = text.split("\n")
    if len(lines) > max_lines + 1:
        errors.append(f"{label}: {len(lines)} lines, the cap is {max_lines}; split it into "
                      "another file")
    for number, line in enumerate(lines, start=1):
        if line.startswith("```"):
            errors.append(f"{label}: line {number}: fenced code block; name paths and "
                          "methods instead of pasting code")
        if len(line) > max_line_chars:
            errors.append(f"{label}: line {number}: {len(line)} characters, the cap is "
                          f"{max_line_chars}")
        if "\u2014" in line:
            errors.append(f"{label}: line {number}: em dash; use a comma, a colon or a full stop")
        for index, char in enumerate(line):
            if char == "\u2013" and not (index and line[index - 1].isdigit()
                                         and index + 1 < len(line) and line[index + 1].isdigit()):
                errors.append(f"{label}: line {number}: en dash outside a number range; "
                              "use a comma, a colon or a full stop")
                break
        if EMOJI.search(line):
            errors.append(f"{label}: line {number}: emoji; brain docs stay plain text")
        for pattern, what in SECRETS:
            if pattern.search(line):
                errors.append(f"{label}: line {number}: looks like {what}; never put a "
                              "credential in a brain doc")
    return lines
