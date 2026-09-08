#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""`uv run render.py OUT_DIR/intake.json` -> `report.html` next to it.

One page for the customer's developer: the questions we could not answer from the code, our
guess where we have one, and one box per question. It is self contained (inline CSS and JS,
no external URL) so it works from `file://` and travels as an attachment. Chrome is English;
the customer questions stay in their own language. Everything is escaped: the agent never
supplies HTML.

`build(data, flavour)` is shared with the sibling `brain-schema-intake` renderer: the flavour
dict carries the wording, the groups, the tiles, the status pills and any extra evidence
section, so both intakes render as one page with one copy button.
"""

from __future__ import annotations

import argparse
import json
import sys
from html import escape
from pathlib import Path

GROUPS = [
    ("architecture", "Architecture"),
    ("where", "Where does it live"),
    ("conventions", "Conventions"),
    ("db", "Database"),
    ("deploy", "Deploy and environments"),
]
TILES = [
    ("scanned", "questions scanned",
     "Real customer questions from the window we read, one row each in questions.tsv."),
    ("found", "found",
     "The answer has exactly one place in the code or the database. Nothing to ask."),
    ("ambiguous", "ambiguous",
     "Two or more places could answer it and we cannot tell which one is live. This is what "
     "most of the questions below are about."),
    ("missing", "missing",
     "We found nothing that would ground the answer. Either it lives somewhere we did not "
     "see, or it is not in the code at all."),
    ("n_a", "n/a",
     "No code or table would answer it: a sales or policy question. Listed for completeness."),
]
STATUSES = {
    "found": ("found", "s-found"),
    "ambiguous": ("ambiguous", "s-ambiguous"),
    "missing": ("missing", "s-missing"),
    "n/a": ("n/a", "s-na"),
}
INTRO = ("<p>These are the questions your codebase could not answer for us while we were "
         "building the support brain for it. Roughly fifteen minutes of your time, and one "
         "button at the end copies every answer in one go.</p>")

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f6f7f9;color:#101828;font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:40px 20px 140px}
h1{margin:0;font-size:26px}h2{margin:0 0 14px;font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#667085}
h3{margin:0 0 8px;font-size:17px}p{margin:0}section{margin-top:38px}
.stamp,.muted{color:#667085;font-size:13px}
.lede{margin-top:18px;background:#fff;border:1px solid #e4e7ec;border-left:4px solid #33518c;border-radius:12px;padding:16px 20px}
.lede p+p{margin-top:8px}
.tiles{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px}
.tile{background:#fff;border:1px solid #e4e7ec;border-radius:12px;padding:12px 16px;min-width:120px}
.tile b{display:block;font-size:24px}
.tile span{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#667085;font-weight:700}
.card{background:#fff;border:1px solid #e4e7ec;border-radius:12px;padding:18px 20px;margin-bottom:12px}
.idb{display:inline-block;background:#eef2fa;color:#33518c;border-radius:999px;padding:2px 9px;
 font-size:11px;font-weight:700;letter-spacing:.06em;margin-right:8px}
.guess{background:#fbfcfd;border:1px solid #d3d9e2;border-radius:10px;padding:12px 14px;margin:12px 0}
.guess b{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#667085;margin-bottom:4px}
.seg{display:inline-flex;border:1px solid #d3d9e2;border-radius:8px;overflow:hidden;margin-top:10px}
.seg label{font-size:12px;font-weight:700;color:#667085;padding:5px 14px;cursor:pointer;background:#fff;
 border-right:1px solid #e4e7ec}
.seg label:last-child{border-right:0}
.seg input{position:absolute;opacity:0;pointer-events:none}
.seg input:checked+span{color:#fff}
.seg label:has(input:checked){background:#33518c;color:#fff}
.chips{margin:10px 0 2px}
.chip{display:inline-block;background:#f2f4f7;color:#475467;border-radius:6px;padding:2px 8px;margin:0 6px 6px 0;
 font:12px/1.5 ui-monospace,Menlo,monospace;word-break:break-all}
textarea{width:100%;margin-top:10px;font:15px/1.5 inherit;padding:10px 12px;border:1px solid #d3d9e2;
 border-radius:8px;resize:vertical;background:#fff;color:#101828}
textarea:focus{outline:2px solid #33518c;outline-offset:1px}
details{background:#fff;border:1px solid #e4e7ec;border-radius:12px;padding:14px 18px;margin-top:12px}
details details{margin-top:8px;border-radius:8px}
summary{cursor:pointer;font-weight:700;font-size:14px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}
th,td{text-align:left;padding:7px 8px;border-top:1px solid #e4e7ec;vertical-align:top}
th{border-top:none;font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#667085}
td.loc{font:12px/1.5 ui-monospace,Menlo,monospace;word-break:break-all}
.pill{display:inline-block;border-radius:999px;padding:2px 9px;font-size:11px;font-weight:700}
.s-found{background:#e8f5ee;color:#2f6f4f}.s-ambiguous{background:#fbf1e3;color:#7a4a12}
.s-missing{background:#fdeceb;color:#b42318}.s-na{background:#f2f4f7;color:#667085}
pre{margin:8px 0 0;white-space:pre-wrap;word-break:break-word;background:#f6f7f9;border-radius:8px;
 padding:12px 14px;font:13px/1.5 ui-monospace,Menlo,monospace;color:#344054}
.bar{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid #e4e7ec;
 padding:12px 20px;display:flex;gap:12px;align-items:center;justify-content:center}
button{font:inherit;cursor:pointer;border-radius:8px}
button.primary{background:#33518c;color:#fff;border:0;padding:11px 22px;font-weight:700;font-size:15px}
button.ghost{background:#fff;color:#33518c;border:1px solid #d3d9e2;padding:9px 14px;font-size:13px}
#status{color:#667085;font-size:13px}
#modal{display:none;position:fixed;inset:0;background:rgba(16,24,40,.5);padding:40px 20px;overflow:auto}
#modal .box{max-width:860px;margin:0 auto;background:#fff;border-radius:12px;padding:18px 20px}
@media print{.bar,.seg,#modal{display:none}details{page-break-inside:avoid}body{background:#fff}}
"""

JS = """
var DATA = JSON.parse(document.getElementById('data').textContent);
var KEY = DATA.kind + ':' + DATA.project + ':' + DATA.date;

function state(){ try { return JSON.parse(localStorage.getItem(KEY)) || {v:{},a:{}}; }
  catch (e) { return {v:{},a:{}}; } }
function save(s){ try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) {} }

var saved = state();
DATA.devquestions.forEach(function(q){
  var box = document.getElementById('a-' + q.id);
  if (box) {
    if (saved.a && saved.a[q.id]) box.value = saved.a[q.id];
    grow(box);
    box.addEventListener('input', function(){
      var s = state(); s.a = s.a || {}; s.a[q.id] = box.value; save(s); grow(box);
    });
  }
  document.querySelectorAll('input[name="v-' + q.id + '"]').forEach(function(radio){
    if (saved.v && saved.v[q.id] === radio.value) radio.checked = true;
    radio.addEventListener('change', function(){
      var s = state(); s.v = s.v || {}; s.v[q.id] = radio.value; save(s);
    });
  });
});

function grow(box){ box.style.height = 'auto'; box.style.height = (box.scrollHeight + 2) + 'px'; }

function verdict(id){
  var picked = document.querySelector('input[name="v-' + id + '"]:checked');
  return picked ? picked.value : 'unsure';
}

function markdown(){
  var out = ['# ' + DATA.mdtitle, ''];
  var answered = 0;
  DATA.groups.forEach(function(group){
    var rows = DATA.devquestions.filter(function(q){ return q.group === group; });
    if (!rows.length) return;
    out.push('## ' + group, '');
    rows.forEach(function(q){
      out.push('### ' + q.id + ' (' + q.group + '): ' + q.question);
      if (q.proposal) {
        out.push('Proposal: ' + q.proposal);
        out.push('Verdict: ' + verdict(q.id));
      }
      var box = document.getElementById('a-' + q.id);
      var text = box && box.value.trim();
      if (text) answered++;
      out.push('Answer: ' + (text || '(no answer)'));
      out.push('');
    });
  });
  return {text: out.join('\\n'), answered: answered};
}

function say(message){ document.getElementById('status').textContent = message; }

function fallbackCopy(text, done){
  var area = document.getElementById('sink');
  area.value = text; area.select();
  var ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  area.blur();
  if (ok) { done(); return; }
  document.getElementById('modal').style.display = 'block';
  var pre = document.getElementById('modaltext');
  pre.textContent = text;
  var range = document.createRange(); range.selectNodeContents(pre);
  var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
  say('Copy is blocked here: the text is selected, press cmd+c or ctrl+c');
}

document.getElementById('copy').onclick = function(){
  var built = markdown();
  var done = function(){ say('Copied ' + built.answered + ' answers'); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(built.text).then(done, function(){
      fallbackCopy(built.text, done); });
  } else { fallbackCopy(built.text, done); }
};

document.getElementById('download').onclick = function(){
  var built = markdown();
  var blob = new Blob([built.text], {type: 'text/markdown'});
  var link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = DATA.kind + '-' + DATA.project + '-' + DATA.date + '.md';
  document.body.appendChild(link); link.click(); document.body.removeChild(link);
  URL.revokeObjectURL(link.href);
  say('Downloaded ' + built.answered + ' answers');
};

document.getElementById('modalclose').onclick = function(){
  document.getElementById('modal').style.display = 'none';
};
"""


def e(text: object) -> str:
    return escape("" if text is None else str(text), quote=True)


def repo_line(scan: dict) -> str:
    parts = []
    for repo in scan.get("repos") or []:
        frames = []
        for app in repo.get("apps") or []:
            for framework in app.get("frameworks") or []:
                name = framework.get("name")
                if name and name not in frames:
                    frames.append(name)
        head = f"{repo.get('name')} ({repo.get('files', 0)} files"
        head += f", {', '.join(frames)})" if frames else ")"
        parts.append(head)
    return " · ".join(parts) or "no repo scanned"


def tiles_html(data: dict, flavour: dict) -> str:
    tally = data.get("tally") or {}
    numbers = {"scanned": len(data.get("questions") or []), **tally}
    cells = []
    for key, label, tip in flavour["tiles"]:
        cells.append(f'<div class="tile" title="{e(tip)}"><b>{numbers.get(key, 0)}</b>'
                     f'<span>{e(label)}</span></div>')
    return '<div class="tiles">' + "".join(cells) + "</div>"


def card_html(question: dict) -> str:
    qid = e(question.get("id"))
    out = [f'<div class="card"><h3><span class="idb">{qid}</span>'
           f'{e(question.get("question"))}</h3>']
    if question.get("proposal"):
        out.append(f'<div class="guess"><b>Our guess</b>{e(question["proposal"])}'
                   '<div class="seg">')
        for value, label in (("confirm", "confirm"), ("deny", "deny"), ("unsure", "not sure")):
            out.append(f'<label><input type="radio" name="v-{qid}" value="{value}">'
                       f'<span>{e(label)}</span></label>')
        out.append("</div></div>")
    chips = question.get("evidence") or []
    if chips:
        out.append('<div class="chips">'
                   + "".join(f'<span class="chip">{e(chip)}</span>' for chip in chips)
                   + "</div>")
    out.append(f'<textarea id="a-{qid}" rows="3" '
               'placeholder="A sentence or a path is enough"></textarea></div>')
    return "".join(out)


def benchmark_html(data: dict, flavour: dict) -> str:
    questions = {row["id"]: row.get("question", "") for row in data.get("questions") or []}
    rows = data.get("benchmark") or []
    by_cluster: dict[str, list[dict]] = {}
    for row in rows:
        by_cluster.setdefault(row.get("cluster") or "(none)", []).append(row)
    headers = "".join(f"<th>{e(name)}</th>" for name in flavour["benchmark_headers"])
    body = [f"<table><tr>{headers}</tr>"]
    for cluster in sorted(by_cluster):
        for row in by_cluster[cluster]:
            status = row.get("status", "")
            label, css = flavour["statuses"].get(status, (status, "s-na"))
            where = ("<br>".join(e(item) for item in (row.get("where") or []))
                     or '<span class="muted">no locator</span>')
            body.append(
                f"<tr><td>{e(cluster)}</td>"
                f'<td><span class="pill {css}">{e(label)}</span></td>'
                f'<td class="loc">{where}</td>'
                f'<td>{e(row.get("note"))}'
                f'<div class="muted">{e(row.get("id"))}: '
                f'{e(questions.get(row.get("id"), ""))}</div></td></tr>')
    body.append("</table>")
    return (f"<details><summary>{e(flavour['benchmark_title'])}</summary>"
            + "".join(body) + "</details>")


def proposal_html(data: dict, flavour: dict) -> str:
    files = data.get("proposal") or {}
    if not files:
        return ""
    inner = []
    for name in sorted(files, key=lambda n: (n != "INDEX.md", n)):
        inner.append(f"<details><summary>{e(name)}</summary><pre>{e(files[name])}</pre></details>")
    return (f"<details><summary>{e(flavour['proposal_title'])}</summary>"
            + "".join(inner) + "</details>")


DEFAULT_FLAVOUR = {
    "kind": "source-intake",
    "title": "Source intake",
    "intro": INTRO,
    "groups": GROUPS,
    "tiles": TILES,
    "statuses": STATUSES,
    "benchmark_title": "Where each question would be grounded (benchmark)",
    "benchmark_headers": ("cluster", "status", "where", "note"),
    "proposal_title": "Proposed codebase map (skills/codebase/)",
    "stamp": lambda data: repo_line(data.get("scan") or {}),
    "heading": lambda data, date: f"Source intake \u00b7 {data.get('project')} \u00b7 {date}",
    "md_subject": lambda data: str(data.get("project") or ""),
    "extra_sections": (),
}


def build(data: dict, flavour: dict | None = None) -> str:
    """The whole page. `flavour` carries everything the two intakes word differently."""
    flavour = {**DEFAULT_FLAVOUR, **(flavour or {})}
    date = str(data.get("collected_at") or "")[:10]
    groups = list(flavour["groups"])
    payload = {
        "kind": flavour["kind"],
        "project": data.get("project"),
        "date": date,
        "mdtitle": f"{flavour['title']} answers: {flavour['md_subject'](data)} ({date})",
        "groups": [key for key, _ in groups],
        "devquestions": [{"id": q.get("id"), "group": q.get("group"),
                          "question": q.get("question"), "proposal": q.get("proposal") or ""}
                         for q in data.get("devquestions") or []],
    }
    embedded = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    sections = []
    for key, label in groups:
        rows = [q for q in data.get("devquestions") or [] if q.get("group") == key]
        if not rows:
            continue
        sections.append(f"<section><h2>{e(label)} ({len(rows)})</h2>"
                        + "".join(card_html(q) for q in rows) + "</section>")

    headline = "".join(f"<p>{e(line)}</p>" for line in data.get("headline") or [])
    evidence = [benchmark_html(data, flavour), proposal_html(data, flavour),
                *flavour["extra_sections"]]

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(flavour['title'])} {e(data.get('project'))}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<h1>{e(flavour['heading'](data, date))}</h1>
<p class="stamp">{e(flavour['stamp'](data))}</p>
<div class="lede">{headline}{flavour['intro']}</div>
{tiles_html(data, flavour)}
{''.join(sections)}
<section><h2>Evidence</h2>
{chr(10).join(evidence)}
</section>
</div>
<div class="bar">
<button class="primary" id="copy">Copy all as Markdown</button>
<button class="ghost" id="download">Download .md</button>
<span id="status"></span>
</div>
<div id="modal"><div class="box"><button class="ghost" id="modalclose">close</button>
<pre id="modaltext"></pre></div></div>
<textarea id="sink" style="position:fixed;left:-9999px;top:0" aria-hidden="true"></textarea>
<script id="data" type="application/json">{embedded}</script>
<script>{JS}</script>
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render intake.json into report.html")
    parser.add_argument("intake", type=Path, help="the assembled intake.json")
    args = parser.parse_args(argv)
    if not args.intake.is_file():
        print(f"render: no such file: {args.intake}", file=sys.stderr)
        return 2
    data = json.loads(args.intake.read_text(encoding="utf-8"))
    target = args.intake.parent / "report.html"
    target.write_text(build(data), encoding="utf-8")
    print(f"{len(data.get('devquestions') or [])} dev questions -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
