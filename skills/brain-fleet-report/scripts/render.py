#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2"]
# ///
"""Four fleet report outputs, projected once per audience for HTML and text."""

from __future__ import annotations

import argparse
import html
from pathlib import Path

from report_schema import Report, compose_prompt, load_report, owner_prompt_allowed

STR = {
    "en": {
        "act": "Act today",
        "owner_act": "Decisions and tasks",
        "open": "Still open",
        "diagnostics": "Diagnostics",
        "since": "since",
        "update": "Since last report",
        "prompt": "Prompt — copy",
        "owner_prompt": "Prompt for the assistant in the brain repo — copy",
        "copy": "Copy prompt",
        "conversation": "conversation",
        "for": "For",
        "unchanged": "unchanged",
        "prompt_from": "prompt from",
        "evidence": "Evidence",
        "review": "Complete feedback review (about 10 minutes)",
        "quiet": "No new tasks.",
        "high": "High",
        "medium": "Medium",
        "low": "Low",
        "good": "Good",
    },
    "nl": {
        "act": "Vandaag",
        "owner_act": "Te beslissen / te doen",
        "open": "Nog open",
        "diagnostics": "Diagnostiek",
        "since": "sinds",
        "update": "Sinds vorig rapport",
        "prompt": "Prompt — kopieer",
        "owner_prompt": "Prompt voor de assistent in de brain-repo — kopieer",
        "copy": "Kopieer prompt",
        "conversation": "gesprek",
        "for": "Voor",
        "unchanged": "ongewijzigd",
        "prompt_from": "prompt van",
        "evidence": "Bewijs",
        "review": "Feedbackreview invullen (±10 minuten)",
        "quiet": "Geen nieuwe acties.",
        "high": "Hoog",
        "medium": "Midden",
        "low": "Laag",
        "good": "Goed",
    },
}


def owner_lang(report):
    return "en" if report.coverage.lang() == "en" else "nl"


def e(value):
    return html.escape(str(value))


def _table(title, columns, rows):
    return {"title": title, "columns": columns, "rows": rows}


def _kpi_table(report):
    columns = [
        "Scope",
        "Runs",
        "Drafts",
        "Sent as proposed",
        "Edited",
        "Shadow compared",
        "Actions failed",
        "Run errors",
    ]
    rows = [
        [
            r.name,
            r.runs,
            r.drafts,
            r.sent_as_proposed,
            r.edited,
            r.shadow_compared,
            r.actions_failed,
            r.run_errors,
        ]
        for r in report.axis_rows()
    ]
    return _table("Per-axis counts", columns, rows)


def _diagnostics(report):
    tables = [_kpi_table(report)]
    funnel = report.kpis.action_funnel
    if funnel:
        columns = [
            "Action",
            "Proposed",
            "Executed",
            "Failed",
            "Superseded",
            "Awaiting",
            "Stale",
        ]

        def row(label, r):
            return [
                label,
                r.proposed_total,
                r.succeeded,
                r.failed,
                r.superseded,
                r.pending,
                r.stale,
            ]

        rows = [row(r.action_id, r) for r in funnel.focus.rows if r.proposed_total]
        if rows:
            rows.append(row("Whole project", funnel.focus.total))
            tables.append(_table("Action funnel", columns, rows))
        axes = [a for a in funnel.per_axis if a.total.proposed_total]
        if len(axes) >= 2:
            tables.append(
                _table(
                    "Actions by scope",
                    ["Scope"] + columns[1:],
                    [
                        row(f"{a.axis}: {report.axis_name(a.key)}", a.total)
                        for a in axes
                    ],
                )
            )
    coverage = [
        f"{c.feed}: {c.status}" + (f" — {c.reason}" if c.reason else "")
        for c in report.coverage.coverage
    ]
    notes = [
        n
        for n in (
            report.technical.noise_note,
            report.meta.signal_note if report.meta else None,
        )
        if n
    ]
    return {
        "tables": [t for t in tables if t["rows"]],
        "coverage": coverage,
        "notes": notes,
    }


def project_sections(report: Report, half: str) -> dict:
    """All selection, ordering, localization and prompt gating lives here."""
    technical = half == "technical"
    lang = "en" if technical else owner_lang(report)
    words = STR[lang]
    k = report.kpis.focus
    counters = (
        (
            f"{k.runs} runs · {k.drafts} drafts · {k.actions_ok}/{k.actions_failed} actions ok/failed"
            + (f" · {k.run_errors} errors" if k.run_errors is not None else "")
        )
        if technical
        else (
            f"{k.runs} mails · {k.drafts} voorstellen"
            if lang == "nl"
            else f"{k.runs} mails · {k.drafts} drafts"
        )
    )
    totals = (
        report.kpis.action_funnel.focus.total if report.kpis.action_funnel else None
    )
    actions = ""
    if not technical and totals and totals.proposed_total:
        actions = (
            (
                f"{totals.proposed_total} acties voorgesteld · {totals.succeeded} uitgevoerd · "
                f"{totals.failed} mislukt · {totals.pending} wachten op bevestiging"
            )
            if lang == "nl"
            else (
                f"{totals.proposed_total} actions proposed · {totals.succeeded} executed · "
                f"{totals.failed} failed · {totals.pending} awaiting confirmation"
            )
        )
    coverage = [
        f"{c.feed}: {c.status}"
        for c in report.coverage.coverage
        if c.status != "complete"
    ]
    active, carried = [], []
    for original in report.findings:
        if not technical and original.audience == "technical":
            continue
        if technical and original.audience == "owner":
            carried.append(
                {"id": original.id, "line": f"Waiting on owner: {original.title}"}
            )
            continue
        f = report.effective(original)
        scope = (
            report.axis_name(f.scope.axis_value())
            if f.scope.axis_value()
            else "project"
        )
        prompt = (
            compose_prompt(f, owner=not technical)
            if technical or owner_prompt_allowed(f)
            else ""
        )
        prompt_label = words["prompt" if technical else "owner_prompt"]
        if f.status == "unchanged":
            date = report._prior[f.signature]["prompt_date"]
            prompt_label += f" — {words['prompt_from']} {date}, {words['unchanged']}"
        links = [
            {
                "url": url,
                "label": (
                    url.split("/runs/")[-1].split("?")[0][:8]
                    if technical
                    else words["conversation"]
                    + (f" {i + 1}" if len(f.evidence.run_urls) > 1 else "")
                )
                + " ↗",
            }
            for i, url in enumerate(f.evidence.run_urls)
        ]
        ask = (
            ((f"{words['for']} {f.ask_for}: " if f.ask_for else "") + (f.ask_nl or ""))
            if not technical
            else ""
        )
        item = {
            "id": f.id,
            "severity": f.severity,
            "severity_label": words[f.severity],
            "title": f.title if technical else "",
            "text": f.text_en if technical else f.text_nl,
            "ask": ask,
            "links": links,
            "prompt": prompt,
            "prompt_label": prompt_label,
            "signature": f.signature if technical else "",
            "chips": [scope],
            "prompt_chips": [f.prompt.task_kind, *[t.repo for t in f.prompt.targets]]
            if f.prompt
            else [],
            "update": f.update_en if technical else f.update_nl,
        }
        if technical:
            item["chips"] += [f.root_cause.plane, f"{f.impact.runs} runs"]
        if f.status == "changed":
            item["chips"].append("changed" if lang == "en" else "gewijzigd")
        if f.status == "unchanged":
            start = f.title if technical else ask.split(". ", 1)[0]
            since = f"{words['since']} {f.recurrence.first_seen}"
            if technical:
                since += f" · {f.recurrence.focus_count} today"
            item["line"] = f"{start} — {since} · {item['update']}"
            carried.append(item)
        else:
            active.append(item)
    sections = []
    if active:
        sections.append(
            {
                "id": "act",
                "title": words["act" if technical else "owner_act"],
                "items": active,
            }
        )
    if carried:
        sections.append({"id": "open", "title": words["open"], "items": carried})
    if technical:
        sections.append(
            {
                "id": "diagnostics",
                "title": words["diagnostics"],
                "data": _diagnostics(report),
            }
        )
    review = report.coverage.feedback_review
    feedback = (
        not technical
        and review.get("enabled")
        and (
            review.get("cadence", "weekly") == "daily-lite"
            or len(report.window.focus_days) > 1
        )
    )
    return {
        "lang": lang,
        "technical": technical,
        "title": f"{' + '.join(report.coverage.projects)} — {report.date}",
        "headline": report.technical.headline
        if technical
        else report.owner.headline_nl,
        "counters": counters,
        "actions": actions,
        "warning": " · ".join(coverage) if technical else "",
        "sections": sections,
        "feedback": feedback,
        "quiet": not active,
    }


CSS = """
:root{color-scheme:light;font-family:system-ui,sans-serif;color:#172b36;background:#f5f7f9}
body{max-width:1000px;margin:40px auto;padding:0 24px 40px}h1{font-size:26px;margin:8px 0 16px}
h2{font-size:18px;margin:30px 0 14px}p{line-height:1.55}.lede{font-size:18px;max-width:800px}
.counts,.muted,footer{font-size:13px;color:#536673}.warning{background:#fff0c2;padding:12px;border-radius:6px}
.card{background:white;border:1px solid #dbe2e7;border-radius:10px;padding:20px;margin:12px 0}
h3{font-size:18px;margin:10px 0}.chip,.severity{display:inline-block;font-size:12px;padding:3px 7px;margin:0 6px 5px 0;border-radius:4px;background:#edf2f5}
.high{color:#a52020;background:#ffe9e9}.medium{color:#815400;background:#fff1cd}.low{color:#475467}.good{color:#12734a}
.ask{font-weight:650}.update{font-style:italic;color:#536673}details{margin-top:12px}summary{cursor:pointer;font-size:14px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f7;padding:16px;border-radius:6px;font-size:13px;line-height:1.5}
button{cursor:pointer;padding:7px 12px;border:1px solid #bbcbd5;border-radius:5px;background:white;margin-top:10px}
a{color:#145d8e;margin-right:12px}table{border-collapse:collapse;width:100%;font-size:12px;margin:12px 0}th,td{padding:8px;border-bottom:1px solid #dde5ea;text-align:left}
.scroll{overflow-x:auto}.open-item{padding:12px 0;border-bottom:1px solid #dde5ea}.open-item p{margin:0}footer{margin-top:32px}
"""
JS = """document.querySelectorAll('button.copy').forEach(b=>b.addEventListener('click',async()=>{
const p=document.getElementById(b.dataset.target);try{await navigator.clipboard.writeText(p.textContent);
b.textContent=document.documentElement.lang==='nl'?'Gekopieerd':'Copied';}catch{const r=document.createRange();r.selectNodeContents(p);const s=window.getSelection();s.removeAllRanges();s.addRange(r);b.textContent=document.documentElement.lang==='nl'?'Selectie klaar: kopieer handmatig':'Selected: copy manually';}}));"""


def _prompt_html(item, lang):
    if not item.get("prompt"):
        return ""
    pid = "prompt-" + item["id"]
    chips = "".join(f'<span class="chip">{e(c)}</span>' for c in item["prompt_chips"])
    return (
        f"<details><summary>{e(item['prompt_label'])}</summary><p>{chips}</p>"
        f'<button class="copy" data-target="{e(pid)}">{e(STR[lang]["copy"])}</button>'
        f'<pre id="{e(pid)}">{e(item["prompt"])}</pre></details>'
    )


def render_html(view):
    lang = view["lang"]
    words = STR[lang]
    body = [
        f'<header><h1>{e(view["title"])}</h1><p class="lede">{e(view["headline"])}</p>',
        f'<p class="counts">{e(view["counters"])}</p>',
    ]
    for key, cls in (("actions", "counts"), ("warning", "warning")):
        if view[key]:
            body.append(f'<p class="{cls}">{e(view[key])}</p>')
    if view["feedback"]:
        body.append(f'<a href="feedback-review.html">{e(words["review"])}</a>')
    if view["quiet"]:
        body.append(f'<p class="quiet">{e(words["quiet"])}</p>')
    body.append("</header>")
    for section in view["sections"]:
        body.append(f'<section id="{section["id"]}">')
        if section["id"] == "diagnostics":
            body.append(f"<details><summary>{e(section['title'])}</summary>")
            for table in section["data"]["tables"]:
                body.append(
                    f'<h3>{e(table["title"])}</h3><div class="scroll"><table><thead><tr>'
                )
                body.extend(f"<th>{e(c)}</th>" for c in table["columns"])
                body.append("</tr></thead><tbody>")
                for row in table["rows"]:
                    body.append(
                        "<tr>" + "".join(f"<td>{e(v)}</td>" for v in row) + "</tr>"
                    )
                body.append("</tbody></table></div>")
            body.append("<h3>Data coverage</h3>")
            body.extend(
                f'<p class="muted">{e(line)}</p>'
                for line in section["data"]["coverage"] + section["data"]["notes"]
            )
            body.append("</details>")
        else:
            body.append(f"<h2>{e(section['title'])}</h2>")
            for item in section["items"]:
                cls = "open-item" if "line" in item else "card"
                body.append(f'<article class="{cls}" id="{e(item["id"])}">')
                if "severity" in item:
                    body.append(
                        f'<span class="severity {item["severity"]}">{e(item["severity_label"])}</span>'
                    )
                if "line" in item:
                    body.append(f"<p>{e(item['line'])}</p>")
                else:
                    body.extend(
                        f'<span class="chip">{e(c)}</span>' for c in item["chips"]
                    )
                    if item["title"]:
                        body.append(f"<h3>{e(item['title'])}</h3>")
                    body.append(f"<p>{e(item['text'])}</p>")
                    if item["update"]:
                        body.append(
                            f'<p class="update">{e(words["update"])}: {e(item["update"])}</p>'
                        )
                    if item["ask"]:
                        body.append(f'<p class="ask">{e(item["ask"])}</p>')
                    links = "".join(
                        f'<a href="{e(link["url"])}" target="_blank" rel="noopener">{e(link["label"])}</a>'
                        for link in item["links"]
                    )
                    if item["signature"]:
                        body.append(
                            f"<details><summary>{e(words['evidence'])}</summary><p>{links}</p><code>{e(item['signature'])}</code></details>"
                        )
                    else:
                        body.append(f"<p>{links}</p>")
                body.append(_prompt_html(item, lang))
                body.append("</article>")
        body.append("</section>")
    body.append("<footer>brain-fleet-report · v2</footer>")
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(view["title"])}</title><style>{CSS}</style></head><body>'
        + "".join(body)
        + f"<script>{JS}</script></body></html>"
    )


def render_text(view):
    words = STR[view["lang"]]
    lines = [view["title"], "", view["headline"], view["counters"]]
    lines += [view[k] for k in ("actions", "warning") if view[k]]
    if view["feedback"]:
        lines.append(words["review"] + ": feedback-review.html")
    if view["quiet"]:
        lines.append(words["quiet"])
    for section in view["sections"]:
        lines += ["", section["title"].upper()]
        if section["id"] == "diagnostics":
            for table in section["data"]["tables"]:
                lines += [table["title"], " | ".join(table["columns"])]
                lines += [" | ".join(map(str, row)) for row in table["rows"]]
            lines += (
                ["Data coverage"]
                + section["data"]["coverage"]
                + section["data"]["notes"]
            )
            continue
        for item in section["items"]:
            if "line" in item:
                lines.append(item["line"])
            else:
                lines += [
                    f"[{item['severity_label']}] "
                    + " · ".join(
                        ([item["title"]] if item["title"] else []) + item["chips"]
                    ),
                    item["text"],
                ]
                if item["update"]:
                    lines.append(f"{words['update']}: {item['update']}")
                if item["ask"]:
                    lines.append(item["ask"])
                lines += [f"{link['label']}: {link['url']}" for link in item["links"]]
                if item["signature"]:
                    lines.append("signature: " + item["signature"])
            if item.get("prompt"):
                lines += [item["prompt_label"]] + [
                    "    " + line for line in item["prompt"].splitlines()
                ]
            lines.append("")
    return "\n".join(lines) + "\n"


def render_all(report: Report, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for half in ("technical", "owner"):
        view = project_sections(report, half)
        for extension, content in (
            ("html", render_html(view)),
            ("txt", render_text(view)),
        ):
            path = out_dir / f"{half}.{extension}"
            path.write_text(content, encoding="utf-8")
            written.append(path)
    return written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument(
        "--prior-dir",
        type=Path,
        action="append",
        help="explicit prior report directory (fixtures)",
    )
    args = parser.parse_args()
    for path in render_all(
        load_report(args.report, prior_dirs=args.prior_dir),
        args.out_dir or args.report.parent,
    ):
        print(path)
