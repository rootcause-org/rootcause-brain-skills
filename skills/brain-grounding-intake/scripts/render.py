#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""`uv run render.py OUT_DIR/intake.json` -> `report.html` next to it.

One page for the customer's developer: what the code and the data could not tell the support
brain, our guess where we have one, and one box per question. Self contained (inline CSS and
JS, no external URL) so it works from `file://` and travels as an attachment. Chrome is
English; the customer questions stay in their own language. Everything is escaped: the agent
never supplies HTML. `build(data)` is the whole page.
"""

from __future__ import annotations

import argparse
import json
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collect import table_facts  # noqa: E402

GROUPS = [
    ("architecture", "Architecture"),
    ("where", "Where does it live"),
    ("data", "What the data means"),
    ("settings", "Settings and flags"),
    ("queues", "Queues, jobs and logs"),
    ("conventions", "Conventions"),
    ("ownership", "Ownership: legacy or new"),
]
TILES = [
    ("scanned", "scanned",
     "Real customer questions from the window we read, one row each in questions.tsv."),
    ("grounded", "grounded",
     "One named place answers it: a run reads that and replies. Nothing to ask."),
    ("ambiguous", "ambiguous",
     "Two or more candidate places, or a place whose meaning is unclear. This is what most "
     "of the questions below are about."),
    ("missing", "missing",
     "Nothing locatable within budget. Either it lives somewhere we did not see, or it is "
     "not in the code or the data at all."),
    ("knowledge", "knowledge",
     "Static knowledge or a brain runbook answers it, no lookup needed."),
    ("human", "human",
     "A write, money or a judgement call. Support prepares it, a person decides."),
]
STATUSES = {
    "grounded": ("grounded", "s-grounded"),
    "ambiguous": ("ambiguous", "s-ambiguous"),
    "missing": ("missing", "s-missing"),
    "knowledge": ("knowledge", "s-knowledge"),
    "human": ("human", "s-human"),
}
VERDICTS = (("confirm", "confirm"), ("deny", "deny"), ("unsure", "not sure"))
INTRO = (
    "<p>These are the things your code and your data could not tell the support brain we are "
    "building for your customers. Everything else we already grounded ourselves.</p>"
    "<p>Roughly fifteen minutes of your time: a sentence, a path or a table name per question, "
    "and one button at the bottom copies every answer in one go.</p>")
MAX_GLANCE_ROWS = 400

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f6f7f9;color:#101828;font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:28px 18px 120px}
h1{margin:0;font-size:25px;letter-spacing:-.01em}
h2{margin:0 0 12px;font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#667085}
h3{margin:0 0 6px;font-size:16px;line-height:1.4}
p{margin:0}section{margin-top:30px;scroll-margin-top:76px}
.stamp,.muted{color:#667085;font-size:13px}
.stamp{margin-top:6px}.stamp b{color:#344054;font-weight:600}
.lede{margin-top:16px;background:#fff;border:1px solid #e4e7ec;border-left:4px solid #33518c;
 border-radius:12px;padding:14px 18px}
.lede p+p{margin-top:8px}.lede .head{color:#7a4a12;background:#fdf6ea;border-radius:8px;
 padding:8px 10px;margin-bottom:10px;font-size:14px}
.tiles{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.tile{background:#fff;border:1px solid #e4e7ec;border-radius:10px;padding:10px 14px;min-width:104px;flex:1 1 104px}
.tile b{display:block;font-size:22px;line-height:1.2}
.tile>span{font-size:10px;text-transform:uppercase;letter-spacing:.09em;color:#667085;font-weight:700}
.tile.done{border-color:#33518c;background:#f7f9fd}.tile.done b{color:#33518c}
.nav{position:sticky;top:0;z-index:5;margin:18px -18px 0;padding:10px 18px;background:#f6f7f9ee;
 backdrop-filter:blur(6px);border-bottom:1px solid #e4e7ec;display:flex;flex-wrap:wrap;gap:6px}
.nav a{text-decoration:none;font-size:12px;font-weight:600;color:#475467;background:#fff;
 border:1px solid #e4e7ec;border-radius:999px;padding:4px 11px}
.nav a:hover{border-color:#33518c;color:#33518c}
.nav a i{font-style:normal;color:#98a2b3;margin-left:5px}
.card{background:#fff;border:1px solid #e4e7ec;border-left:3px solid #fff;border-radius:12px;
 padding:15px 18px;margin-bottom:10px}
.card.answered{border-left-color:#33518c}
.idb{display:inline-block;background:#eef2fa;color:#33518c;border-radius:999px;padding:2px 9px;
 font-size:11px;font-weight:700;letter-spacing:.06em;margin-right:8px;vertical-align:2px}
.why{font-size:13px;color:#667085;margin:0 0 10px}
.why b{color:#475467;font-weight:600}
.guess{background:#fbfcfd;border:1px solid #d3d9e2;border-radius:10px;padding:10px 12px;margin:0 0 10px;font-size:15px}
.guess b{display:block;font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:#667085;margin-bottom:3px}
.seg{display:inline-flex;border:1px solid #d3d9e2;border-radius:8px;overflow:hidden;margin-top:9px;background:#fff}
.seg label{font-size:12px;font-weight:700;color:#667085;padding:5px 14px;cursor:pointer;border-right:1px solid #e4e7ec}
.seg label:last-child{border-right:0}
.seg input{position:absolute;opacity:0;pointer-events:none}
.seg label:has(input:checked){background:#33518c;color:#fff}
.chips{margin:0 0 2px}
.chip{display:inline-block;background:#f2f4f7;color:#475467;border:1px solid #f2f4f7;border-radius:6px;
 padding:2px 8px;margin:0 6px 6px 0;font-size:12px;line-height:1.5;word-break:break-all}
.chip.loc{font-family:ui-monospace,Menlo,monospace}
button.chip{cursor:pointer;font-weight:600;background:#eef2fa;border-color:#dbe3f3;color:#33518c}
button.chip:hover{border-color:#33518c}
button.chip.open{background:#33518c;color:#fff;border-color:#33518c}
.panel{display:none;border:1px solid #e4e7ec;border-radius:10px;padding:8px 12px;margin:2px 0 10px;background:#fbfcfd}
.panel.open{display:block}
.panel .q{font-size:13px;padding:6px 0;border-top:1px solid #eef0f3}
.panel .q:first-child{border-top:0}
.panel .q em{font-style:normal;color:#98a2b3;font-size:11px;margin-right:6px}
.panel a{color:#33518c}
textarea{width:100%;font:15px/1.5 inherit;padding:9px 11px;border:1px solid #d3d9e2;border-radius:8px;
 resize:vertical;background:#fff;color:#101828;min-height:44px;overflow:hidden}
textarea:focus{outline:2px solid #33518c;outline-offset:1px}
details{background:#fff;border:1px solid #e4e7ec;border-radius:12px;padding:12px 16px;margin-top:10px}
details details{margin-top:8px;border-radius:8px;background:#fbfcfd}
summary{cursor:pointer;font-weight:700;font-size:14px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}
th,td{text-align:left;padding:6px 8px;border-top:1px solid #e4e7ec;vertical-align:top}
th{border-top:none;font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:#667085}
td.loc{font:12px/1.5 ui-monospace,Menlo,monospace;word-break:break-all}
tr.cl td{background:#f8f9fb;font-weight:700;font-size:12px;letter-spacing:.04em;color:#475467}
.filter{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.filter button{border:1px solid #e4e7ec;background:#fff;padding:3px 10px;font-size:11px;font-weight:700;color:#667085}
.filter button.on{border-color:#33518c;color:#33518c;background:#eef2fa}
.pill{display:inline-block;border-radius:999px;padding:2px 9px;font-size:11px;font-weight:700;white-space:nowrap}
.s-grounded{background:#e8f5ee;color:#2f6f4f}.s-ambiguous{background:#fbf1e3;color:#7a4a12}
.s-missing{background:#fdeceb;color:#b42318}.s-knowledge{background:#eef2fa;color:#3f5a91}
.s-human{background:#f2f4f7;color:#667085}
pre{margin:8px 0 0;white-space:pre-wrap;word-break:break-word;background:#f6f7f9;border-radius:8px;
 padding:11px 13px;font:13px/1.5 ui-monospace,Menlo,monospace;color:#344054}
.bar{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid #e4e7ec;
 padding:10px 16px;display:flex;gap:10px;align-items:center;justify-content:center;flex-wrap:wrap}
button{font:inherit;cursor:pointer;border-radius:8px}
button.primary{background:#33518c;color:#fff;border:0;padding:10px 20px;font-weight:700;font-size:15px}
button.ghost{background:#fff;color:#33518c;border:1px solid #d3d9e2;padding:8px 13px;font-size:13px}
#status,#note{color:#667085;font-size:13px}
#modal{display:none;position:fixed;inset:0;z-index:9;background:rgba(16,24,40,.5);padding:36px 18px;overflow:auto}
#modal .box{max-width:860px;margin:0 auto;background:#fff;border-radius:12px;padding:16px 18px}
@media (max-width:520px){.wrap{padding:20px 12px 132px}h1{font-size:20px}
 .nav{margin-left:-12px;margin-right:-12px;padding-left:12px;padding-right:12px}
 .tile{min-width:84px;flex:1 1 84px;padding:8px 10px}.tile b{font-size:19px}.card{padding:13px 13px}}
@media print{.bar,.seg,.nav,#modal,.filter{display:none}details{page-break-inside:avoid}
 body{background:#fff}.card{page-break-inside:avoid}}
"""

JS = """
var DATA = JSON.parse(document.getElementById('data').textContent);
var KEY = 'grounding-intake:' + DATA.project + ':' + DATA.date;
var TOTAL = DATA.devquestions.length;

function state(){ try { return JSON.parse(localStorage.getItem(KEY)) || {v:{},a:{}}; }
  catch (e) { return {v:{},a:{}}; } }
function save(s){ try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) {} }
function grow(box){ box.style.height = '0px'; box.style.height = (box.scrollHeight + 2) + 'px'; }
function verdict(id){
  var picked = document.querySelector('input[name="v-' + id + '"]:checked');
  return picked ? picked.value : '';
}
function answerOf(id){
  var box = document.getElementById('a-' + id);
  return box ? box.value.trim() : '';
}

function refresh(){
  var done = 0;
  DATA.devquestions.forEach(function(q){
    var filled = answerOf(q.id) !== '' || verdict(q.id) !== '';
    if (filled) done++;
    var card = document.getElementById('c-' + q.id);
    if (card) card.classList.toggle('answered', filled);
  });
  document.getElementById('done').textContent = done;
  document.getElementById('progress').classList.toggle('done', done > 0);
  document.getElementById('note').textContent = done + ' of ' + TOTAL + ' answered';
}

var saved = state();
DATA.devquestions.forEach(function(q){
  var box = document.getElementById('a-' + q.id);
  if (box) {
    if (saved.a && saved.a[q.id]) box.value = saved.a[q.id];
    box.addEventListener('input', function(){
      var s = state(); s.a = s.a || {}; s.a[q.id] = box.value; save(s); grow(box); refresh();
    });
  }
  document.querySelectorAll('input[name="v-' + q.id + '"]').forEach(function(radio){
    if (saved.v && saved.v[q.id] === radio.value) radio.checked = true;
    radio.addEventListener('change', function(){
      var s = state(); s.v = s.v || {}; s.v[q.id] = radio.value; save(s); refresh();
    });
  });
});
document.querySelectorAll('textarea[id^="a-"]').forEach(grow);

document.querySelectorAll('button[data-panel]').forEach(function(chip){
  chip.onclick = function(){
    var panel = document.getElementById(chip.getAttribute('data-panel'));
    var open = panel.classList.toggle('open');
    chip.classList.toggle('open', open);
  };
});

var picked = {};
document.querySelectorAll('.filter button').forEach(function(pill){
  pill.onclick = function(){
    var name = pill.getAttribute('data-status');
    if (picked[name]) { delete picked[name]; } else { picked[name] = 1; }
    pill.classList.toggle('on', !!picked[name]);
    var any = Object.keys(picked).length > 0;
    document.querySelectorAll('tr[data-status]').forEach(function(row){
      row.style.display = (!any || picked[row.getAttribute('data-status')]) ? '' : 'none';
    });
    document.querySelectorAll('tr.cl').forEach(function(head){
      var next = head.nextElementSibling, shown = false;
      while (next && !next.classList.contains('cl')) {
        if (next.style.display !== 'none') shown = true;
        next = next.nextElementSibling;
      }
      head.style.display = shown ? '' : 'none';
    });
  };
});

function markdown(){
  var out = ['# ' + DATA.mdtitle, ''];
  DATA.groups.forEach(function(group){
    var rows = DATA.devquestions.filter(function(q){ return q.group === group; });
    if (!rows.length) return;
    out.push('## ' + group, '');
    rows.forEach(function(q){
      out.push('### ' + q.id + ' (' + q.group + '): ' + q.question);
      if (q.proposal) {
        out.push('Proposal: ' + q.proposal);
        out.push('Verdict: ' + (verdict(q.id) || 'unsure'));
      }
      out.push('Answer: ' + (answerOf(q.id) || '(no answer)'));
      out.push('');
    });
  });
  return out.join('\\n');
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
  var text = markdown();
  var done = function(){ say('Copied all ' + TOTAL + ' questions'); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, function(){ fallbackCopy(text, done); });
  } else { fallbackCopy(text, done); }
};

document.getElementById('download').onclick = function(){
  var blob = new Blob([markdown()], {type: 'text/markdown'});
  var link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'grounding-intake-' + DATA.project + '-' + DATA.date + '.md';
  document.body.appendChild(link); link.click(); document.body.removeChild(link);
  URL.revokeObjectURL(link.href);
  say('Downloaded');
};

document.getElementById('modalclose').onclick = function(){
  document.getElementById('modal').style.display = 'none';
};

refresh();
"""


def e(text: object) -> str:
    return escape("" if text is None else str(text), quote=True)


def stamp(data: dict) -> str:
    """What we read: the repos on one side, the database on the other. Either may be absent."""
    parts = []
    for repo in (data.get("scan") or {}).get("repos") or []:
        frames = []
        for app in repo.get("apps") or []:
            for framework in app.get("frameworks") or []:
                name = framework.get("name")
                if name and name not in frames:
                    frames.append(name)
        head = f"{repo.get('name')} ({repo.get('files', 0)} files"
        head += f", {', '.join(frames)})" if frames else ")"
        parts.append(head)
    schema = data.get("schema")
    if schema:
        tables = [t for t in schema.get("tables") or []
                  if "VIEW" not in str(t.get("type") or "").upper()]
        candidates = schema.get("tenant_candidates") or []
        tenant = f", tenant key {candidates[0]['column']}" if candidates else ""
        parts.append(f"{data.get('engine') or schema.get('engine')} "
                     f"{schema.get('version', '')}, database {schema.get('database')} "
                     f"({len(tables)} tables, {len(schema.get('columns') or [])} columns"
                     f"{tenant})")
    return "Read: " + (" · ".join(parts) or "nothing scanned")


def tiles_html(data: dict) -> str:
    tally = data.get("tally") or {}
    numbers = {"scanned": len(data.get("questions") or []), **tally}
    cells = [f'<div class="tile" title="{e(tip)}"><b>{numbers.get(key, 0)}</b>'
             f"<span>{e(label)}</span></div>" for key, label, tip in TILES]
    total = len(data.get("devquestions") or [])
    cells.append('<div class="tile" id="progress" title="Your answers, saved in this browser '
                 'as you type."><b><span id="done">0</span> of '
                 f"{total}</b><span>answered</span></div>")
    return '<div class="tiles">' + "".join(cells) + "</div>"


def nav_html(counts: dict) -> str:
    chips = [f'<a href="#g-{key}">{e(label)}<i>{counts[key]}</i></a>'
             for key, label in GROUPS if counts.get(key)]
    return '<div class="nav">' + "".join(chips) + "</div>" if chips else ""


def card_html(question: dict, cluster_questions: dict) -> str:
    qid = e(question.get("id"))
    out = [f'<div class="card" id="c-{qid}"><h3><span class="idb">{qid}</span>'
           f'{e(question.get("question"))}</h3>']
    if question.get("impact"):
        out.append(f'<p class="why"><b>Why it matters:</b> {e(question["impact"])}</p>')
    if question.get("proposal"):
        out.append(f'<div class="guess"><b>Our guess</b>{e(question["proposal"])}'
                   '<div class="seg">')
        for value, label in VERDICTS:
            out.append(f'<label><input type="radio" name="v-{qid}" value="{value}">'
                       f"<span>{e(label)}</span></label>")
        out.append("</div></div>")
    chips, panels = [], []
    for index, chip in enumerate(question.get("evidence") or []):
        rows = cluster_questions.get(chip)
        if not rows:
            chips.append(f'<span class="chip loc">{e(chip)}</span>')
            continue
        panel_id = f"p-{qid}-{index}"
        chips.append(f'<button class="chip" type="button" data-panel="{panel_id}">'
                     f"{e(chip)} ({len(rows)})</button>")
        items = []
        for row in rows:
            link = (f' <a href="{e(row.get("url"))}" target="_blank" rel="noopener">run</a>'
                    if row.get("url") else "")
            items.append(f'<div class="q"><em>{e(row.get("id"))} {e(row.get("date"))}</em>'
                         f"{e(row.get('question'))}{link}</div>")
        panels.append(f'<div class="panel" id="{panel_id}">' + "".join(items) + "</div>")
    if chips:
        out.append('<div class="chips">' + "".join(chips) + "</div>")
    out.extend(panels)
    out.append(f'<textarea id="a-{qid}" rows="2" '
               'placeholder="A sentence, a path or a table name is enough"></textarea></div>')
    return "".join(out)


def benchmark_html(data: dict, cluster_questions: dict) -> str:
    rows = data.get("benchmark") or []
    if not rows:
        return ""
    texts = {row["id"]: row.get("question", "") for row in data.get("questions") or []}
    by_cluster: dict[str, list[dict]] = {}
    for row in rows:
        by_cluster.setdefault(row.get("cluster") or "(none)", []).append(row)
    seen = [key for key in STATUSES if any(r.get("status") == key for r in rows)]
    pills = "".join(f'<button type="button" data-status="{key}">{e(STATUSES[key][0])}</button>'
                    for key in seen)
    body = ['<table><tr><th>question</th><th>status</th><th>where</th><th>note</th></tr>']
    for cluster in sorted(by_cluster):
        body.append(f'<tr class="cl"><td colspan="4">{e(cluster)}</td></tr>')
        for row in by_cluster[cluster]:
            status = row.get("status", "")
            label, css = STATUSES.get(status, (status, "s-human"))
            where = ("<br>".join(e(item) for item in (row.get("where") or []))
                     or '<span class="muted">no locator</span>')
            body.append(
                f'<tr data-status="{e(status)}"><td>{e(row.get("id"))}'
                f'<div class="muted">{e(texts.get(row.get("id"), ""))}</div></td>'
                f'<td><span class="pill {css}">{e(label)}</span></td>'
                f'<td class="loc">{where}</td><td>{e(row.get("note"))}</td></tr>')
    body.append("</table>")
    cited = sum(1 for name in by_cluster if name in cluster_questions)
    return (f"<details><summary>Where each question would be grounded "
            f"({len(rows)} questions, {cited} clusters)</summary>"
            f'<div class="filter">{pills}</div>' + "".join(body) + "</details>")


def proposal_html(data: dict) -> str:
    files = data.get("proposal") or {}
    if not files:
        return ""

    def order(name: str) -> tuple:
        head = name.split("/")[0]
        return (0 if name.endswith("codebase/INDEX.md") else 1,
                0 if head == "codebase" else 1 if head == "databases" else 2, name)

    inner = [f"<details><summary>{e(name)}</summary><pre>{e(files[name])}</pre></details>"
             for name in sorted(files, key=order)]
    return (f"<details><summary>Proposed brain files ({len(files)})</summary>"
            + "".join(inner) + "</details>")


def glance_html(data: dict) -> str:
    """Tables at a glance: the reduction schema.md shows, as a collapsed table."""
    schema = data.get("schema")
    if not schema:
        return ""
    rows = table_facts(schema)[:MAX_GLANCE_ROWS]
    if not rows:
        return ""
    body = ["<table><tr><th>table</th><th>rows</th><th>MB</th><th>role</th>"
            "<th>tenant column</th><th>flags</th></tr>"]
    for row in rows:
        rows_text = f"{row['rows']}" + (" exact" if row["exact"] else "")
        tenant = e(row["tenant"]) or '<span class="muted">none</span>'
        body.append(f'<tr><td class="loc">{e(row["table"])}</td><td>{e(rows_text)}</td>'
                    f'<td>{e(row["mb"])}</td><td>{e(row["role"])}</td><td>{tenant}</td>'
                    f'<td class="loc">{e(" · ".join(row["flags"]))}</td></tr>')
    body.append("</table>")
    return (f"<details><summary>Tables at a glance ({len(rows)})</summary>"
            + "".join(body) + "</details>")


def context_html(data: dict) -> str:
    context = str(data.get("context") or "").strip()
    if not context:
        return ""
    return ("<details><summary>What the support brain receives</summary>"
            f"<pre>{e(context)}</pre></details>")


def build(data: dict) -> str:
    """The whole page, from the assembled intake.json."""
    date = str(data.get("collected_at") or "")[:10]
    project = str(data.get("project") or "")
    devquestions = data.get("devquestions") or []
    cluster_questions: dict[str, list[dict]] = {}
    texts = {row.get("id"): row for row in data.get("questions") or []}
    for row in data.get("benchmark") or []:
        source = texts.get(row.get("id"))
        if source:
            cluster_questions.setdefault(row.get("cluster") or "", []).append(source)

    payload = {
        "project": project,
        "date": date,
        "mdtitle": f"Grounding intake answers: {project} ({date})",
        "groups": [key for key, _ in GROUPS],
        "devquestions": [{"id": q.get("id"), "group": q.get("group"),
                          "question": q.get("question"), "proposal": q.get("proposal") or ""}
                         for q in devquestions],
    }
    embedded = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    counts = {key: sum(1 for q in devquestions if q.get("group") == key) for key, _ in GROUPS}
    sections = []
    for key, label in GROUPS:
        rows = [q for q in devquestions if q.get("group") == key]
        if not rows:
            continue
        sections.append(f'<section id="g-{e(key)}"><h2>{e(label)} ({len(rows)})</h2>'
                        + "".join(card_html(q, cluster_questions) for q in rows) + "</section>")

    headline = "".join(f'<p class="head">{e(line)}</p>' for line in data.get("headline") or [])
    evidence = [benchmark_html(data, cluster_questions), proposal_html(data),
                glance_html(data), context_html(data)]
    evidence_section = ("<section><h2>Evidence</h2>" + "".join(evidence) + "</section>"
                        if any(evidence) else "")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grounding intake {e(project)}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<h1>Grounding intake · {e(project)} · {e(date)}</h1>
<p class="stamp">{e(stamp(data))}</p>
<div class="lede">{headline}{INTRO}</div>
{tiles_html(data)}
{nav_html(counts)}
{''.join(sections)}
{evidence_section}
</div>
<div class="bar">
<button class="primary" id="copy">Copy all as Markdown</button>
<button class="ghost" id="download">Download .md</button>
<span id="note"></span><span id="status"></span>
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
