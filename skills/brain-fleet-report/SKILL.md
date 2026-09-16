---
name: brain-fleet-report
description: "Build the daily two-audience fleet report from a brain checkout: collect public rc evidence, judge it into a ranked work queue, validate and render technical and owner reports. Use for fleet reports, dagrapport, or what to fix today."
---

# Daily work queue

Python collects evidence; you judge it. Technical EN is developer work. The owner half uses
`coverage.owner_lang` (default Dutch; `_nl` field names are historical) and contains only what the
owner can decide or do. Report generation is read-only: no `rc ask`, action confirmation or brain edits.
Read the project's `_internal/fleet-report/OVERLAY.md` and `ledger.md` before judging;
[overlay.md](overlay.md) describes their contract.

## Pipeline

Run from the brain root with `rc auth status` showing an all-projects token when members are combined.

```bash
FR="$PWD/.agents/skills/brain-fleet-report"
D=2026-09-16
uv run "$FR/scripts/collect.py" --date "$D"
# Last output line names OUT; --project X (repeatable), --report-id, --context-days 4,
# --refresh, --offline, --no-correlate and --prune-raw are available.
OUT="$PWD/.rootcause/fleet-report/<report_id>/$D"
cat "$OUT/digest.md"
cat "$OUT/commits.md"   # when onset/deployment evidence changes the task
uv run "$FR/scripts/drill.py" --date "$D" --run f89d3d89,a1b2c3d4
uv run "$FR/scripts/drill.py" --date "$D" --cluster 'action_failure:create_placeholder'
uv run "$FR/scripts/drill.py" --date "$D" --delta f89d3d89
uv run "$FR/scripts/drill.py" --date "$D" --feedback
# Write report.json using report_schema.md. Copy kpis.json and manifest.json verbatim.
uv run "$FR/scripts/validate.py" "$OUT/report.json" \
  --kpis "$OUT/kpis.json" --manifest "$OUT/manifest.json" --evidence "$OUT/evidence.json"
uv run "$FR/scripts/render.py" "$OUT/report.json"
uv run "$FR/scripts/prompt_compose.py" "$OUT/report.json" --finding F3
open "$OUT/technical.html" "$OUT/owner.html"
```

Four files: `technical.html`, `owner.html`, `technical.txt`, `owner.txt`. No email variants.
Read both halves before handing them over. The [schema](report_schema.md) documents exact fields.
`evidence.json` is the raw tier: search selectively, never read it whole. Drill every high finding
and every consequential live correction; stop when more detail would not change the task.

## Judging rules

1. No finding without an actionable change or decision. “By design”, “recovered”, or an
   unimportant measurement gap is at most a lede clause. A consequential unknown may become a
   bounded investigation with a question and stop condition. Zero findings is valid.
2. State each finding once. `findings[]` is the ranked queue, in author order; the lede names
   nothing that a card carries. One finding = one remedy or decision; split omnibus findings.
3. Reuse signatures from the digest's **Prior findings** table. Collector-backed signatures are
   copied exactly; other signatures use stable `<kind>:<slug>` names. Prior v1 reports are ignored.
4. `unchanged` carries only today's metadata and short update; it inherits the prior text/ask/prompt.
   Use it when the remedy has not moved. Count changes alone do not justify another full card.
   `changed` has full fields plus the specific delta. A prior owner ask can carry without a prompt.
5. A `fix` is an edit another agent can apply without reopening the run to diagnose it. Otherwise
   use `investigate`: exact checks, stop condition, output location. Every prompt path was opened
   today and exists in its target repo; list all repositories touched. `decide` names the question
   and options before implementation. Prompts must respect their production-read-only boundary.
6. Owner card = what happened + what we ask, with the person named when useful. Read the overlay's
   Owner reach table. PJ-only work never becomes an owner card. Shared findings must have a distinct
   owner task. Owner prompts appear only for content fixes targeting brain repos; dashboard/profile,
   persona and business decisions remain explicit human asks. Do not move settings into Markdown.
7. At most five new/changed technical findings and four owner findings; prioritize customer impact.
   Numbers belong in counters/chips, not repeated prose. Keep recurrence history out of prompts.
8. Honour ledger dispositions: accepted/noise never become fresh findings absent their retest
   trigger. Publish drift needs an actionable unpublished runtime change; tooling-only drift stays
   parked. An unexplained deployed ref can be a separate investigation.
9. First names only; no emails, surnames, phone numbers or credentials. Canonical token-free run
   URLs go into prompts; owner conversation links may use the collected access URL.

## Evidence semantics

- Focus is day D; context is recurrence background. Do not assert undrilled context-day facts.
  `gone` means “not seen”, never “fixed”. New consequential signatures outrank familiar noise.
- `proposed` actions have not executed. Domain refusals differ from infrastructure failures.
  Read the full error before naming the fix plane; `error_message` is often truncated.
- Funnel buckets use execution-or-proposal date; stale proposals are >36h at collection.
  Keep collector counters verbatim; the report does not display heuristic reviewer acceptance.
- Draft placement is not delivery. Shadow deltas are independent comparisons, not human corrections.
  Microsoft `sent_as_proposed` without sent-body capture cannot prove verbatim sending.
- Lost/errored runs must be checked against their thread: a later run may have recovered the reply.
- Feedback comments carry intent; scores alone do not prove a defect. An escalation-ledger comment
  is human workflow. Cosmetic edits and justified reviewer placeholders are not standalone defects.
- Exit -1 is timeout, 64 is the reread guard, grep/rg exit 1 is noise; `usage:` usually means bad flags.
  Privacy-reduced excerpts and server `⟦pii:…⟧` masking are not proof of corrupted outgoing text.
- Live DB evidence is “as of” the query time, not the run. Missing/partial feeds qualify affected
  claims. Excluded runs do not enter denominators; context detection can have less coverage.
- Commit correlation is not causation. A commit after first occurrence cannot explain onset.

## Handoff

Besides the two HTML reports, deliver the **spawn list**: at most five ranked bullets naming the
problem, concrete direction and target repo/skill. Derive it from actionable findings, never invent
another summary. Owner decisions without coding work do not belong in it. Close with the HTML paths.
Project wrappers own delivery; delivered titles begin `🌅`. Do not send reports independently.

When `[feedback_review].enabled`, follow [brain-feedback-review](../brain-feedback-review/SKILL.md)
on its configured cadence; keep its sibling attachment link. No automatic learning or delivery.

v2 2026-09-16: work-queue layout.
