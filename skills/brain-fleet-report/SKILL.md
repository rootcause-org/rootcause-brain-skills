---
name: brain-fleet-report
description: "Build the daily two-audience fleet report for one rootcause project from a brain checkout: Python collects a day of evidence (runs, actions, bash corpus, deltas, feedback, commits), you judge it and write report.json, Python validates and renders it. Use for 'fleet report', 'dagrapport', 'daily report for the product owner', 'what did the agent do yesterday', or a ranked 'what do I fix today' list."
---

# brain-fleet-report — one day, two audiences, ranked actionables

**Python is the evidence, you are the judgement.** The scripts never decide what is wrong; they
collect, cluster, count recurrence, validate and render. You read the digest, drill what matters,
and write one `report.json`.

North star: **"what do I fix today"** — findings ranked by customer impact, each high one carrying a
copy-paste prompt for a fresh coding agent. KPIs are context, not the point.

Two halves of one report: **technical (EN)** for the developer — failing actions with error text,
brain-script breakage, capture gaps, lost runs, correlating commits — and the **owner half** for the
product owner, who sees `text_nl` only: no run ids, no technical prose — conversation links are
fine, and each finding's copyable **English prompt** rides along so the owner can hand it to a coding
agent as is. The owner language is the overlay's `[owner].lang` (default `nl`, `en` when the owner is you);
it travels through `manifest.owner_lang`, and `render.py` puts the page chrome in that language. The
`*_nl` field names are historical.

Read-only. Every `rc` call is a list/show/trace. Never `rc ask` from here, never edit a brain from
here — the report *produces* prompts, other skills execute them
([docs/side-effects.md](../../docs/side-effects.md)).

## Prerequisites

- `rc auth status` with an **all-projects** token (a combined report fans out over members).
- A brain checkout of any member project; run from its root. Output goes to the gitignored
  `<brain>/.rootcause/fleet-report/<report_id>/<D>/`.
- Optional but strongly recommended: the project's overlay at `<brain>/_internal/fleet-report/`
  ([overlay.md](overlay.md)). Without it you get one project, defaults, and no project follow-up.

## Pipeline

```bash
cd ~/code/rootcause-org/rootcause-brain-<project>          # any member checkout
FR="$PWD/.agents/skills/brain-fleet-report"                # installed kit skill
D=2026-09-04

uv run "$FR/scripts/collect.py" --date "$D"        # --project X (repeatable) --report-id --context-days 4
                                                   # --refresh | --offline | --no-correlate | --prune-raw
                                                   # last line of its summary = the output dir
OUT="$PWD/.rootcause/fleet-report/<report_id>/$D"
cat "$OUT/digest.md"                               # tier 0 — the only file you read whole
cat "$OUT/commits.md"                              # correlation detail when a signature is new

uv run "$FR/scripts/drill.py" --date "$D" --run f89d3d89          # tier 1 → details/run-<run8>.md
uv run "$FR/scripts/drill.py" --date "$D" --run f89d3d89,a1b2c3d4,9e8f7a6b   # batch, one file each
uv run "$FR/scripts/drill.py" --date "$D" --cluster action_failure:create_placeholder
                                                   # the `sig=` key from digest.md; a title
                                                   # substring also matches. One excerpt per run,
                                                   # per-axis/per-fate split, every run URL.
uv run "$FR/scripts/drill.py" --date "$D" --delta f89d3d89       # proposed vs sent, in full
uv run "$FR/scripts/drill.py" --date "$D" --feedback             # every score+comment, joined to its run

# write $OUT/report.json yourself — fields in report_schema.md, examples in fixtures/
uv run "$FR/scripts/validate.py" "$OUT/report.json" \
    --kpis "$OUT/kpis.json" --manifest "$OUT/manifest.json" --evidence "$OUT/evidence.json"
uv run "$FR/scripts/render.py" "$OUT/report.json"                 # 6 deliverables next to report.json
uv run "$FR/scripts/prompt_compose.py" "$OUT/report.json" --finding F3   # check one prompt as text
open "$OUT/technical.html" "$OUT/owner.html"                      # read both halves locally
```

Delivery to the owner is project-specific: a wrapper skill in the brain (e.g. `fleet-report-dentai`)
attaches `owner.html` to a ticket.

`evidence.json` is the raw tier — grep it, never read it whole. Copy `kpis.json` and `manifest.json`
verbatim into `report.json` (all keys, including `raw` and `owner_lang`); never retype a counter.
Clusters in `evidence.json` carry `first_seen`/`last_seen`/`state` — copy those into
`findings[].recurrence` instead of transcribing digest prose.

## Window semantics

Focus = day `D`. Context = the 4 workdays before it, for recurrence only. Every signature carries
`new | recurring | gone` plus `first_seen/last_seen/known_since`.

- Report the focus day. Context days are pattern background — **do not make hard claims about them**
  (you did not drill them).
- **Absence is not resolution.** `gone` means "not seen on D", never "fixed". Say so in those words.
- Recurrence beats volume: a `new` high-severity signature outranks a familiar noisy one.

## Data semantics — how to not be wrong

- **Draft fate ladder**: `sent_as_proposed | edited | shadow_compared | not_sent_yet | no_draft`. In
  draft-mode projects a placed draft is **not** proof of a send — never claim the customer received it.
- **Deltas**: per-delta `shadow` flag. A shadow delta is a blind comparison against what the human
  wrote independently — **not a correction**, and `equivalent` is positive evidence. A live delta
  (non-shadow) in a live mailbox is a real human edit and the strongest adoption signal you have.
- **`[[✏️]]` fills** are counted as a cluster, not as individual deltas; a shipped unresolved
  placeholder is a capture gap.
- **Feedback**: comments are near-100% actionable, scores are ambiguous. Quote the comment; treat a
  score-1 escalation-ledger comment (e.g. "escalated: <clickup url>") as a human workflow, not a bot
  failure. Open feedback older than the window is its own finding class.
- **Actions**: `proposed` = recorded, never executed — a stale `proposed` pile is a finding, not a
  failure. Classify a failure as infra (RootCause machinery — report, do not edit the brain) or
  domain (the action's own refusal) before proposing anything.
- **Bash corpus**: exit `-1` = timeout, exit `64` = the context re-read guard, `rg`/`grep` exit 1 =
  noise. `usage:` lines are brain-script flag errors and are usually a brain-content fix.
- **`error_message` is server-truncated (~80 chars)**; the manifest says how many. Do not extrapolate
  a root cause from a cut string — drill.
- **Reduced text is not corrupted text.** Every digest line and every drill excerpt is post-privacy
  (first names shortened, e-mail addresses dropped, sometimes mid-sentence). Before filing "the agent
  emitted mangled output", check the same string in `raw/` — that is the unreduced original.
- **`⟦pii:…⟧` in `draft_markdown` is server-side masking**, applied inconsistently (an unfamiliar
  first name comes through in the clear). It is not a placeholder the agent shipped, and never a
  finding on its own.
- **`sent_as_proposed` in a shadow tenant** means the practice sent its own mail and it matched the
  agent's draft — *not* that our draft reached anyone. Check the thread before writing impact.
- **A lost or errored run must be checked against its thread** before you claim the customer got
  nothing: threads are re-processed, often hours later. digest.md marks it `↻ recovered <HH:MM>`;
  `drill --run` shows the whole thread.
- **Live grounding-DB reads are as of now, not as of the run.** An overlay drill that queries the
  project database stamps `as of <time>`; anything the customer changed in between is already in
  the answer. That gap is the difference between "the agent misread the agenda" and "the human
  moved it afterwards".
- **Excluded runs are not in any denominator.** A run excluded as noise contributes no cluster count;
  its run-level error still shows, flagged `excl`. digest.md prints the window total and the
  focus-day total separately — read the label before quoting a number.
- **Coverage**: any feed at `partial`/`unavailable` must be said out loud in `meta.signal_note`, and
  no claim may rest on the missing part.

## Drill before you judge

`findings[].signature` is the collector's cluster key when the finding has one — the `sig=` line in
digest.md, copied verbatim (the bold title next to it is privacy-reduced and truncated, so it is not
an identifier). Delta-, feedback-, watch- and policy-derived findings have no cluster: give them
`<kind>:<stable-kebab-slug>` and reuse the same slug tomorrow, or recurrence memory fragments.

Drill **every** finding you intend to mark `high`, and **every** consequential correction (a live
delta that changed the answer's substance). One line in the digest is never enough to name a plane.
Stop drilling when more detail would not change the action.

Choose `root_cause.plane` only after the drill: `host`, `action_plane`, `brain_script`,
`brain_content`, `tenant_brain`, `persona`, `settings`, `mirror`, `project_code`, `human_policy`,
`human_context`, `noise`, `unknown`.

**Commit correlation is correlation.** A commit that landed *after* a signature's first occurrence
cannot explain onset — at most expansion or recovery. Candidates in `commits.md` narrow the search;
you decide.

## Audience split

- Technical half and **all prompts**: English. Owner half: owner language, standing on its own —
  with the English prompt accordions attached (copy button on the page, plain `<pre>` in the e-mail
  and text variants).
- Tenant-scoped findings ≈ 1 per 5 project findings (validator warns above 25%). A tenant section
  exists only when that tenant's **policy demonstrably departs from the project** (public says 14
  days, this tenant does 7) — never to show a nice example.
- Never synthesize good news. A `good` finding needs the same evidence as a bad one.
- **Quiet day**: zero findings is a valid report. One honest sentence in `technical.headline` /
  `owner.headline_nl`, keep `watch[]` for open work, let coverage do the rest.

## Prompt rules

You fill structured fields; `prompt_compose.py` writes the blob (100–220 words).

- `run_refs` are **canonical** `https://app.replypen.com/runs/<uuid>` — strip `?t=`. Tokenized URLs
  belong in `evidence.run_urls` only; the owner page renders them as a neutral "conversation ↗"
  link (numbered when there are several), never as a run id.
- `target_repo` and `paths` are absolute (or `~/…`), pointing at the checkout that owns the plane.
- Name the skill that should do the work: [`brain-dream-cycle`](../brain-dream-cycle/SKILL.md) for a
  lesson from feedback/deltas, [`brain-ask`](../brain-ask/SKILL.md) to verify on prod,
  [`rc-debug`](../rc-debug/SKILL.md) for one more run, [`prod-console`](../prod-console/SKILL.md) for
  a guarded production primitive, [`brain-publish`](../brain-publish/SKILL.md) to ship.
- Voice, tone, salutation, signature, language → **persona/triage settings**, never a markdown edit.
- `conclusion` says what is wrong and why; `proposed_change` is the concrete edit, not a direction;
  `verification` is how a fresh agent proves it worked.

## Privacy

First names only, no e-mail addresses, no phone numbers, no tokens — in prompts, in HTML, anywhere.
The collector's privacy reducer already runs over the digest; keep it that way when you quote.
`evidence.json` and `raw/` stay local (the whole `.rootcause/` tree is gitignored).

## More

[report_schema.md](report_schema.md) the one file you write · [overlay.md](overlay.md) the per-brain
contract · [`rc-fleet`](../rc-fleet/SKILL.md) interactive triage when you have no report ·
[docs/side-effects.md](../../docs/side-effects.md).

## Iteration log

- **2026-09-07** — built (collect/correlate/drill/schema/validate/render + overlays). First
  real runs: dentai, kampadmin (+kampadmin-support), pro-backup, momentum-tools on 09-03 / 09-04.
