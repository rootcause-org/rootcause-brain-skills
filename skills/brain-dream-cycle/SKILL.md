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
   early:
   ```bash
   rc whoami
   git status --short --branch
   git pull --ff-only
   ```
   Preserve local work. In a tenant checkout, keep tenant-specific lessons in the tenant brain or tenant
   settings unless they clearly apply to the shared project.

   If the checkout has local work, preserve it. In a tenant checkout, keep tenant-specific lessons in
   the tenant brain or tenant settings unless they clearly apply to the shared project.

2. Pull broad evidence first:
   ```bash
   rc dream evidence --limit 50 -o json
   rc fleet --kind email --days 14
   rc patterns --days 30
   ```
   Weight evidence in this order: explicit feedback, sent-vs-proposed deltas, repeated run patterns,
   then journal/debug traces. Use `rc dream evidence` instead of private DB queries; it already ranks
   feedback by sharpest criticism and sent deltas by strongest human rewrite.

   Stop here if the corpus is empty or too weak. Report "no durable lesson" with the commands run
   rather than creating a speculative brain rule.

3. Drill progressively, only for evidence that can justify an edit:
   ```bash
   rc run <run-id> --debug
   rc run <run-id> --brain-diff -o json
   rc thread <thread-or-session-id>
   ```
   Read the debug markdown index first. Open JSONL only for exact commands, stdout/stderr, reasoning,
   reply payloads, or journal lines. Prefer one high-signal run over five low-signal dumps.

   Progressive disclosure order:

   | Need | First command | Escalate only if needed |
   |---|---|---|
   | Bad score/comment or sent edit context | `rc dream evidence -o json` | `rc run <id> --debug` |
   | Fleet-level recurring failure | `rc fleet`, `rc patterns` | `rc run <id> --debug` for one representative |
   | Conversation wording / sender context | `rc thread <id>` | `rc run <id> --full -o json` |
   | Whether a previous brain edit helped | `rc run <id> --brain-diff -o json` | compare with current brain files |

4. Decide the durable home:

   | Evidence says | Write to |
   |---|---|
   | Product fact, routing, terminology, source-of-truth pointer, repeatable investigation/playbook | Brain files. |
   | Missing reusable script, action instructions, action selection rules | Brain files or `actions/<id>/`. |
   | Voice, language, signature, formality, wording preference, “sound more like us” | Persona settings via `rc config hierarchy`. |
   | Which inbound mail should become a draft, broad draft/no-draft guidance | Triage policy via `rc triage policy`. |
   | Deterministic always-skip or always-process rule based on sender/subject/header | Triage hard rule via `rc triage rules`. |
   | Missing public surface, channel promotion, tenant publish, action wiring, cache divergence | `brain-publish` support request. |

   Avoid raw email quotes, one-off customer facts, copied private data, and generic RootCause behavior
   that belongs in product docs rather than the project brain.

5. Inspect current settings before changing them:
   ```bash
   rc config hierarchy get -o json
   rc triage policy get -o json
   rc triage rules ls -o json
   ```

   Then apply settings changes only when the lesson is not a brain file:
   ```bash
   rc config hierarchy set persona.tone="..." persona.guidance="..."
   rc tenant settings get --tenant <slug> -o json
   rc tenant settings set --tenant <slug> persona.guidance="..."
   rc mailbox settings get <mailbox-id> -o json
   rc mailbox settings set <mailbox-id> persona.guidance="..."

   rc triage policy set "Draft customer support questions; ignore vendor newsletters and automated alerts."
   rc triage rules add effect=skip match_kind=subject_contains pattern="newsletter" reason="marketing mail"
   rc triage rules add effect=force_process match_kind=sender_email pattern="vip@example.com" reason="VIP support mailbox"
   ```
   Keep persona and triage concise. If guidance starts becoming product knowledge or a runbook, put it
   in the brain instead. Use `effect=skip` for deterministic no-draft mail and `effect=force_process`
   for deterministic draft-worthy mail. If a temporary rule is created for verification, delete it with
   `rc triage rules rm <id>` before finishing.

6. Apply brain changes narrowly. Search first; edit the smallest existing home:
   ```bash
   rg -n "<customer phrase>|<internal term>|<policy name>" AGENTS.md skills notes playbooks actions 2>/dev/null
   ```
   Prefer editing existing `AGENTS.md`, `terminology.md`,
   `skills/*/SKILL.md`, `notes/`, scripts, or bounded `actions/<id>/` files over creating new top-level
   structure.

7. Verify with the cheapest check that proves the change:
   ```bash
   uv run "$SKILL/scripts/brain_test.py"
   git diff --check
   git push origin dev/<branch>
   rc ask "<customer-style case that previously failed>" --brain-ref dev/<branch>
   rc run <new-run-id> --debug
   ```
   For settings-only changes, use a fresh `rc ask` against the live scope and inspect the run. For
   triage rules, prefer a prompt or harmless disabled create/delete check that proves the API contract
   without touching unrelated mail.

8. Publish:
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
- Do not use `rc db` against RootCause internals for this workflow. Project data-plane `rc db` reads are
  fine only when verifying a brain script or fact.
- Do not use private rootcause `db.py`, raw production SQL, host scripts, or support-only credentials.
