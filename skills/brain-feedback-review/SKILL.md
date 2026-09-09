---
name: brain-feedback-review
description: Build a weekly owner feedback questionnaire from scored/commented runs and sent-versus-proposed deltas, then export confirmed answers as a Brain-changes instruction. Use for a ten-minute feedback ritual, outdated-source review, or a feedback-only backlog. Read-only until the owner pastes their answers.
---

Python collects, you judge, Python renders. Work from the project brain; use public `rc` only.
Owner language comes from `_internal/fleet-report/config.toml`, `[owner].lang` (default `nl`; `en`
also supported). Needs Python 3.11 via `uv`, `rc` login, and Node for the identical CLI/browser exporter.

```bash
REVIEW=<installed skill path>
uv run "$REVIEW/scripts/collect.py" --days 7            # --project NAME --tenant SLUG
# --date YYYY-MM-DD: calendar window ending that day, matching fleet (default rolling)
# --only-feedback: scores/comments only, including positive score examples
# --days 30: backlog; explicit --out-dir and --refresh supported
# Read evidence.json progressively; raw/ contains the unabridged public responses.
# Write review.json beside evidence.json using schema.md.
uv run "$REVIEW/scripts/render.py" <output>/review.json  # report.html + report.md
```

All artifacts stay in gitignored `.rootcause/feedback-review/`. HTML is self-contained, works from
disk, stores answers locally when the browser permits, and provides a selectable-copy fallback.
Send the attachment only when asked; otherwise hand over its absolute path. Rendering changes no
brain, settings, feedback rows or customer data.

1. **Gather and check coverage.** The learning feed uses feedback-update/send time and caps each
   plane at 100. Supplement with paginated `run list --reviewed`: the learning feed omits positive
   scores and held-out evaluations. Supplementary rows use run creation time because their review
   timestamps are not exposed. The collector keeps those rows review-only: never override
   `learning_allowed=false`. A missing/capped feed must be stated in the owner-language coverage
   note. Do not claim all feedback was found when the public surface cannot prove it.
2. **Judge.** Explicit feedback first, consequential live deltas second. Read `detail.header`,
   `detail.show`, `detail.thread`, and trace steps for question, proposed draft and visible sources.
   Use full paired delta bodies when present; `bodies_scrubbed` means unavailable, never reconstruct
   a quote from `delta_description`. Shadow means independent human answer, not an edited draft;
   preserve that distinction. A positive shadow verdict is not a request to copy its wording.
3. **Questionnaire.** Aim for ten minutes: group repeats; account for omitted evidence. Each card
   has a short inbound question, proposed vs sent text (collapsed), score/comment, consulted sources
   only when actually visible, and 1–3 closed questions with optional detail. Do not invent the
   correct answer or preselect confirmations. Ask only what changes a decision: general rule vs
   one customer, preferred wording, or a named source to prefer/avoid. Use score-only cases for
   “fine / explain the miss”, not inferred policy. No run ids, technical trace prose, tokenized links,
   customer identities or raw contact details in owner prose. Business contact aliases necessary
   for an explicit rule may appear in its proposed learning; avoid one-off customer facts.
4. **Progress.** Read the previous same-scope, same-length report when present. Compare feedback
   count, score distribution (with denominator), paired-delta coverage and recurrence of the same
   issue. Compare actual later evidence against previous confirmed lessons, not against unanswered
   proposals. `report.md` is the initial unanswered export; owner selections remain in the browser
   until copied. Save received answers locally if you need them next week. No comparable baseline
   means say so; absence of a repeated error does not establish resolution.
5. **Handoff.** Recommend the project's **Brain-changes** prompt box: one-shot small edits with
   provenance on that page. The exporter asks for missing/incorrect knowledge only; each confirmed
   bullet has type, scope and evidence. “Fine” becomes an explicit do-not-change entry; unanswered,
   excluded and unclear choices remain open. For customer-only choices require detail and a tenant-scoped review; project-scoped exports keep them open; do not put
   that customer's facts into a shared project brain. Open the actual tenant's Brain-changes surface
   for tenant changes; text labels cannot bind the write scope.

The prompt box is an instruction-to-edits path, **not an exact-write file upload**. The model proposes
changes; the service applies them. Do not generate a tool envelope or assume all bullets fit into one
pass: check the resulting changes and unapplied points. Persona/wording goes through settings
proposals; check those separately. No promise that a pending proposal is already effective.
Use `chat?lane=setup` instead when answers conflict, a target scope is unclear, or the owner wants
clarification first; start with “Help clarify the open points before proposing changes.”

For a local assistant receiving the answers, use [brain-dream-cycle](../brain-dream-cycle/SKILL.md)
to apply each confirmed lesson in its existing home, then verify/publish. Receiving this questionnaire
alone is not permission to apply its proposed answers.

## Fleet integration

Enable `[feedback_review]` in the [fleet overlay](../brain-fleet-report/overlay.md):
`enabled=true`, `cadence="weekly"` (or `"daily-lite"`). This is an optional sibling report, not a
new daily findings section. Run the collector over the requested window, judge/render separately,
and copy `report.html` beside the fleet owner report as `feedback-review.html`. Deliver both files
so the relative link works; each still opens independently. A weekly fleet report uses
`brain-fleet-report/scripts/collect.py --days 7`; its `--date` is the last included day.
