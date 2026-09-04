"""Row-list rendering shared by the reporting connectors (Meta Ads, Google Analytics).

``lib._output`` renders ``list[dict]`` for the db/cloudwatch CLIs. Reporting connectors build
positional ``list[list[str]]`` (header row first) because column ORDER and repeated/duplicate
header names (``date_start``, ``action:link_click``) matter more than key lookup. Kept tiny and
stdlib-only.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any


def table(rows: list[list[str]]) -> str:
    """Fixed-width aligned text; ``rows[0]`` is the header.

    Ragged rows are padded, not fatal: a provider that omits a field for one row must not turn a
    report into an IndexError.
    """
    width_count = max(len(r) for r in rows)
    padded = [list(r) + [""] * (width_count - len(r)) for r in rows]
    widths = [max(len(str(r[i])) for r in padded) for i in range(width_count)]
    return "\n".join(
        "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in padded
    )


def emit(rows: list[list[str]], as_csv: bool = False) -> None:
    """Print a header+rows table, or CSV when ``as_csv``. A header-only set prints ``(no rows)``."""
    if not rows or len(rows) == 1:
        print("(no rows)")
        return
    if as_csv:
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        print(buf.getvalue().rstrip())
    else:
        print(table(rows))


def dump(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def trunc(value: Any, width: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= width else text[: width - 1] + "…"
