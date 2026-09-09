# The per-brain overlay — `<brain>/_internal/fleet-report/`

Everything project-specific lives here; the kit scripts stay generic. Without an overlay the report
still works: one project, defaults, no project follow-up.

```
<brain>/_internal/fleet-report/
  config.toml   window, members, noise, repos                (read by fr_common.load_overlay)
  overlay.py    optional hooks, pure stdlib, fail-soft
  OVERLAY.md    guidance for the judging LLM (triage table, repo paths, dig-deeper recipes)
  ledger.md     human dispositions of known patterns
```

**Commit it, but keep it out of runs:** `/_internal/` must be in the brain's `.replypenignore` (it
already is in most brains). The report output (`.rootcause/`) is gitignored.

## `config.toml`

| Key | Meaning |
|---|---|
| `report_id` | output dir + `report.json.report_id`; defaults to the project name |
| `display_name` | human label used in report titles and ticket subjects |
| `timezone` | day boundaries (default `Europe/Brussels`, DST-correct) |
| `[[members]]` | `project` — one report over 1..n projects; `channel_label` labels the half (read by you, not by the scripts) |
| `[owner]` | `name`, `email`, `lang` — who the owner half is written for. `lang` (default `nl`) is **load-bearing**: it reaches `manifest.owner_lang`, and `render.py` renders the owner page's chrome, labels and dates in it while `validate.py` skips the Dutch heuristic. The `*_nl` field names stay as they are; they mean "owner language" |
| `dev_tenants` | tenant slugs that are test beds, excluded and counted |
| `noise_topics` | case-insensitive substrings of the run topic that are never support work |
| `noise_senders` | same, on the sender |
| `known_non_names` | words the privacy reducer must not mistake for a first name |
| `include_kinds` | override the default `["email", "analysis"]` |
| `[[repos]]` | `id`, `path`, `plane` (`brain\|tenant_brain\|mirror\|project_code\|host`), optional `relevant_paths` — scanned by `correlate.py` for onset candidates |

TOML has no globs: enumerate tenant overlay repos explicitly, one `[[repos]]` row per tenant.
`relevant_paths` matters on the host repo — without it every host commit becomes a candidate.

## `overlay.py` hooks

All optional, all called through `Overlay.call()`, which catches every exception and records it in
the manifest instead of losing the report. Be defensive anyway.

| Hook | Signature | Called by |
|---|---|---|
| `classify_run` | `(run: dict) -> list[str]` — extra tags on the reduced run dict. **A tag starting with `noise:` excludes the run**: it drops out of `counted`, out of every cluster denominator, and is counted in `excluded` under the tag name. Use it for exclusions `config.toml` cannot express — vendor identity, sender heuristics, anything needing more than a topic substring | `collect.py` |
| `channel_of` | `(run: dict) -> str \| None` — axis key on a tenantless project | `collect.py` |
| `drill` | `(run: dict, ctx: dict) -> str \| None` — markdown appended under "## Project follow-up" | `drill.py` |
| `custom_sections` | `(evidence: dict) -> list[dict]` | reserved — no script calls it yet; pre-render sections yourself into `report.json.custom_sections[]` |

`classify_run` sees the run *before* exclusion, with its reduced trace header attached:
`run["trace"]["question_head"]` is the first 600 characters of the inbound message and
`run["trace"]["guards"]["injection_scan"]["rationale"]` names the sender's nature. Match on those
first — `topic` is the model's own summary (it never says "sentry") and an errored run has no topic
at all.

An overlay `drill` that reads a live project database must stamp its output `as of <time>`: the DB
answers as of now, not as of the run, and the difference between "the agent misread the agenda" and
"the human moved it afterwards" is exactly that stamp.

`ctx` keys are documented in `scripts/drill.py`'s module docstring: `rc` (the disk-cached read-only
`fr_common.Rc`) + `rc_args`, `show`, `thread`, `actions`, `trace`, `steps`, `deltas`, `feedback`,
`tz`, `brain_root`, `privacy`, `fr`. Everything you emit must pass through `ctx["privacy"]`.
Import nothing from the kit — use `ctx["fr"]` if you need a helper.

## `OVERLAY.md` — the *Owner reach* table

The kit decides `findings[].audience` by **reach** (SKILL.md § *Audience split*), and the generic
plane table only knows RootCause planes. `OVERLAY.md` adds a section `## Owner reach` that names the
project's concrete owner surfaces — the admin dashboard, master data, source-system configuration,
policy calls — and, opposite it, what is developer-only. Two columns, one row per surface, written
so the judging LLM can look at a finding and say "owner" or "technical" without guessing. Without
this section the LLM falls back to the generic table and the owner half degrades into a diluted copy
of the technical one.

## `ledger.md`

A markdown table the judging LLM subtracts before writing findings — the disposition of patterns
already understood. (`state/ledger.json` is the *automatic* signature memory; this file is the human
one.) When the overlay has no `ledger.md`, the digest's coverage line says so.

**A ledger row may never forward-reference the report being written.** "prompted in the 4/9 report"
is circular the moment the 4/9 report is what you are judging: the row must stand on what already
happened (a commit, a deploy, a decision), or it is not a disposition.

```markdown
| Since | Pattern | Disposition | Re-test when |
|---|---|---|---|
| 2026-09-04 | Shadow tenants: large sent-deltas because the practice attached a document | benign — the agent cannot produce attachments | an attachment flow exists |
```

## Two worked examples

**kampadmin — one report over two projects.** `kampadmin` (parent/participant e-mail) and
`kampadmin-support` (admin Embassy tickets + chat) are two `[[members]]` with `channel_label`s, one
`report_id = "kampadmin"`, one owner. They share a mirror repo, so one `[[repos]]` row with
`plane = "mirror"` explains symptoms in both halves at once; findings carry `members[]` so the
technical page can say which channel bled.

**dentai — a DSN-backed drill.** `overlay.py`'s `drill` hook reconstructs every
`search_availability.py` invocation from the trace steps, then reads the practice's real agenda live
through the public guarded primitive:

```python
rc.json(*ctx["rc_args"], "dev", "console", "database", "query", "DENTAI_DSN", sql)
# -> {"columns": [...], "rows": [[...]]}
```

`rc_args` already carries `--project` and `--tenant`, so the query is tenant-scoped by the server
([`prod-console`](../prod-console/SKILL.md)). Keep such a drill cheap — a handful of queries per run,
never a per-day sweep.

## Optional owner feedback ritual

```toml
[feedback_review]
enabled = true
cadence = "weekly" # or "daily-lite"
```

Run [brain-feedback-review](../brain-feedback-review/SKILL.md) over the same requested window.
Copy its self-contained `report.html` alongside `owner.html` as `feedback-review.html`; hand over
both attachments. The owner page links it for multi-day reports, or every report with `daily-lite`.
With `daily-lite`, generate and attach the questionnaire every day; do not render a link-only delivery.
The setting selects the report workflow; it does not itself schedule a job or apply answers.
