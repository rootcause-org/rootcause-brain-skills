#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2"]
# ///
"""`uv run render.py DIR/suggestions.json` → `report.html` + `learnings.md` next to it.

Validates first; on any error nothing is written. Chrome is English, quotes and
proposed edits stay in the customer's language. Everything is escaped — the
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
*{box-sizing:border-box}
body{margin:0;background:#f6f7f9;color:#101828;font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:40px 20px 80px}
a{color:#33518c}h1{margin:0;font-size:26px}h3{margin:2px 0 6px;font-size:18px}p{margin:0}section{margin-top:40px}
h2,.tile span,th,.edit b,.badge{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#667085;font-weight:700}
h2{margin:0 0 14px}.stamp,.score,.targets,.footer{color:#667085;font-size:13px}.footer{margin-top:40px}
.card,.tile,.lede,table{background:#fff;border:1px solid #e4e7ec;border-radius:12px}
.lede{margin-top:20px;border-left:4px solid #33518c;padding:18px 20px;font-size:18px}
.tiles,.top{display:flex;flex-wrap:wrap;gap:10px}.top{align-items:center;gap:8px}
.tile{padding:12px 16px;min-width:104px}.tile b{display:block;font-size:24px}.card{padding:18px 20px;margin-bottom:12px}
.rank{flex:0 0 26px;height:26px;border-radius:50%;background:#33518c;color:#fff;display:flex;
 align-items:center;justify-content:center;font-size:12px;font-weight:700}
.badge{border-radius:999px;padding:3px 9px;letter-spacing:.06em}.route{background:#fdf3e7;color:#b54708}
.edit{background:#f6f7f9;border-radius:10px;padding:12px 14px;margin:10px 0;font-size:14px}
.edit b{display:block;margin-bottom:4px}.why{font-size:15px;color:#344054;margin:8px 0}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font:13.5px/1.5 ui-monospace,Menlo,monospace}
.quote{border-left:2px solid #e4e7ec;padding-left:12px;margin:10px 0;color:#344054}
.quote a,summary{font-size:12px;font-weight:700;text-decoration:none;color:#33518c}
.quote a{margin-left:6px;white-space:nowrap}
details{margin-top:12px;border-top:1px solid #e4e7ec;padding-top:10px}summary{cursor:pointer;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px;border-radius:0}
th,td{text-align:left;padding:7px 8px;border-top:1px solid #e4e7ec;vertical-align:top}th{border-top:none}
button.copy{font:inherit;font-size:11px;font-weight:700;border:1px solid #e4e7ec;background:#fff;color:#33518c;
 border-radius:6px;padding:2px 8px;cursor:pointer;float:right}
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


def _coverage(ev) -> str:
    """One line per feed, reason included — 'complete (63/121)' alone reads contradictory."""
    parts = [f"{f.feed}: {f.status}" + (f" — {f.reason}" if f.reason else "") for f in ev.coverage]
    return " · ".join(parts) or "no coverage recorded"


def _block(label: str, body: str, copyable: bool = False) -> str:
    copy = '<button class="copy">copy</button>' if copyable else ""
    return f'<div class="edit"><b>{label}{copy}</b><pre>{body}</pre></div>'


def _edit_block(s, arts) -> str:
    out = []
    if s.kind == "retitle":
        out.append(_block("New title", e(s.title)))
    if s.kind == "add_alias":
        out.append(_block("Add aliases", e("\n".join(s.aliases))))
    if s.kind in ("merge", "delete") and s.destination:
        out.append(_block("Destination", f"{e(arts[s.destination].id)} · {e(arts[s.destination].title)}"))
    if s.text:
        out.append(_block(f"New text — section “{e(s.section)}”" if s.section else "New text", e(s.text), True))
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


def _classification_table(sug, convs_by_id, arts) -> str:
    rows = []
    for c in sorted(sug.classification, key=lambda c: convs_by_id[c.conversation_id].created_at):
        conv = convs_by_id[c.conversation_id]
        cid = f'<a href="{e(conv.url)}">{e(conv.id)}</a>' if conv.url else e(conv.id)
        rows.append(
            f"<tr><td>{cid}</td><td>{e(conv.created_at[:16])}</td><td>{e(conv.channel)}</td>"
            f"<td>{e(VERDICT_LABEL[c.verdict])}</td><td>{e(', '.join(c.topics))}</td>"
            f"<td>{e(', '.join(arts[a].title for a in c.article_ids))}</td>"
            f"<td>{e(conv.noise.reason if conv.noise else '')}</td></tr>"
        )
    return (
        f"<section><h2>Detail</h2><details><summary>Classification of all {len(sug.classification)} "
        "conversations</summary><table><tr><th>conversation</th><th>date</th><th>channel</th>"
        "<th>verdict</th><th>topics</th><th>articles</th><th>noise hint</th></tr>"
        f"{''.join(rows)}</table></details></section>"
    )


def render_html(sug, ev) -> str:
    convs_by_id = {c.id: c for c in ev.conversations}
    arts = {a.id: a for a in ev.articles}
    by_topic: dict[str, list] = {}
    for c in sug.classification:
        for t in c.topics:  # a conversation counts once per topic it lists
            by_topic.setdefault(t, []).append(c)
    tiles, cov = summary(ev, sug), _coverage(ev)
    scope = f"{ev.project} · {ev.tenant}" if ev.tenant else ev.project

    body = [
        f"<h1>{e(scope)} — help centre suggestions</h1>",
        f'<p class="stamp">{e(ev.window.start[:10])} → {e(ev.window.end[:10])} ({ev.window.days} days) · '
        f"source {e(ev.source)} · KB {e(ev.kb.status)} ({ev.kb.articles} articles"
        f"{', ' + e(ev.kb.root) if ev.kb.root else ''})<br>{e(cov)}</p>",
        f'<div class="lede">{e(sug.headline)}</div>',
        '<section><h2>This window</h2><div class="tiles">'
        + "".join(f'<div class="tile"><b>{tiles[k]}</b><span>{e(label)}</span></div>' for k, label in TILES)
        + "</div></section>",
    ]
    if tiles["top_topics"]:
        rows = "".join(f"<tr><td>{e(t)}</td><td>{n}</td></tr>" for t, n in tiles["top_topics"])
        body.append("<section><h2>Top unanswered topics</h2><table><tr><th>topic</th>"
                    f"<th>conversations</th></tr>{rows}</table></section>")
    cards = "".join(_card(pos, s, sc, arts, by_topic, convs_by_id) for pos, s, sc in rank(sug.suggestions, sug.classification))
    body.append(f"<section><h2>Suggested edits ({len(sug.suggestions)})</h2>{cards or '<p>No suggestions.</p>'}</section>")
    body.append(_classification_table(sug, convs_by_id, arts))
    learn = "".join(f"<li><b>{e(l.observation)}</b> → {e(l.proposed_change)} ({e(l.target)})</li>" for l in sug.learnings)
    body.append(
        '<div class="footer"><h2>Notes</h2>'
        + (f"<ul>{learn}</ul>" if learn else "<p>No learnings recorded.</p>")
        + f"<p>Coverage: {e(cov)}. KB: {e(ev.kb.status)}, {ev.kb.articles} articles, "
        f"{ev.kb.brain_docs} brain documents. Collected {e(ev.collected_at)} from {e(ev.source)}.</p></div>"
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{e(scope)} — help centre suggestions</title><style>{CSS}</style></head>"
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
    for name, body in (("report.html", render_html(sug, ev)), ("learnings.md", render_learnings(sug, ev))):
        path = args.suggestions.parent / name
        path.write_text(body, encoding="utf-8")
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
