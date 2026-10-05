---
name: brain-helper-devloop
description: "The feedback loop for adding or changing a brain/mirror helper script: design from real tickets, build read-only and verdict-first, prove on real data (mirror-try, scope-check), push a dev ref, ask synthetic questions with rc ask, read the trace to see whether the agent picked the helper up unprompted, fix developer experience instead of adding prompt rules, publish, re-ask live. Use when a support topic needs a deterministic helper or case runbook and you want evidence that production runs actually use it."
---

# brain-helper-devloop — build a helper the run agent actually reaches for

A helper is only worth its bytes when a production run opens its SKILL.md and calls it **without
being told to**. This loop measures exactly that and keeps fixes on the developer-experience side
(names, `--help`, routing lines, output shape), not on the steering side (prompt rules per failed
run — [run-trace-model § Debug Discipline](../../docs/run-trace-model.md#debug-discipline)).

Each step links the skill that owns the mechanics; this file owns the order and the stop rules.

## 1. Design from tickets, not from the spec sentence

- Start from 5–8 **real** tickets of the topic (support dump, `rc run list`, help-centre threads).
  Write down per ticket: the admin's opening symptom, what the human actually replied, which record
  ids they looked at. The reply is your expected answer; the spec line is intent, not a contract.
- Read the **source rule** before porting it (the model/service in the mirror). Note the file +
  method in the helper docstring with a "verified <date>" so the next maintainer can diff.
- Check what already exists: a renderer section (`*_to_md`), a pure module, a KB article. Extend the
  existing seam when a symptom is "one record"; add a new script only for a new question shape
  (per-person sweep, export membership, cross-record diagnosis).
- 80/20: one helper, ≤ ~60 lines of output, verdict in the first two lines, `--json` for chaining.
  No settings, no per-tenant knobs, no writes ([side-effects](../../docs/side-effects.md)).

## 2. Build read-only, scope-safe

- One primary table → `rows()`; enrichment tables → `optional_rows()` / `is_visible()` so a hidden
  table degrades a section instead of killing the answer ([scope-check § The Rule](../scope-check/SKILL.md#the-rule)).
  Never add tenant/deleted filters; the host views already encode them.
- Importable + CLI, catalog header in the first 40 lines (`# name:` / `# purpose:` / `# args:`,
  [prod-console § catalog](../prod-console/SKILL.md#brain-script-catalog-convention)) so the script
  is discoverable through `bash list`.
- The module docstring is read by the grounding pre-step: put the admin's symptom words and one
  copy-pasteable CLI example in it, not only in SKILL.md.
- Every lookup flag accepts the words the admin uses (a name, an address), not only a uuid, and lists
  candidates on ambiguity: a uuid-only flag costs the agent 3–4 guessing turns.
- Error text names the **next call** ("not a subscription id — try `--person-id`"), never a trace.
- Unit tests only for non-trivial ported rules (money, eligibility, dates). Pure functions take rows
  in, lines out, so the tests need no database.
- Register it: the owning `SKILL.md` (one call line + one "when" sentence), and ONE routing line in
  the brain `AGENTS.md` only when the helper is the obvious entry point for a symptom. A case
  runbook (`skills/cases/<slug>.md`: symptom → steps → helper → what to say / escalate) is the
  place for procedure, not AGENTS.md.

## 3. Prove the bytes before proving the agent

| Rung | Proves | Skill |
|---|---|---|
| offline pytest / `brain_test.py` | the ported rule | [local-brain-work](../local-brain-work/SKILL.md) |
| `mirror_try.py --diff` on the real ticket ids | the staged helper on real scoped data, before vs after | [mirror-try](../mirror-try/SKILL.md) |
| `scope_smoke.py --cmd …` per audience | exit 0 for the narrow admin, not only for you | [scope-check](../scope-check/SKILL.md) |

`mirror-try --diff` stages every uncommitted file in the mirror (other threads' too) and blows the
240 KB budget; commit your own files and use `--ref`. Its privacy reducer shortens capitalised words
(`Meerdere` → `M.`): never "fix" a label you only saw through it. Severity (✓/⚠/✗) is a product call:
run the helper on 3+ live records before deciding what counts as a problem.

Stop here if the helper is wrong on the real ticket record: the agent test below would only measure
pickup of a wrong answer.

## 4. Ask the brain like the admin would

- Push the brain change to `dev/<branch>`; a mirror change must be refreshed into the mirror first
  (`rc dev mirror refresh --expect-sha`, [prod-console § Freshness](../prod-console/SKILL.md#workflow)).
- Derive 5–8 **synthetic admin questions** from the tickets: the admin's words, chat style, anonymised,
  with the real record ids a ticket needs. Never invent ids. Keep the question path-free — do not
  name your helper or SKILL file; that is the thing under test.
- `rc ask "<question>" --brain-ref dev/<branch> [--tenant <slug>]` ([brain-ask](../brain-ask/SKILL.md)).
  Capture every `run_id`. For the narrow-admin pickup test add `--principal-kind <kind>
  --principal-id <id>` (works on `rc ask` and `mirror-try`).
- Tickets drift: re-derive each question and its expected answer from **today's** record state (half
  the ticket situations are usually fixed in prod by now); judge the run against what the helper
  reports now, naming the historical cause as a likely explanation is correct, not a miss.

## 5. Read the trace for pickup, not only for correctness

`rc run debug <run_id>` ([rc-debug](../rc-debug/SKILL.md)); per run record four things:

1. **Opened** — did the main loop read your SKILL/case file? (files-read section; grounding
   pre-step selections are hints, not proof).
2. **Called** — did it run the helper with sane arguments? (timeline `bash` commands).
3. **Right** — does the answer match what the human replied? Note what it still got wrong.
4. **Cause of a miss** — grounding (file never surfaced), routing (surfaced but not opened),
   helper output (called, verdict buried or misread), or model (everything right, wrong prose).

Table it: question → opened/called → ✓/✗ → cause. That table is the deliverable of the loop.
When the draft contradicts your runbook, check the source method before calling it a miss: the model
reading `/mirrors` code is often right and the runbook paraphrase wrong — fix the runbook.

## 6. Fix developer experience, not steering

Allowed fixes after a miss: rename to the symptom word the admin uses; put the verdict on line 1;
add `--json`; make `--help` show a copy-pasteable example; add the one missing routing line; make
the error text name the next call; split a wall of output into sections the agent can quote.
Not allowed: a prompt rule per failed run, broad AGENTS.md rewrites, touching other topics' files.
Two or three loops is plenty — stop when the helper is used unprompted in ≥ 4 of 5 fresh questions,
or when the remaining misses are clearly not yours (model prose, a grounding-pass artefact).

## 7. Ship and re-ask live

[brain-publish](../brain-publish/SKILL.md) for the brain (exact SHA, channel proof); the mirror's
own publish path for the mirror. Then one `rc ask` **without** `--brain-ref` and confirm in its trace
`brain_resolved` is the published SHA and the helper still gets called. Done means live, not
"ready to publish". Before publishing: every `skills/**` description ≤ 150 chars (the publish lint
rejects longer ones, including other threads' files already on `main`), and on a shared checkout land
commits through a detached `/tmp` worktree + cherry-pick, never by rebasing other agents' WIP.

## Lessons from past loops

- Pickup is driven by **symptom words in the case-md/SKILL/docstring**, not by the script name; one
  routing line + a case runbook in the admin's own Dutch has carried 4/4 record-bearing questions on
  the first loop in three topics, with zero prompt rules.
- Knowledge-only questions (no record id) legitimately skip the helper; count them separately.
- A `*_to_md` section that already states the reason is not enough when the question is "why this
  child and not the sibling": a per-person sweep is a different question shape.
- A rule you only know from the KB is a paraphrase; the port of the real service (e.g. an export
  scope) is where the invisible traps surface.
- Shared checkouts commit shared files whole: your hunk may already be on `main` under another
  thread's commit — diff against `origin/main` before assuming it is missing.
- Per-topic lessons and open gaps live in the brain's `_internal/devloop-lessons.md` (backlog).
