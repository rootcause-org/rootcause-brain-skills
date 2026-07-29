# /// script
# requires-python = ">=3.11"
# ///
"""Render sent-vs-proposed delta evidence as a reviewable, word-level-diff HTML report.

`rc dev learning evidence --plane deltas --include-bodies` returns the two bodies that matter for a
dream cycle: what the brain proposed and what the human actually sent. JSON is unreadable for a
human reviewer and a line diff is wrong for prose — humans rewrite *inside* sentences, so a
line-granular diff paints a whole paragraph red/green and hides the actual edit.

This script produces one self-contained HTML file that shows, per delta:

  * the run/thread identifiers needed to drill further (`rc run debug <id>`);
  * a paragraph alignment (fuzzy, so a rewritten paragraph stays paired with its original);
  * a word-level inline diff inside each aligned pair, plus a side-by-side view of the same data;
  * kept/removed/added word counts, so "polish" and "full rewrite" are distinguishable at a glance;
  * the agent's per-delta conclusion next to the evidence it was drawn from.

    sent_delta_report.py --limit 20
    sent_delta_report.py --limit 20 --out .rootcause/dream/2026-07-29-sent-deltas.html
    sent_delta_report.py --from-json deltas.json --annotations notes.json --conclusion concl.md

Read-only: it shells out to `rc` for a GET, or reads a JSON file, and writes one local HTML file.

PRIVACY: the output embeds raw customer mail. Keep it under the gitignored `.rootcause/` tree
(the default), never commit it, and delete it when the dream cycle is done.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# A token is one word (or one punctuation mark) plus the whitespace that follows it. Keeping
# whitespace attached matters: with whitespace as its own token every "equal" space would split an
# otherwise contiguous edit into a string of separate red/green fragments.
TOKEN_RE = re.compile(r"(\w+|[^\w\s])(\s*)", re.UNICODE)
PARA_SPLIT_RE = re.compile(r"\n\s*\n")
# Below this paragraph similarity two paragraphs are unrelated, so pairing them would produce a
# word diff that is noisier than showing them as a clean delete + insert.
PAIR_THRESHOLD = 0.34
# Where a reply's quoted history starts. Sent bodies arrive with the full quoted thread appended;
# diffing that against a proposal that never contained it would drown the real edit in green.
QUOTE_START_RE = re.compile(
    r"""^(?:>.*
      |-{2,}\s*(?:Original\s+Message|Oorspronkelijk\s+bericht|Forwarded\s+message)\s*-{2,}
      |(?:From|Van|Von)\s*:\s*.+<.+@.+>)\s*$""",
    re.IGNORECASE | re.VERBOSE,
)
# An attribution line ("Op <date> schreef <name> <addr>:") sits directly above the quote block and
# often wraps over two lines, so it is absorbed by walking backwards from the block.
ATTRIBUTION_RE = re.compile(
    r"[\w.+-]+@[\w.-]+|\b(?:schreef|wrote|geschreven|a\s+écrit|escribió|schrieb)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------- evidence loading


def load_evidence(args: argparse.Namespace) -> dict:
    if args.from_json:
        raw = sys.stdin.read() if args.from_json == "-" else Path(args.from_json).read_text("utf-8")
        return json.loads(raw)
    cmd = ["rc", "dev", "learning", "evidence", "--plane", "deltas", "--include-bodies",
           "--limit", str(args.limit), "-o", "json"]
    if args.project:
        cmd = ["rc", "--project", args.project, *cmd[1:]]
    if args.tenant:
        cmd = [cmd[0], "--tenant", args.tenant, *cmd[1:]]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"rc failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return json.loads(proc.stdout)


def local_scope(cwd: Path) -> tuple[str, str]:
    """Best-effort project/tenant label from the brain checkout's committed marker."""
    marker = cwd / ".rootcause.toml"
    if not marker.is_file():
        return "", ""
    try:
        data = tomllib.loads(marker.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "", ""
    return str(data.get("project", "")), str(data.get("tenant", ""))


# ---------------------------------------------------------------------------- diff model


@dataclass
class Row:
    """One aligned paragraph pair: equal, changed (word diff), deleted, or inserted."""

    kind: str  # equal | changed | delete | insert
    left: list[tuple[str, str]] = field(default_factory=list)   # (op, text) for the proposed side
    right: list[tuple[str, str]] = field(default_factory=list)  # (op, text) for the sent side
    inline: list[tuple[str, str]] = field(default_factory=list)  # both sides interleaved in order


@dataclass
class Diff:
    rows: list[Row]
    kept: int
    removed: int
    added: int
    quoted: str = ""  # quoted history stripped off the sent body, shown but not diffed

    @property
    def total(self) -> int:
        return self.kept + self.removed + self.added

    @property
    def similarity(self) -> float:
        return (2 * self.kept / (2 * self.kept + self.removed + self.added)) if self.total else 1.0

    @property
    def shape(self) -> str:
        """A one-word verdict a reviewer can scan without reading the diff."""
        if not self.removed and not self.added:
            return "identical"
        if self.similarity >= 0.85:
            return "polish"
        if self.similarity >= 0.5:
            return "reworked"
        if self.kept and self.similarity >= 0.2:
            return "heavy rewrite"
        return "replaced"


def split_quotes(text: str) -> tuple[str, str]:
    """Split a body into (new text, quoted history). The trailer is shown, but never diffed."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if not QUOTE_START_RE.match(line.strip()):
            continue
        start = i
        while start > 0 and not lines[start - 1].strip():
            start -= 1  # blank lines between the attribution and the quote block
        if start == 0 or not ATTRIBUTION_RE.search(lines[start - 1]):
            start = i
        while start > 0 and lines[start - 1].strip() and ATTRIBUTION_RE.search(lines[start - 1]):
            start -= 1  # the attribution itself, which often wraps over two lines
        return "\n".join(lines[:start]).rstrip(), "\n".join(lines[start:]).strip()
    return text or "", ""


def paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in PARA_SPLIT_RE.split((text or "").replace("\r\n", "\n"))]
    return [p for p in parts if p]


def tokens(text: str) -> list[tuple[str, str]]:
    """Split into (word-or-punctuation, trailing whitespace) pairs."""
    return TOKEN_RE.findall(text)


def _keys(toks: list[tuple[str, str]]) -> list[str]:
    return [core.casefold() for core, _ in toks]


def _words(toks: list[tuple[str, str]]) -> int:
    return sum(1 for core, _ in toks if core.isalnum() or "_" in core)


def _join(toks: list[tuple[str, str]]) -> str:
    return "".join(core + ws for core, ws in toks)


def word_diff(a: str, b: str) -> tuple[Row, int, int, int]:
    """Word-level diff of two paragraphs; returns (row, kept, removed, added)."""
    at, bt = tokens(a), tokens(b)
    matcher = difflib.SequenceMatcher(None, _keys(at), _keys(bt), autojunk=False)
    row = Row("changed")
    kept = removed = added = 0
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            row.left.append(("eq", _join(at[i1:i2])))
            row.right.append(("eq", _join(bt[j1:j2])))
            row.inline.append(("eq", _join(at[i1:i2])))
            kept += _words(at[i1:i2])
            continue
        if op in ("delete", "replace"):
            row.left.append(("del", _join(at[i1:i2])))
            row.inline.append(("del", _join(at[i1:i2])))
            removed += _words(at[i1:i2])
        if op in ("insert", "replace"):
            row.right.append(("ins", _join(bt[j1:j2])))
            row.inline.append(("ins", _join(bt[j1:j2])))
            added += _words(bt[j1:j2])
    return row, kept, removed, added


def align(a_paras: list[str], b_paras: list[str]) -> list[tuple[int | None, int | None]]:
    """Fuzzy paragraph alignment (Needleman-Wunsch over similarity ratios).

    Exact-match alignment would pair almost nothing in rewritten prose; this keeps a paragraph
    paired with its rewritten self so the word diff below it is meaningful, while genuinely new or
    dropped paragraphs fall out as unpaired.
    """
    n, m = len(a_paras), len(b_paras)
    ratios = [[0.0] * m for _ in range(n)]
    for i, pa in enumerate(a_paras):
        ka = _keys(tokens(pa))
        for j, pb in enumerate(b_paras):
            matcher = difflib.SequenceMatcher(None, ka, _keys(tokens(pb)), autojunk=False)
            if matcher.real_quick_ratio() < PAIR_THRESHOLD or matcher.quick_ratio() < PAIR_THRESHOLD:
                continue
            r = matcher.ratio()
            ratios[i][j] = r if r >= PAIR_THRESHOLD else 0.0

    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            pair = dp[i - 1][j - 1] + ratios[i - 1][j - 1] if ratios[i - 1][j - 1] else -1.0
            dp[i][j] = max(dp[i - 1][j], dp[i][j - 1], pair)

    pairs: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        r = ratios[i - 1][j - 1] if i > 0 and j > 0 else 0.0
        if i > 0 and j > 0 and r and abs(dp[i][j] - (dp[i - 1][j - 1] + r)) < 1e-9:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif j > 0 and (i == 0 or dp[i][j - 1] >= dp[i - 1][j]):
            pairs.append((None, j - 1))
            j -= 1
        else:
            pairs.append((i - 1, None))
            i -= 1
    pairs.reverse()
    return pairs


def build_diff(proposed: str, sent: str, keep_quotes: bool = False) -> Diff:
    quoted = ""
    if not keep_quotes:
        proposed, _ = split_quotes(proposed)
        sent, quoted = split_quotes(sent)
    a_paras, b_paras = paragraphs(proposed), paragraphs(sent)
    rows: list[Row] = []
    kept = removed = added = 0
    for ai, bj in align(a_paras, b_paras):
        if ai is not None and bj is not None:
            a, b = a_paras[ai], b_paras[bj]
            if a == b:
                rows.append(Row("equal", [("eq", a)], [("eq", b)], [("eq", a)]))
                kept += _words(tokens(a))
                continue
            row, k, r, d = word_diff(a, b)
            if not r and not d:
                # Same words, different line wrapping — sent bodies come back hard-wrapped.
                row.kind = "equal"
            rows.append(row)
            kept, removed, added = kept + k, removed + r, added + d
        elif ai is not None:
            rows.append(Row("delete", [("del", a_paras[ai])], [], [("del", a_paras[ai])]))
            removed += _words(tokens(a_paras[ai]))
        else:
            rows.append(Row("insert", [], [("ins", b_paras[bj])], [("ins", b_paras[bj])]))
            added += _words(tokens(b_paras[bj]))
    return Diff(rows, kept, removed, added, quoted)


# ---------------------------------------------------------------------------- rendering

CSS = """
:root{--bg:#fff;--fg:#1b1f24;--muted:#5b6570;--line:#d8dee4;--card:#fff;--sub:#f6f8fa;
--del-bg:#ffe3e3;--del-fg:#8b1a1a;--ins-bg:#dcf5e3;--ins-fg:#0f5427;--accent:#0b62d6;}
@media (prefers-color-scheme:dark){:root{--bg:#0f1418;--fg:#e6edf3;--muted:#9aa7b2;--line:#2b333c;
--card:#161c22;--sub:#11171c;--del-bg:#4a1f22;--del-fg:#ffb4b4;--ins-bg:#173a26;--ins-fg:#9ff0bd;
--accent:#68a9ff;}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0}
.wrap{max-width:1180px;margin:0 auto}
.banner{border:1px solid #c9a227;background:#fff8e1;color:#5c4400;padding:8px 12px;border-radius:6px;
margin:12px 0;font-size:12.5px}
@media (prefers-color-scheme:dark){.banner{background:#2e2611;color:#f0dfa8;border-color:#6b5a1e}}
.meta{color:var(--muted);font-size:12.5px}
.controls{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0;border-bottom:1px solid var(--line);
display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
button{font:inherit;padding:4px 10px;border:1px solid var(--line);background:var(--card);color:var(--fg);
border-radius:6px;cursor:pointer}
button[aria-pressed=true]{border-color:var(--accent);color:var(--accent)}
.card{border:1px solid var(--line);border-radius:8px;background:var(--card);margin:0 0 20px;overflow:hidden}
.head{padding:12px 14px;background:var(--sub);border-bottom:1px solid var(--line)}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.tag{font-size:11.5px;border:1px solid var(--line);border-radius:999px;padding:1px 8px;color:var(--muted)}
.tag.shape{border-color:var(--accent);color:var(--accent)}
code,kbd,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.cmd{margin-top:8px;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;
overflow-x:auto;white-space:pre}
.note{margin:0;padding:10px 14px;border-bottom:1px solid var(--line);background:var(--sub);
white-space:pre-wrap}
.note b{color:var(--accent)}
.body{padding:4px 0}
table{width:100%;border-collapse:collapse;table-layout:fixed}
td{vertical-align:top;padding:8px 14px;white-space:pre-wrap;overflow-wrap:anywhere;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
td.l{border-right:1px solid var(--line)}
tr.equal td{color:var(--muted)}
del{background:var(--del-bg);color:var(--del-fg);text-decoration:line-through;border-radius:2px}
ins{background:var(--ins-bg);color:var(--ins-fg);text-decoration:none;border-radius:2px}
.colhead td{background:var(--sub);color:var(--muted);font-size:11.5px;text-transform:uppercase;
letter-spacing:.04em;white-space:normal;border-bottom:1px solid var(--line)}
body.inline .side,body.inline .colhead{display:none}
body:not(.inline) .inlineview{display:none}
body.hide-equal tr.equal{display:none}
.empty{color:var(--muted);font-style:italic}
.quoted{border-top:1px solid var(--line);background:var(--sub)}
.quoted summary{padding:8px 14px;cursor:pointer;color:var(--muted);font-size:12.5px}
.quoted pre{margin:0;padding:0 14px 12px;white-space:pre-wrap;overflow-wrap:anywhere;
color:var(--muted);font-size:12px}
"""

JS = """
const b=document.body;
function set(view){b.classList.toggle('inline',view==='inline');
document.querySelectorAll('[data-view]').forEach(x=>x.setAttribute('aria-pressed',x.dataset.view===view));}
document.querySelectorAll('[data-view]').forEach(x=>x.onclick=()=>set(x.dataset.view));
const eq=document.getElementById('eq');
eq.onclick=()=>{b.classList.toggle('hide-equal');eq.setAttribute('aria-pressed',b.classList.contains('hide-equal'));};
set('side');
"""


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def spans(ops: list[tuple[str, str]]) -> str:
    out = []
    for op, text in ops:
        if op == "eq":
            out.append(esc(text))
            continue
        # Keep trailing whitespace outside the marker so the highlight ends on the last word.
        body, trail = text.rstrip(), text[len(text.rstrip()):]
        tag = "del" if op == "del" else "ins"
        out.append(f"<{tag}>{esc(body)}</{tag}>{esc(trail)}" if body else esc(text))
    return "".join(out) or '<span class="empty">—</span>'


def render_rows(diff: Diff) -> str:
    side = ['<table class="side"><tr class="colhead"><td class="l">Proposed draft (brain)</td>'
            "<td>Actually sent (human)</td></tr>"]
    inline = ['<table class="inlineview">']
    for row in diff.rows:
        side.append(f'<tr class="{row.kind}"><td class="l">{spans(row.left)}</td>'
                    f"<td>{spans(row.right)}</td></tr>")
        inline.append(f'<tr class="{row.kind}"><td>{spans(row.inline)}</td></tr>')
    side.append("</table>")
    inline.append("</table>")
    return "".join(side) + "".join(inline)


def fmt_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return value or "—"


def render_card(index: int, item: dict, diff: Diff, note: str) -> str:
    run_id = item.get("related_run_id") or ""
    tags = [
        f'<span class="tag shape">{esc(diff.shape)}</span>',
        f'<span class="tag">kept {diff.kept}w · −{diff.removed}w · +{diff.added}w</span>',
        f'<span class="tag">word similarity {diff.similarity:.0%}</span>',
    ]
    if item.get("similarity") is not None:
        tags.append(f'<span class="tag">server similarity {float(item["similarity"]):.0%}</span>')
    if item.get("channel"):
        tags.append(f'<span class="tag">{esc(str(item["channel"]))}</span>')
    if item.get("turn_index") is not None:
        tags.append(f'<span class="tag">turn {item["turn_index"]}</span>')

    ids = [f"thread {item.get('thread_id') or '—'}", f"session {item.get('session_id') or '—'}"]
    cmd = (f"rc run debug {run_id}" if run_id
           else "no related run — drill via thread/session (Embassy-sourced delta)")
    blocks = [
        f'<div class="head"><h2>#{index} · {esc(fmt_time(item.get("sent_at", "")))} · '
        f'{esc(str(item.get("sender") or "unknown sender"))}</h2>'
        f'<div class="tags">{"".join(tags)}</div>'
        f'<div class="meta mono" style="margin-top:6px">{esc(" · ".join(ids))}</div>'
        f'<div class="cmd mono">{esc(cmd)}</div></div>'
    ]
    if item.get("delta_description"):
        blocks.append(f'<p class="note"><b>Server delta note:</b> '
                      f'{esc(str(item["delta_description"]))}</p>')
    if note:
        blocks.append(f'<p class="note"><b>Conclusion:</b> {esc(note)}</p>')
    blocks.append(f'<div class="body">{render_rows(diff)}</div>')
    if diff.quoted:
        blocks.append(f"<details class=\"quoted\"><summary>Quoted history on the sent message "
                      f"({len(diff.quoted.splitlines())} lines, excluded from the diff)</summary>"
                      f"<pre>{esc(diff.quoted)}</pre></details>")
    return f'<div class="card">{"".join(blocks)}</div>'


def render(items: list[dict], diffs: list[Diff], notes: dict[str, str], scope: str,
           conclusion: str) -> str:
    shapes: dict[str, int] = {}
    for d in diffs:
        shapes[d.shape] = shapes.get(d.shape, 0) + 1
    summary = " · ".join(f"{count}× {shape}" for shape, count in sorted(shapes.items(),
                                                                       key=lambda kv: -kv[1]))
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cards = "".join(render_card(i + 1, item, diff, notes.get(str(item.get("id")), ""))
                    for i, (item, diff) in enumerate(zip(items, diffs)))
    head = (f"<h1>Sent vs proposed — {esc(scope)}</h1>"
            f'<div class="meta">{len(items)} deltas · {esc(summary or "—")} · generated {generated}</div>'
            '<div class="banner">Raw customer mail. Keep under the gitignored <code>.rootcause/</code> '
            "tree, do not commit, delete when the dream cycle is done.</div>")
    if conclusion:
        head += f'<div class="card"><p class="note" style="border:0"><b>Conclusion</b>\n{esc(conclusion)}</p></div>'
    controls = ('<div class="controls"><span class="meta">View</span>'
                '<button data-view="side">Side by side</button>'
                '<button data-view="inline">Inline</button>'
                '<button id="eq" aria-pressed="false">Hide unchanged paragraphs</button>'
                '<span class="meta"><del>removed by human</del> · <ins>added by human</ins></span></div>')
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Sent vs proposed — {esc(scope)}</title><style>{CSS}</style></head>"
            f'<body class="inline-off"><div class="wrap">{head}{controls}{cards}</div>'
            f"<script>{JS}</script></body></html>")


# ---------------------------------------------------------------------------- entrypoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=20, help="deltas to fetch (server cap 100)")
    parser.add_argument("--project", default="", help="explicit project slug (all-projects token)")
    parser.add_argument("--tenant", default="", help="explicit tenant slug")
    parser.add_argument("--from-json", default="",
                        help="read `rc dev learning evidence` JSON from a file or '-' for stdin")
    parser.add_argument("--annotations", default="",
                        help='JSON map {"<delta-id>": "conclusion"} shown next to each delta')
    parser.add_argument("--conclusion", default="", help="markdown/text file shown at the top")
    parser.add_argument("--keep-quotes", action="store_true",
                        help="diff quoted reply history too (default: shown collapsed, not diffed)")
    parser.add_argument("--out", default="", help="output path (default .rootcause/dream/<ts>-sent-deltas.html)")
    args = parser.parse_args(argv)

    payload = load_evidence(args)
    items = payload.get("deltas") or []
    if not items:
        print("no sent-vs-proposed deltas in this scope/window", file=sys.stderr)
        return 1
    missing = [i for i in items if not (i.get("proposed_body") and i.get("sent_body"))]
    items = [i for i in items if i.get("proposed_body") and i.get("sent_body")]
    if not items:
        print("deltas carry no bodies — re-run with --include-bodies", file=sys.stderr)
        return 1

    notes: dict[str, str] = {}
    if args.annotations:
        notes = json.loads(Path(args.annotations).read_text("utf-8"))
        unknown = set(notes) - {str(i.get("id")) for i in items}
        if unknown:
            print(f"warning: annotations for unknown delta ids: {', '.join(sorted(unknown))}",
                  file=sys.stderr)
    conclusion = Path(args.conclusion).read_text("utf-8").strip() if args.conclusion else ""

    diffs = [build_diff(i["proposed_body"], i["sent_body"], args.keep_quotes) for i in items]
    order = sorted(range(len(items)), key=lambda k: diffs[k].similarity)
    items = [items[k] for k in order]
    diffs = [diffs[k] for k in order]

    project, tenant = args.project, args.tenant
    if not project:
        project, tenant_local = local_scope(Path.cwd())
        tenant = tenant or tenant_local
    scope = " / ".join(p for p in (project or "unknown project", tenant) if p)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    out = Path(args.out) if args.out else Path(".rootcause/dream") / f"{stamp}-sent-deltas.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(items, diffs, notes, scope, conclusion), "utf-8")
    if ".rootcause" not in out.resolve().parts:
        print(f"warning: {out} is outside .rootcause/ and holds raw customer mail — do not commit",
              file=sys.stderr)
    if missing:
        print(f"note: {len(missing)} delta(s) skipped for missing bodies", file=sys.stderr)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
