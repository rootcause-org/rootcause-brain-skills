"""Shared rendering for the lib CLIs (``python -m lib.db`` / ``python -m lib.cloudwatch``).

Row sets come back as ``list[dict]`` from ``db.query`` and ``cloudwatch.insights``; the CLIs
render them as CSV (default — compact, the LLM parses it cheaply), JSON, or a fixed-width table.
Large output spills to a file under ``/tmp`` and the path is printed instead, so a giant result
never blows the per-``bash`` output cap (``BASH_OUTPUT_CAP``).

Pure stdlib so it imports without the DB/AWS drivers — keeps the modules (and their tests)
loadable on a host that has neither.
"""

import csv
import io
import json
import os
import tempfile

# Above this many bytes, write to a file and print the path rather than flooding stdout. Kept
# under the host's default BASH_OUTPUT_CAP (65536) so the printed path itself never gets clipped.
SPILL_BYTES = 50_000


def render(rows: list[dict], fmt: str = "csv") -> str:
    """Render rows as ``csv`` | ``json`` | ``table``. Non-scalar cells are JSON-encoded for CSV/table."""
    if fmt == "json":
        return json.dumps(rows, indent=2, default=str)
    if not rows:
        return ""
    cols = list(rows[0].keys())
    if fmt == "table":
        return _table(rows, cols)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: _cell(r.get(c)) for c in cols})
    return buf.getvalue()


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return str(v)


def _table(rows: list[dict], cols: list[str]) -> str:
    widths = {c: len(c) for c in cols}
    cells = []
    for r in rows:
        row = {c: _cell(r.get(c)) for c in cols}
        for c in cols:
            widths[c] = max(widths[c], len(row[c]))
        cells.append(row)
    line = lambda row: "  ".join(row[c].ljust(widths[c]) for c in cols)
    head = "  ".join(c.ljust(widths[c]) for c in cols)
    sep = "  ".join("-" * widths[c] for c in cols)
    return "\n".join([head, sep, *(line(r) for r in cells)])


def emit(text: str, label: str = "out") -> None:
    """Print ``text``, or spill to a temp file and print its path when it exceeds ``SPILL_BYTES``."""
    if len(text.encode("utf-8", "replace")) <= SPILL_BYTES:
        print(text)
        return
    out_dir = os.path.join(tempfile.gettempdir(), "rootcause-out")
    os.makedirs(out_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=f"{label}-", suffix=".txt", dir=out_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"[result {len(text)} bytes — spilled to {path}; read it with the fs tools or `sed -n`]")
