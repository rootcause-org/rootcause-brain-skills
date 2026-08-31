---
name: brain-dream-cycle
description: Run the full local dreamcycle / dream-cycle best-practice flow for a rootcause project from a brain checkout using only the public rc CLI. Use when asked to learn from recent runs, feedback, sent-edit deltas, recurring patterns, journal signal, bad scores, or recurring triage mistakes; progressively inspect evidence with rc, decide whether lessons belong in brain files, persona settings, triage policy, or triage rules, verify with production runs, and publish/sync the result.
---

# brain-dream-cycle - learn from recent runs

Use this from inside a project or tenant brain checkout. Run the whole loop: gather evidence, drill
only where justified, choose the right durable home, make the smallest change, verify with production,
then publish or request the missing surface.

The workflow is public-CLI only: no RootCause private source, host shell, SSM, `db.py`, raw registry
SQL, or private operator scripts. If a needed read/write is not exposed through `rc`, finish with a
RootCause support request through `brain-publish`.

## Required Context

Read when relevant:

- [docs/brain-model.md](../../docs/brain-model.md) for what belongs in the brain.
- [docs/rc-cli.md](../../docs/rc-cli.md) for public command scope.
- [docs/support-boundary.md](../../docs/support-boundary.md) when a needed surface is missing.
- [docs/side-effects.md](../../docs/side-effects.md) before triggering `rc ask` or actions.
- [`rc-debug`](../rc-debug/SKILL.md) when drilling one run.
- [`brain-ask`](../brain-ask/SKILL.md) for production-loop verification.
- [`brain-publish`](../brain-publish/SKILL.md) for final sync/support request.

## Workflow

1. Confirm scope and local state. Do this before reading evidence so project/tenant mistakes fail
   early. The two skill paths are used throughout — this cycle's own scripts and, in step 8,
   `local-brain-work`'s test harness:
   ```bash
   DREAM_SKILL=<absolute path to skills/brain-dream-cycle>
   LOCAL_SKILL="$(cd "$DREAM_SKILL/../local-brain-work" && pwd)"
   rc auth status
   git status --short --branch
   git pull --ff-only
   ```
   Preserve local work. In a tenant checkout, keep tenant-specific lessons in the tenant brain or
   tenant settings unless they clearly apply to the shared project.

2. Pull broad evidence first:
   ```bash
   rc dev learning evidence --limit 50 -o json
   rc dev learning evidence --plane feedback --limit 50 -o json
   rc fleet runs --kind email --days 14 --learning
   rc fleet patterns --days 30
   ```
   Weight evidence in this order: explicit feedback, sent-vs-proposed evidence, repeated run patterns,
   then journal/debug traces. Use `rc dev learning evidence` instead of private DB queries; it ranks
   feedback by sharpest criticism and live deltas by strongest human rewrite. Shadow evidence is a
   recent, verdict-neutral sample instead. `--plane feedback|deltas|shadow|triage` narrows it; `rc
   fleet runs --learning` finds the same candidates from the run side.

   Stop here if the corpus is empty or too weak. Report "no durable lesson" with the commands run
   rather than creating a speculative brain rule.

### Shadow mode

Detect shadow per evidence row only from the wire field `shadow: true`. Never infer it from low
similarity, a large diff, suppressed-looking prose, or the verdict. A mixed payload is normal: the
report partitions rows and keeps each live row in the existing edit frame.

Pull an unbiased readiness sample first, then narrow to misses only after reading its distribution:

```bash
rc dev learning evidence --plane shadow --limit 100 -o json
rc dev learning evidence --plane shadow --verdict divergent_facts,missed_content \
  --include-bodies --limit 100 -o json
rc dev learning evidence --plane shadow --include-bodies --limit 100 -o json |
  uv run --no-project python "$DREAM_SKILL/scripts/sent_delta_report.py" --from-json -
uv run --no-project python "$DREAM_SKILL/scripts/sent_delta_report.py" --shadow --limit 100
```

The command still writes both audiences: verdict-first `.md` for the agent and `.html` for the human.

Read in progressive-disclosure order: **verdict → recurring themes → one representative run**.
`served_score` and the body comparison support the verdict; word overlap does not define it. Treat
`equivalent` and `same_outcome_details_differ` as positive blind evidence—the human never saw the
proposal, so a longer answer, different greeting, or different wording is not a correction. Readiness
is `(equivalent + same_outcome_details_differ) / answerable shadow rows`: show both numerator and
denominator, exclude `not_answerable` and unjudged rows, and report the served-score distribution
beside it. Graduation is a human decision, never an automatic threshold.

A lesson requires customer-impacting `divergent_facts` or `missed_content`, a representative debug
drill, and recurrence or one high-impact/high-confidence failure. Positive rows validate current
behavior; do not mine their prose into persona guidance. Route the evidenced cause, not the textual
delta. Fix at the highest level that generalizes; tenant is the easiest level to oversteer.

| Level / bucket | Route here when |
|---|---|
| RootCause host (always) | Product-agnostic system-prompt or loop behavior every project should have. |
| Shared grounding mirror (when present) | Facts/helpers reused by variant projects such as `-support` / `-staff` siblings. |
| Project brain (always) | Shared product facts, playbooks, terminology, or investigation rules. |
| Tenant brain/overlay (always) | A policy, fact, or term is truly tenant-specific. |
| Settings (always) | Persona owns voice/language/signature; triage owns process/skip policy and rules. |
| Grounding-data gap (non-brain) | The required fact was unavailable from DB, KB, website, or mirrors. |
| Action execution/wiring | The correct outcome needs a mutation or reviewer operation the workflow cannot represent. |
| Human-only knowledge (non-brain) | A phone call, private decision, or other out-of-band fact decided the answer. |

Host convention is **as-if-done**: `reply.actions` proposals and `👀` reviewer to-do lines are performed
by the reviewer before sending. Judge them as executed. If the human performs the same mutation and our
draft describes it as done with a backing proposed action or `👀` line, the shadow outcome is
`equivalent`. A `proposed` action status in a shadow trace is expected—shadow suppresses the draft, so
nothing executes—and is not evidence of a false claim. The real miss is asking the **customer** to
confirm or authorize instead of proposing the action or giving the reviewer a `👀` task.

Separate content quality from execution coverage. Keep raw bodies local and temporary. An unpaired
shadow run usually means the human has not answered yet; check the thread before assigning any quality
meaning.

Classification discipline:

- **Rubric first, traces second.** Before classifying a delta, read the draft conventions the run was
  actually given (`rc run debug` lists the prompt sections): proposed actions and `👀` reviewer to-do
  lines count as executed, link and marker policies bind. A worker who reads only traces calls
  convention-correct drafts "false claims".
- **The human's send is strong evidence, not ground truth.** Check the trace's grounded records too;
  when ours was right, say so — the verdict is about the customer outcome, not about matching the human.
- **Splitting classification across workers:** fix the taxonomy and verdict rubric in the brief up
  front, canonicalize theme slugs when merging, and reconcile with a mechanics trace of the host/brain
  before presenting themes.

3. Read live sent-vs-proposed deltas as edits, not JSON. Shadow rows use the verdict-first frame above;
   use `--shadow` or the piped `--plane shadow` command there so sampling stays verdict-neutral:
   ```bash
   uv run --no-project python "$DREAM_SKILL/scripts/sent_delta_report.py" --limit 20
   uv run --no-project python "$DREAM_SKILL/scripts/sent_delta_report.py" --limit 20 \
     --annotations .rootcause/dream/notes.json --conclusion .rootcause/dream/conclusion.md
   ```
   One `rc dev learning evidence --plane deltas --include-bodies` call, two files under the
   gitignored `.rootcause/dream/`, one per audience:

   - **`.md` — read this one.** Two grouping axes, then the deltas. **Delta categories** first — the
     server's own capture-time classification (`factual | tone | policy | omission | addition |
     other`), assigned from the bodies and stable across projects. Then the **signal index** (dropped
     date, dropped link, unfilled placeholder, dropped confirmation question, rewritten sign-off,
     length shift) as the secondary axis: regex-level markers, never a judgement — confirm on the body
     before writing anything durable. Then each delta as `[-removed-]`/`{+added+}` with its run id and
     trace link, unchanged paragraphs omitted. One delta is an anecdote; three sharing a category or
     marker is a pattern.
   - **`.html` — hand this to the human.** Same alignment, rendered side-by-side/inline with
     word-level highlighting, a `polish → replaced` verdict and the category per delta. Do not read it
     yourself.

   Live rows use a fuzzy paragraph alignment with a **word-level** diff inside each pair (a line diff would
   paint every rewritten paragraph solid red/green and hide the actual edit), drop quoted reply
   history from the diff, and order deltas most-rewritten first. Shadow rows are grouped by verdict and
   ordered by verdict severity then recency; their diff is secondary and uses neutral “only in ours” /
   “only in human answer” labels.

   **Two similarity numbers, never the same thing.** The report's `polish → replaced` verdict and its
   ordering come from the script's own word-level metric over display text. The payload's
   `server similarity` is the host's character-level edit distance over its own normalized text; it is
   printed with that label in the markdown and nowhere else. Do not average or substitute them.

   **Bodies live 14 days.** Retention scrubs message bodies at the host's email TTL, so a delta older
   than that has no wording left to diff. Those rows still arrive, flagged `bodies_scrubbed`, and land
   in an **Aged out (description only)** section carrying the server's category + description — good
   as corroboration for a pattern, never as the sole evidence for a durable edit. Run the cycle
   reasonably close to the sends if you want the wording.

   Write the reasoning back in so evidence and conclusion ship together: `--annotations`
   (`{"<delta-id>": "why this matters"}`) per delta and `--conclusion` for the overall call. Both land
   in both files. Cite delta ids and run ids in the final report.

   Use `--from-json -` when the evidence JSON is already in hand. Both files embed raw customer mail:
   never commit them, never paste bodies into brain files, delete when the cycle is done.

4. Drill progressively, only for evidence that can justify an edit:
   ```bash
   rc run debug <run-id>
   rc run brain-diff <run-id> -o json
   rc run thread <thread-or-session-id>
   ```
   Read the debug markdown index first. Open JSONL only for exact commands, stdout/stderr, reasoning,
   reply payloads, or journal lines. Prefer one high-signal run over five low-signal dumps.

   Progressive disclosure order:

   | Need | First command | Escalate only if needed |
   |---|---|---|
   | Bad score/comment or sent edit context | `rc dev learning evidence -o json` | `rc run debug <id>` |
   | Fleet-level recurring failure | `rc fleet runs`, `rc fleet patterns` | `rc run debug <id>` for one representative |
   | Conversation wording / sender context | `rc run thread <id>` | `rc run trace <id> -o json` |
   | Whether a previous brain edit helped | `rc run brain-diff <id> -o json` | compare with current brain files |

5. Decide the durable home:

   | Evidence says | Write to |
   |---|---|
   | Product fact, routing, terminology, source-of-truth pointer, repeatable investigation/playbook | Brain files. |
   | Missing reusable script, action instructions, action selection rules | Brain files or `actions/<id>/`. |
   | Voice, language, signature, formality, wording preference, “sound more like us” | Persona settings via `rc project settings behavior`. |
   | Which inbound mail should become a draft, broad draft/no-draft guidance | Triage policy via `rc project triage policy`. |
   | Deterministic draft blacklist/whitelist based on sender/subject/header | Triage hard rule via `rc project triage rules` (`skip` / `force_process`). |
   | Spam blacklist/whitelist by sender | Spam settings via `rc project senders` (`block` / `allow`). |
   | Shared project channel promotion | `brain-publish` exact-SHA public `rc` flow. |
   | Missing public surface, tenant publish, action wiring, cache divergence | `brain-publish` support request. |

   Avoid raw email quotes, one-off customer facts, copied private data, and generic RootCause behavior
   that belongs in product docs rather than the project brain.

   Never encode a deterministic blacklist/whitelist in `AGENTS.md`, `triage.md`, a brain skill, or a
   brain test. Settings are the source of truth; keeping the same selector in the brain creates a
   second, weaker rule that can diverge from the UI.

6. Inspect current settings before changing them:
   ```bash
   rc project settings behavior get -o json
   rc project triage policy get -o json
   rc project triage rules ls -o json
   ```

   Then apply settings changes only when the lesson is not a brain file:
   ```bash
   rc project settings behavior set persona.tone="..." persona.guidance="..."
   rc project tenant settings get <slug> -o json
   rc project tenant settings set <slug> persona.guidance="..."
   rc project mailbox settings get <mailbox-id> -o json
   rc project mailbox settings set <mailbox-id> persona.guidance="..."

   rc project triage policy set "Draft customer support questions; ignore vendor newsletters and automated alerts."
   rc --scope project project triage rules add effect=skip match_kind=sender_address pattern="alerts@example.com" reason="automated alerts"
   rc --scope project project triage rules add effect=force_process match_kind=sender_address pattern="vip@example.com" reason="VIP support mailbox"
   rc --scope project project senders block "spam.example.com" --reason "known spam sender"
   rc --scope project project senders allow "partner.example.com" --reason "trusted sender"
   ```
   Keep persona and triage concise. If guidance starts becoming product knowledge or a runbook, put it
   in the brain instead. Use `effect=skip` for deterministic no-draft mail and `effect=force_process`
   for deterministic draft-worthy mail. If a temporary rule is created for verification, delete it with
   `rc project triage rules rm <id>` before finishing.

7. Apply brain changes narrowly. Search first; edit the smallest existing home:
   ```bash
   rg -n "<customer phrase>|<internal term>|<policy name>" AGENTS.md skills notes playbooks actions 2>/dev/null
   ```
   Prefer editing existing `AGENTS.md`, `terminology.md`,
   `skills/*/SKILL.md`, `notes/`, scripts, or bounded `actions/<id>/` files over creating new top-level
   structure.

8. Verify with the cheapest check that proves the change:
   ```bash
   uv run "$LOCAL_SKILL/scripts/brain_test.py"
   git diff --check
   git push origin dev/<branch>
   rc ask "<customer-style case that previously failed>" --brain-ref dev/<branch>
   rc run debug <new-run-id>
   ```
   For settings-only changes, use a fresh `rc ask` against the live scope and inspect the run. For
   triage rules, prefer a prompt or harmless disabled create/delete check that proves the API contract
   without touching unrelated mail.

9. Publish:
   - Brain files changed: commit, push, then use `brain-publish`.
   - Settings changed only: record the exact `rc` commands and verification run id.
   - Public surface missing: use `brain-publish` support-request template with evidence and desired
     product outcome.
   - Mixed brain + settings: publish the brain first, then include settings commands and verification in
     the final note.

## Discipline

- Do not hand-edit `journal/`.
- Do not promote a single anecdote unless it is high-impact explicit human feedback.
- Do not hide write policy in persona; use triage for draft/no-draft decisions and actions for
  confirmed mutations.
- Do not use `rc dev console database` against RootCause internals for this workflow. Project
  data-plane reads are fine only when verifying a brain script or fact.
- Do not use private rootcause `db.py`, raw production SQL, host scripts, or support-only credentials.
