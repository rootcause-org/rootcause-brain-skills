# report.json v2

Source of truth: `scripts/report_schema.py`. Unknown keys and v1 reports fail validation.
Array order is rank. English technical prose/prompts; owner prose uses `coverage.owner_lang`.

| Root field | Contract |
|---|---|
| schema_version | Exactly 2 |
| report_id, date, generated_at | Report identity, focus YYYY-MM-DD, timestamp |
| window | Collector window; date equals focus |
| kpis, coverage | Copy kpis.json and manifest.json verbatim, including raw/owner_lang/action_funnel |
| findings | At most 12, unique IDs and signatures, ranked |
| technical | headline ≤220; optional noise_note ≤400 |
| owner | headline_nl ≤220 |
| meta | Optional signal_note ≤600 |

No TLDR/actions/watch/regressions arrays, tenant sections, good-news sections or custom HTML.
The lede adds context without repeating cards. Diagnostics own counters, funnel and coverage.

## Findings

| Field | Contract |
|---|---|
| id, signature | F1 etc.; stable collector signature or `<kind>:<slug>` across days |
| kind | lost_runs, action_failure, script_error, capture_gap, correction, open_feedback, policy_question, regression, watch, pattern, good |
| audience | technical, owner, both |
| members | Member project names |
| scope | level project/member/channel/tenant; tenant required only for tenant, key optional except project; tenant must exist in per_axis |
| severity | high, medium, low, good |
| recurrence | first_seen, last_seen, focus_count, context_count, state new/recurring/gone, optional known_since; copy evidence cluster |
| title | English ≤90 |
| status | new, changed, unchanged |
| evidence | run_ids[], run_urls[]; run URLs may carry access tokens |
| impact | runs, threads; no prose |
| root_cause | plane + confidence low/medium/high; no detail prose |
| text_en | ≤700; required technical/both |
| text_nl | ≤500; required owner/both; no code/run IDs |
| ask_nl | ≤300; required owner/both; explicit decision/options or admin task |
| ask_for | Optional person ≤40 |
| update_en, update_nl | ≤200; required for relevant audience when changed/unchanged; forbidden for new |
| prompt | Structured below; required high except owner policy_question |

Planes: host, action_plane, brain_script, brain_content, tenant_brain, persona, settings,
mirror, project_code, human_policy, human_context, noise, unknown.

`unchanged` forbids impact, root_cause, text_en, text_nl, ask_nl, ask_for and prompt, including null
values. It requires a prior v2 signature with a prompt or owner ask. The reader resolves chained
carry-overs, retaining the original prompt date. `changed` requires a prior signature and full fields.
New/changed require impact and root_cause. `gone` never means resolved.

Prior reports are earlier dated sibling directories (`YYYY-MM-DD` or `YYYY-MM-DD-Nd`), filtered by
report_id and cadence (daily never inherits weekly). Reusing a prior signature as `new` is an error. `scripts/prior.py <OUT>` prints the same table appended by collect.py to the digest.
For fixture rendering/validation pass `--prior-dir fixtures/prior/2026-09-03` explicitly.

## Prompt

| Field | Contract |
|---|---|
| task_kind | fix, investigate, decide |
| targets | Nonempty list of `{repo, paths[]}`; existing absolute/~/ checkout, nonempty existing paths inside it; `:line` suffix supported |
| run_refs | Canonical `https://app.replypen.com/runs/<uuid>`, never tokens |
| repro | Optional ≤400: concrete command or exact trace step; missing on fix warns |
| conclusion | 20–600: known diagnosis or bounded unknown |
| change | 20–900: fix steps per repo; investigation checks + stop condition + output path; decide branches |
| done_when | 10–400: immediate verifiable completion criteria |
| decision | Required for decide: question ≤300, 2–4 options ≤120 each |

Composer emits uppercase task kind/title, start checkout + AGENTS.md, other targets, files, canonical
runs, diagnosis, optional repro, change/checks/options, completion, decision, boundaries. It excludes
recurrence and skills. Warns outside 90–260 words and on hedged fixes (“locate”, “consider”, “either”,
“investigate whether”, “or (b)”, “read the full error”). Production remains read-only; never rc ask or
confirm actions. Commit; publish only when explicitly required by Change.

Owner prompt rendering requires **all**: owner/both, fix, brain_content/tenant_brain, and every
resolved target checkout name starts `rootcause-brain-`. Otherwise only the human ask appears. Path
containment prevents another repo being smuggled through an absolute path or symlink.

Validation warns on accepted/noise ledger rows containing the signature and bold disposition (rows must include the exact signature; prose-only ledgers still require author review), and
on new titles sharing ≥60% of their significant words with another prior signature. These are
steering, not proof of identity. Collector KPI/manifest comparisons and evidence-ID checks remain.

## Compact example

```json
{
  "id": "F1", "signature": "policy_question:refund-window", "kind": "policy_question",
  "audience": "owner", "members": ["example"], "scope": {"level": "project"},
  "severity": "high", "status": "new", "title": "Choose the refund window",
  "recurrence": {"first_seen": "2026-09-16", "last_seen": "2026-09-16",
    "focus_count": 1, "context_count": 0, "state": "new"},
  "evidence": {"run_ids": [], "run_urls": []}, "impact": {"runs": 1, "threads": 1},
  "root_cause": {"plane": "human_policy", "confidence": "high"},
  "text_nl": "De praktijk hanteert een andere termijn dan de huidige instructies.",
  "ask_nl": "Geldt de termijn van zeven of veertien dagen?", "ask_for": "Eigenaar"
}
```

Full root examples: `fixtures/sample_report.json`, `fixtures/quiet_report.json`.
The sample targets `/tmp` solely to demonstrate portable path validation; real reports name the
actual checkout/files inspected by the author. Tests create isolated brain repos for owner prompts.
