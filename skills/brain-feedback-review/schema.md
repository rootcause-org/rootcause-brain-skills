# review.json

Model-authored, beside the collector's `evidence.json`. No HTML fields; renderer escapes everything.

```json
{
  "project": "example", "tenant": "", "owner": "Owner", "lang": "nl",
  "period": "2026-09-01 t/m 2026-09-07",
  "coverage_note": "Welke teksten/bronnen ontbreken, welke limieten zijn geraakt.",
  "comparison_note": "Vorige vergelijkbare week versus nu, met noemers; of geen nulmeting.",
  "omitted_run_ids": [], "omission_note": "",
  "items": [{
    "run_id": "copy from evidence", "url": "copy from evidence",
    "learning_allowed": true,
    "question": "Korte klantvraag zonder persoonsgegevens",
    "proposed": null,
    "sent": null,
    "feedback": "Score plus privacyveilige opmerking; zeg wanneer geparafraseerd",
    "sources": null,
    "availability_note": "Alleen feedback bewaard; antwoordteksten verlopen, bronnen niet vastgelegd.",
    "questions": [{
      "id": "stable-topic-slug", "text": "Geldt deze regel algemeen?", "type": "rule",
      "options": [
        {"value": "yes", "label": "Ja, algemeen", "effect": "confirm", "scope": "general", "learning": "Concrete, evidence-backed requested change"},
        {"value": "customer", "label": "Alleen deze klant", "effect": "confirm", "scope": "customer", "learning": "Same rule, narrowly scoped"},
        {"value": "fine", "label": "Was goed; niets wijzigen", "effect": "fine"},
        {"value": "unsure", "label": "Eerst uitklaren", "effect": "defer"}
      ]
    }]
  }]
}
```

Types: `rule | wording | source-prefer-avoid | persona`. One to three questions per item; unique
question ids across the report; two to five options. A selected `confirm` exports only that option's
learning plus the owner's detail. Customer scope also requires a tenant-scoped review; otherwise it stays open. `fine` forbids a change; `defer` remains open. Never encode a vague
“other / I'll explain” as a confirmed fixed learning. Reviewed examples absent from the learning feed
retain `learning_allowed=false` (they may be held-out); all their selected changes remain open.

Every evidence item must appear once or in `omitted_run_ids`; explain grouping/omission. Evidence
links preserve the API-issued `?t=` access token verbatim in HTML and copied markdown. If no issued
link exists, set `url: null`; never fabricate a tokenless link. Treat the report and copied markdown
as private owner handoffs, not public artifacts. Raw IDs live only in the
embedded data and link targets, not displayed prose.

For received answers, `answers.json` maps question ids to `{ "choice": "yes", "detail": "…" }`.
`node scripts/answers.js review.json answers.json` exports the same markdown as the copy button.
The initial `report.md` has no confirmations. Never substitute the model's selections for the owner's.

`proposed`, `sent`, `sources`: real content, or null/omitted/empty when unavailable. Never fill these
fields with “not available” prose. The renderer hides empty sections. Use one short `availability_note`
per affected item; HTML shows it directly and markdown carries it beside the evidence reference.
Do not ask the owner to grade a proposal they cannot see; ask about the preserved feedback instead.
