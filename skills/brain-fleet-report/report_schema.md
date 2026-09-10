# `report.json` — the one file you write

Source of truth: `scripts/report_schema.py`. Unknown keys are rejected. Validate with

```bash
uv run scripts/validate.py <dir>/report.json --kpis <dir>/kpis.json --manifest <dir>/manifest.json
```

An error is one JSON path — fix that path, never regenerate the report. `warn:` lines are steering,
not failure. Keys and the technical half are **English**; the owner half is written in the overlay's
`[owner].lang`, which the collector copies to `manifest.owner_lang` (default `nl`). The `*_nl` field
names are **owner-language fields**; `_nl` is historical. `render.py` renders the owner page's chrome,
labels and dates in that language, and the validator only runs its Dutch heuristic when it really is
Dutch. You never write a prompt blob: fill the `prompt` fields and Python composes the text
(`prompt_compose.py`). Prompts stay English on **both** halves — the owner page shows them in a
copyable accordion with owner-language chrome, so the owner can pass one to a coding agent.

## Root

| Path | Type | Rules |
|---|---|---|
| `schema_version` | int | `1` |
| `report_id` | str | same as `manifest.report_id` |
| `date` | str | `YYYY-MM-DD`, the focus day; must equal `window.focus` |
| `generated_at` | str | ISO timestamp with offset |
| `window` | obj | `{focus, context_days[]}` — copy from `manifest.window` |
| `kpis` | obj | **copy `kpis.json` verbatim** (warn if it differs) |
| `kpis.action_funnel` | obj? | **copy verbatim**: `{focus: {rows, total}, context: {rows, total}, per_axis: [{axis, key, rows, total}], rule: {reviewer_confirmed_after_s, stale_after_h}}`; per-axis covers focus only. Rows/totals carry `action_id`, `proposed_total`, `succeeded`, `failed`, `superseded`, `canceled`, `executing`, `pending`, `stale`, `human_confirmed`, `auto`, `acceptance_rate` (0–1, null with no denominator). `proposed_total` counts all rows in the execution-or-proposal day bucket. |
| `coverage` | obj | **copy `manifest.json` verbatim** — every key of it, including `raw`, `owner_lang` and `ledger_md` (the round-trip is tested; if it does not validate, that is a kit bug, not yours) |
| `findings[]` | ≤ 20 | see below; rank by customer impact, drop the rest |
| `technical` | obj | English half (PJ) |
| `owner` | obj | Dutch half (product owner) |
| `custom_sections[]` | list | pre-rendered project blocks |
| `meta.signal_note` | str | ≤ 1200 — which sections carried signal today |

## `findings[]`

| Field | Rules |
|---|---|
| `id` | `F1`, `F2`, … unique |
| `signature` | the collector's cluster key when the finding has one (`sig=…` in digest.md, verbatim, **not** the privacy-reduced title), else `<kind>:<stable-kebab-slug>` you choose — e.g. `open_feedback:rerun-requests`, `correction:unsupported-completion-claim`, `watch:mailbox-subscription-expiry`. Stable across days: it is what drives recurrence memory |
| `kind` | `lost_runs` \| `action_failure` \| `script_error` \| `capture_gap` \| `correction` \| `open_feedback` \| `policy_question` \| `regression` \| `watch` \| `pattern` \| `good` |
| `audience` | `technical` \| `owner` \| `both` |
| `members[]` | member projects this touches (e.g. `kampadmin`, `kampadmin-support`) |
| `scope` | `{level: project\|member\|channel\|tenant, tenant?, key?}` — `tenant` only on level `tenant` and it must be a `kpis.per_axis` key; `key` names the axis value on level `tenant\|channel\|member` (channel slug, member project, tenant slug) and warns when it is not a `per_axis` key. `level: project` is keyless |
| `severity` | `high` \| `medium` \| `low` \| `good` |
| `impact` | `{customer_effect ≤ 400, runs, threads, confidence}` — what the customer noticed, not what the log said |
| `recurrence` | `{first_seen, last_seen, focus_count, context_count, state: new\|recurring\|gone, known_since?}` — `evidence.json` clusters carry `first_seen`/`last_seen`/`state` on the cluster itself, so copy them; do not transcribe them out of digest prose |
| `title` | ≤ 100, English |
| `text_en` | required for audience `technical`/`both`; ≤ 1400 |
| `text_nl` | required for audience `owner`/`both`; ≤ 1400, in `coverage.owner_lang`. The owner page shows **only** this as prose — it must stand alone, no run ids, no code (the finding's English `prompt` is still rendered below it as a copyable accordion). Links from `evidence.run_urls` render as a neutral "conversation ↗" label, never a run id |
| `evidence` | `{run_ids[], run_urls[]}` — URLs may carry `?t=<token>` (they open for the owner) |
| `root_cause` | `{plane, detail ≤ 600, confidence}` |
| `followup` | optional `{status: done\|partial\|open, evidence ≤ 1200}` |
| `recommendation` | optional ≤ 400 |
| `prompt` | optional; **required when `severity` is `high`**, unless the finding is an owner-audience `policy_question` |

`root_cause.plane` — where the fix lives: `host`, `action_plane`, `brain_script`, `brain_content`,
`tenant_brain`, `persona`, `settings`, `mirror`, `project_code`, `human_policy`, `human_context`, `noise`, `unknown`.

### `prompt`

| Field | Rules |
|---|---|
| `task_kind` | `fix` \| `investigate` \| `decide` (`decide` requires `decision_needed`) |
| `target_repo` | one repo, absolute or `~/…` |
| `paths[]` | file or dir, `path:line` welcome |
| `skills[]` | skills to invoke, e.g. `brain-dream-cycle`, `rc-debug`, `prod-console` |
| `run_refs[]` | **canonical** `https://app.replypen.com/runs/<uuid>` — strip `?t=` |
| `conclusion` | what is actually wrong and why, 1–3 sentences |
| `proposed_change` | the concrete edit, not a direction |
| `verification` | how a fresh agent proves it worked |
| `decision_needed` | optional; the choice a human must make |

Python composes these into 100–220 words. Longer or shorter warns: too short is not actionable, too
long is not editable.

## `technical`

`headline` ≤ 280 · `tldr[]` ≤ 6 (`{text ≤ 280, severity, finding_ids[]}`) ·
`actions[]` (`{rank ≥ 1, finding_id, summary ≤ 300}`, ranked by customer impact) ·
`regressions[]` **list of strings** ≤ 300 each · `watch[]` **list of strings** ≤ 300 each ·
`noise_note` ≤ 600 (counts of what you excluded).

## `owner`

Owner-language fields (`_nl` is historical — the language is `coverage.owner_lang`):

`headline_nl` ≤ 280 (string) · `tldr_nl[]` ≤ 5 items of `{text ≤ 280, severity, finding_ids[]}` ·
`actions_nl[]`, `policy_questions_nl[]`, `good_nl[]` — **plain `list[str]`, ≤ 300 chars each, not
objects** (no `{rank, finding_id, summary}` here; that shape is `technical.actions[]` only) ·
`tenants[]` = `{slug, policy_difference_nl ≤ 400, bullets ≤ 3 items of ≤ 300, finding_ids[]}`.

### Every length cap, in one place

| Field | Cap |
|---|---|
| `technical.headline`, `owner.headline_nl`, `technical.tldr[].text`, `owner.tldr_nl[].text` | 280 |
| `technical.actions[].summary` | 300 |
| `technical.regressions[]`, `technical.watch[]` | 300 each |
| `owner.actions_nl[]`, `owner.policy_questions_nl[]`, `owner.good_nl[]`, `owner.tenants[].bullets[]` | 300 each |
| `owner.tenants[].policy_difference_nl` | 400 |
| `findings[].title` | 100 |
| `findings[].text_en`, `findings[].text_nl` | 1400 |
| `findings[].impact.customer_effect`, `findings[].recommendation` | 400 |
| `findings[].root_cause.detail` | 600 |
| `findings[].followup.evidence` | 1200 |
| `prompt.conclusion`, `prompt.proposed_change` | 20–1200 |
| `prompt.verification` | 10–600 |
| `prompt.decision_needed` | 400 |
| `custom_sections[].title` | 120 |
| `meta.signal_note` | 1200 |
| `findings[]` | ≤ 20 items · `technical.tldr` ≤ 6 · `owner.tldr_nl` ≤ 5 · `owner.tenants[].bullets` ≤ 3 |

A tenant section exists only when that tenant's **policy differs from the project** — not to show a
nice example. Keep tenant-scoped findings under 25% of the pool (soft warning).

## `custom_sections[]`

`{id (lowercase slug), audience, member?, title ≤ 120, html, text}`. `html` is inserted raw into the
matching page, `text` into the `.txt`. Project-side Python may pre-render it.

## Rules the validator enforces

1. `date == window.focus`; ids `F<n>`, unique; every `finding_id` reference resolves.
2. Every `scope.tenant` and `owner.tenants[].slug` exists in `kpis.per_axis` (axis `tenant`).
3. `text_en` for technical/both, `text_nl` for owner/both.
4. `severity: high` needs a `prompt`, except an owner-audience `policy_question`.
5. `prompt.run_refs` canonical; `evidence.run_urls` may be tokenized.
6. Warnings: tenant ratio > 25%, Dutch in an English field (and vice versa — the owner check only
   runs when `coverage.owner_lang` is `nl`), a `scope.key` that is not a `per_axis` key, prompt word
   count outside 100–220 (the warning names the longest field to cut), `kpis`/`coverage` not
   identical to the collector's files. A collector artefact that no longer parses is a `warn:` line,
   never a traceback.
7. With `--evidence evidence.json`, every cited `run_id` must exist in the collected evidence.

## Quiet days

Zero findings is a valid report: write one honest sentence in `technical.headline` and
`owner.headline_nl`, keep `watch[]` for open work, and let the coverage block do the rest. Never
synthesize good news, and never claim an older problem is resolved just because it did not appear.

## Compact example

```json
{
  "schema_version": 1,
  "report_id": "kampadmin",
  "date": "2026-09-04",
  "generated_at": "2026-09-05T07:12:00+02:00",
  "window": {"focus": "2026-09-04", "context_days": ["2026-09-02", "2026-09-03"]},
  "kpis": {"focus": {"date": "2026-09-04", "runs": 63}, "context_days": [], "per_axis": [
    {"axis": "tenant", "key": "lbv", "name": "Landelijke Bond Vakanties", "mode": "live", "runs": 34}
  ]},
  "coverage": {"report_id": "kampadmin", "projects": ["kampadmin"],
    "window": {"focus": "2026-09-04", "context_days": ["2026-09-02", "2026-09-03"]},
    "generated_at": "2026-09-05T07:12:00+02:00", "rc_version": "0.8.5",
    "coverage": [{"feed": "runs", "status": "complete", "fetched": 241}], "excluded": {"simulation": 3}},
  "findings": [{
    "id": "F1", "signature": "verify_link:relation-tenants-missing", "kind": "script_error",
    "audience": "both", "members": ["kampadmin"], "scope": {"level": "project"}, "severity": "high",
    "impact": {"customer_effect": "Working links are stripped from drafts.", "runs": 14,
               "threads": 11, "confidence": "high"},
    "recurrence": {"first_seen": "2026-09-02", "last_seen": "2026-09-04", "focus_count": 6,
                   "context_count": 8, "state": "recurring"},
    "title": "verify_link.py reports every link invalid",
    "text_en": "…", "text_nl": "…",
    "evidence": {"run_ids": ["c725536a"],
                 "run_urls": ["https://app.replypen.com/runs/c725536a-845b-4966-bc80-1f62bdac5f8b?t=OI"]},
    "root_cause": {"plane": "mirror", "detail": "…", "confidence": "high"},
    "prompt": {"task_kind": "fix", "target_repo": "~/code/kampadmin/kampadmin-rootcause-common",
               "paths": ["skills/records/scripts/embed_url.py:169"], "skills": ["prod-console"],
               "run_refs": ["https://app.replypen.com/runs/c725536a-845b-4966-bc80-1f62bdac5f8b"],
               "conclusion": "…", "proposed_change": "…", "verification": "…"}
  }],
  "technical": {"headline": "…", "tldr": [], "actions": [{"rank": 1, "finding_id": "F1", "summary": "…"}],
                "regressions": [], "watch": [], "noise_note": "…"},
  "owner": {"headline_nl": "…", "tldr_nl": [], "actions_nl": [], "policy_questions_nl": [],
            "good_nl": [], "tenants": []},
  "custom_sections": [],
  "meta": {"signal_note": "…"}
}
```

Full realistic example: `fixtures/sample_report.json`; quiet day: `fixtures/quiet_report.json`.
