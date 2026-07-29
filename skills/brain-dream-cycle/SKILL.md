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
   Weight evidence in this order: explicit feedback, sent-vs-proposed deltas, repeated run patterns,
   then journal/debug traces. Use `rc dev learning evidence` instead of private DB queries; it already ranks
   feedback by sharpest criticism and sent deltas by strongest human rewrite. `--plane
   feedback|deltas|triage` narrows it; `rc fleet runs --learning` finds the same candidates from the
   run side.

   Stop here if the corpus is empty or too weak. Report "no durable lesson" with the commands run
   rather than creating a speculative brain rule.

3. Read the sent-vs-proposed deltas as diffs, not JSON:
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

   Both use a fuzzy paragraph alignment with a **word-level** diff inside each pair (a line diff would
   paint every rewritten paragraph solid red/green and hide the actual edit), drop quoted reply
   history from the diff, and order deltas most-rewritten first.

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
