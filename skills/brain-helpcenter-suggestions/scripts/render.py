#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2"]
# ///
"""Render `suggestions.json` + `evidence.json` into one self-contained report.

    uv run render.py DIR/suggestions.json [--evidence DIR/evidence.json]

Writes `report.html` (for the help-centre owner) and `learnings.md` (for the
skill's iteration log) next to suggestions.json. Validates first: on any error
nothing is written. Page chrome is English; quotes and proposed edits stay in
the language the customer and the agent used. Everything is escaped — the
agent never supplies HTML.
"""

from __future__ import annotations

import argparse
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schema import WEIGHT, load, rank, summary, validation_errors  # noqa: E402

KIND_COLOR = {
    "new": ("#e8f5ee", "#2f6f4f"), "rewrite": ("#fbf1e3", "#7a4a12"),
    "retitle": ("#eef2fa", "#33518c"), "add_alias": ("#f2ecfb", "#5b3a8a"),
    "merge": ("#e6f4f5", "#1f6b73"), "delete": ("#fdeceb", "#b42318"),
}
VERDICT_LABEL = {"answered": "answered", "partial": "partial", "missing": "missing",
                 "wrong_title": "wrong title", "not_kb": "not KB", "uncertain": "uncertain"}
TILES = [("scanned", "scanned"), ("howto", "how-to"), ("answered", "answered"), ("partial", "partial"),
         ("missing", "missing"), ("wrong_title", "wrong title"), ("uncertain", "uncertain"), ("not_kb", "not KB")]

CSS = """
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:#f6f7f9;color:#101828;font-size:16px;line-height:1.55;
 font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:40px 20px 80px}
a{color:#33518c}
h1{margin:0;font-size:26px;font-weight:600;letter-spacing:-0.01em}
h2{margin:0 0 14px;font-size:12px;text-transform:uppercase;letter-spacing:.12em;color:#667085;font-weight:700}
h3{margin:2px 0 6px;font-size:18px;font-weight:600}
p{margin:0}
section{margin-top:40px}
.stamp{color:#667085;font-size:13px;margin-top:6px}
.lede{margin-top:20px;background:#fff;border:1px solid #e4e7ec;border-left:4px solid #33518c;
 border-radius:14px;padding:18px 20px;font-size:18px;line-height:1.45}
.tiles{display:flex;flex-wrap:wrap;gap:10px}
.tile{background:#fff;border:1px solid #e4e7ec;border-radius:12px;padding:12px 16px;min-width:104px}
.tile b{display:block;font-size:24px;line-height:1.1}
.tile span{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#667085;font-weight:700}
.card{background:#fff;border:1px solid #e4e7ec;border-radius:14px;padding:18px 20px;margin-bottom:12px}
.top{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.rank{flex:0 0 26px;height:26px;border-radius:50%;background:#33518c;color:#fff;font-size:12px;
 font-weight:700;display:flex;align-items:center;justify-content:center}
.badge{font-size:11px;font-weight:700;border-radius:999px;padding:3px 9px;text-transform:uppercase;letter-spacing:.06em}
.route{background:#fdf3e7;color:#b54708}
.score{color:#667085;font-size:13px;margin:2px 0 10px}
.edit{background:#f6f7f9;border-radius:10px;padding:12px 14px;margin:10px 0;font-size:14px}
.edit b{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#667085;display:block;margin-bottom:4px}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font-size:13.5px;line-height:1.5;
 font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.why{font-size:15px;color:#344054;margin:8px 0}
.quote{border-left:2px solid #e4e7ec;padding:2px 0 2px 12px;margin:10px 0;color:#344054;font-size:14.5px}
.quote a{font-size:12px;font-weight:700;text-decoration:none;margin-left:6px;white-space:nowrap}
.targets{font-size:14px;color:#667085}
details{margin-top:12px;border-top:1px solid #e4e7ec;padding-top:10px}
summary{cursor:pointer;font-size:13px;font-weight:600;color:#33518c}
table{width:100%;border-collapse:collapse;font-size:13px;background:#fff}
th,td{text-align:left;padding:7px 8px;border-top:1px solid #e4e7ec;vertical-align:top}
th{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:#667085;border-top:none}
button.copy{font:inherit;font-size:11px;font-weight:700;border:1px solid #e4e7ec;background:#fff;
 color:#33518c;border-radius:6px;padding:2px 8px;cursor:pointer;float:right}
.footer{margin-top:40px;font-size:12.5px;color:#667085}
.footer li{margin-bottom:4px}
"""
JS = """
document.querySelectorAll('button.copy').forEach(function(b){b.onclick=function(){
 navigator.clipboard.writeText(b.parentElement.querySelector('pre').innerText);
 b.textContent='copied';setTimeout(function(){b.textContent='copy';},1200);};});
"""


def e(text: str | None) -> str:
    return escape(text or "", quote=True)


def _worst(classifications) -> str:
    gaps = [c.verdict for c in classifications if c.verdict in WEIGHT]
    return VERDICT_LABEL[max(gaps, key=lambda v: WEIGHT[v])] if gaps else "no gap"


def _edit_block(s, arts) -> str:
    def block(label: str, body: str, pre: bool = False) -> str:
        copy = '<button class="copy">copy</button>' if pre else ""
        inner = f"<pre>{body}</pre>" if pre else body
        return f'<div class="edit"><b>{label}{copy}</b>{inner}</div>'

    out = []
    if s.kind == "retitle":
        out.append(block("New title", f"<pre>{e(s.title)}</pre>"))
    if s.kind == "add_alias":
        out.append(block("Add aliases", "<pre>" + e("\n".join(s.aliases)) + "</pre>"))
    if s.kind in ("merge", "delete") and s.destination:
        dest = arts[s.destination]
        out.append(block("Destination", f"{e(dest.id)} · {e(dest.title)}"))
    if s.text:
        label = f"New text — section “{e(s.section)}”" if s.section else "New text"
        out.append(block(label, e(s.text), pre=True))
    return "".join(out)


def _card(pos: int, s, sc: int, arts, by_topic, convs) -> str:
    bg, fg = KIND_COLOR[s.kind]
    topic_convs = by_topic.get(s.topic, [])
    links = " · ".join(
        f'<a href="{e(arts[a].url)}">{e(arts[a].title)}</a>' if arts[a].url else e(arts[a].title)
        for a in s.target_articles
    )
    route = '<span class="badge route">brain</span>' if s.route == "brain" else ""
    parts = [
        f'<div class="card"><div class="top"><span class="rank">{pos}</span>'
        f'<span class="badge" style="background:{bg};color:{fg}">{e(s.kind.replace("_", " "))}</span>{route}</div>',
        f"<h3>{e(s.title)}</h3>",
        f'<p class="score">{len(topic_convs)} conversation{"s" if len(topic_convs) != 1 else ""} · '
        f"{e(_worst(topic_convs))} · score {sc} · topic {e(s.topic)}</p>",
        f'<p class="targets">Article: {links}</p>' if links else "",
        _edit_block(s, arts),
        f'<p class="why">{e(s.why)}</p>',
    ]
    for q in s.evidence:
        conv = convs[q.conversation_id]
        link = f'<a href="{e(conv.url)}">conversation ↗</a>' if conv.url else ""
        parts.append(f'<div class="quote">“{e(q.quote)}”{link}</div>')
    if s.seed_reply:
        reply = convs[s.seed_reply].reply
        parts.append(
            f"<details><summary>Seed reply from {e(s.seed_reply)}"
            f"{' (' + e(reply.by) + ')' if reply and reply.by else ''}</summary>"
            f'<div class="edit"><pre>{e(reply.text if reply else "")}</pre></div></details>'
        )
    return "".join(parts) + "</div>"


def render_html(sug, ev) -> str:
    convs_by_id = {c.id: c for c in ev.conversations}
    arts = {a.id: a for a in ev.articles}
    by_topic: dict[str, list] = {}
    for c in sug.classification:
        if c.topic:
            by_topic.setdefault(c.topic, []).append(c)
    tiles = summary(ev, sug)
    cov = " · ".join(f"{f.feed}: {f.status}" for f in ev.coverage) or "no coverage recorded"

    body = [
        f"<h1>{e(ev.project)} — help centre suggestions</h1>",
        f'<p class="stamp">{e(ev.window.start[:10])} → {e(ev.window.end[:10])} ({ev.window.days} days) · source {e(ev.source)} · '
        f"{e(cov)} · KB {e(ev.kb.status)} ({ev.kb.articles} articles)</p>",
        f'<div class="lede">{e(sug.headline)}</div>',
        '<section><h2>This window</h2><div class="tiles">'
        + "".join(f'<div class="tile"><b>{tiles[k]}</b><span>{e(label)}</span></div>' for k, label in TILES)
        + "</div></section>",
    ]
    if tiles["top_topics"]:
        rows = "".join(f"<tr><td>{e(t)}</td><td>{n}</td></tr>" for t, n in tiles["top_topics"])
        body.append(f"<section><h2>Top unanswered topics</h2><table><tr><th>topic</th>"
                    f"<th>conversations</th></tr>{rows}</table></section>")
    cards = "".join(_card(pos, s, sc, arts, by_topic, convs_by_id) for pos, s, sc in rank(sug.suggestions, sug.classification))
    body.append(f"<section><h2>Suggested edits ({len(sug.suggestions)})</h2>{cards or '<p>No suggestions.</p>'}</section>")

    rows = []
    for c in sorted(sug.classification, key=lambda c: convs_by_id[c.conversation_id].created_at):
        conv = convs_by_id[c.conversation_id]
        cid = f'<a href="{e(conv.url)}">{e(conv.id)}</a>' if conv.url else e(conv.id)
        titles = ", ".join(e(arts[a].title) for a in c.article_ids)
        rows.append(f"<tr><td>{cid}</td><td>{e(conv.created_at[:16])}</td><td>{e(conv.channel)}</td>"
                    f"<td>{e(VERDICT_LABEL[c.verdict])}</td><td>{e(c.topic)}</td><td>{titles}</td></tr>")
    body.append(
        f"<section><h2>Detail</h2><details><summary>Classification of all {len(sug.classification)} "
        "conversations</summary><table><tr><th>conversation</th><th>date</th><th>channel</th>"
        f"<th>verdict</th><th>topic</th><th>articles</th></tr>{''.join(rows)}</table></details></section>"
    )
    learn = "".join(f"<li><b>{e(l.observation)}</b> → {e(l.proposed_change)} ({e(l.target)})</li>"
                    for l in sug.learnings)
    body.append(
        f'<div class="footer"><h2>Notes</h2>'
        + (f"<ul>{learn}</ul>" if learn else "<p>No learnings recorded.</p>")
        + f"<p>Coverage: {e(cov)}. KB: {e(ev.kb.status)}, {ev.kb.articles} articles, "
        f"{ev.kb.brain_docs} brain documents. Collected {e(ev.collected_at)} from {e(ev.source)}.</p></div>"
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{e(ev.project)} — help centre suggestions</title><style>{CSS}</style></head>"
        f'<body><div class="wrap">{"".join(body)}</div><script>{JS}</script></body></html>'
    )


def render_learnings(sug, ev) -> str:
    lines = [f"# Learnings — {ev.project} ({ev.window.start} → {ev.window.end}, source {ev.source})", ""]
    if not sug.learnings:
        lines.append("_No learnings recorded for this run._")
    for l in sug.learnings:
        lines += [f"- **{l.target}** — {l.observation}", f"  - proposed: {l.proposed_change}"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Render suggestions.json into report.html")
    parser.add_argument("suggestions", type=Path)
    parser.add_argument("--evidence", type=Path, help="default: evidence.json next to suggestions.json")
    args = parser.parse_args()
    evidence = args.evidence or args.suggestions.parent / "evidence.json"

    errors = validation_errors(args.suggestions, evidence)
    if errors:
        print(f"{len(errors)} error(s) in {args.suggestions} — nothing rendered:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    sug, ev = load(args.suggestions, evidence)
    out_dir = args.suggestions.parent
    html_path = out_dir / "report.html"
    md_path = out_dir / "learnings.md"
    html_path.write_text(render_html(sug, ev), encoding="utf-8")
    md_path.write_text(render_learnings(sug, ev), encoding="utf-8")
    print(html_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
