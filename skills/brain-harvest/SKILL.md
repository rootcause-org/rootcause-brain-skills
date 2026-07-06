---
name: brain-harvest
description: Run the full local harvest cycle for a rootcause project from a brain checkout using only the public rc CLI — the sent-history analog of brain-dream-cycle. Use when asked to onboard a mailbox's past replies, bootstrap a brain from historical sent mail, harvest/mine/synthesize a mailbox's answered-email corpus, or seed brain knowledge from a production export. Trigger a production harvest export, download the cleaned Markdown corpus locally, fan out per-topic coding-agent subagents to distil patterns (never raw mail), decide durable homes across brain files / persona / triage, run a privacy+contract lint before every commit, verify with a production run, publish, then delete the local corpus.
---

# brain-harvest - synthesize a brain from historical sent mail

Use this from inside a project or tenant brain checkout to bootstrap or grow a brain from a mailbox's
own answered-email history. It is the harvest analog of [`brain-dream-cycle`](../brain-dream-cycle/SKILL.md):
where dream-cycle mines a handful of recent runs, harvest sweeps the full sent corpus once, clusters it,
and distils durable patterns. Synthesis runs **locally in your coding-agent session** (Claude Code /
Codex) so it uses your stronger-model subscription instead of a hosted tier — the corpus lands on the
laptop only for the session and is deleted at the end.

The workflow is public-CLI only: no RootCause private source, host shell, SSM, `db.py`, raw registry
SQL, or private operator scripts. If a needed read/write is not exposed through `rc`, finish with a
RootCause support request through [`brain-publish`](../brain-publish/SKILL.md).

**Privacy is the hard rule of this skill.** The corpus is real customer mail. Only *distilled patterns*
land in brain files — never raw thread text, credentials, patient data, addresses, or payment links.
Two mechanical checks (`scripts/brain_lint.py`, plus an explicit history-rewrite decision) back the
judgment call, and the operator reviews the diff before push.

## Required Context

Read when relevant:

- [docs/brain-model.md](../../docs/brain-model.md) for what belongs in the brain and its layout.
- [docs/rc-cli.md](../../docs/rc-cli.md) for public command scope, including the harvest/export commands.
- [docs/side-effects.md](../../docs/side-effects.md) before triggering a harvest, download, or `rc ask`.
- [docs/support-boundary.md](../../docs/support-boundary.md) when a needed surface is missing.
- [`brain-ask`](../brain-ask/SKILL.md) for production-loop verification.
- [`brain-publish`](../brain-publish/SKILL.md) for the final sync/support request.

Templates in [`templates/`](templates/) hold two archetype brain skeletons (product-support and
personal/mixed). Edit the matching skeleton in step 4 instead of inventing structure.

## Workflow

1. **Confirm scope and inventory existing configuration.** Do this before reading any corpus so
   project/tenant mistakes fail early, and so synthesis knows what grounding, persona, and triage
   already exist before proposing new homes — never infer "no grounding" from a local repo search alone:
   ```bash
   rc whoami
   rc mailbox ls
   git status --short --branch
   git pull --ff-only

   rc config hierarchy get -o json
   rc triage policy get -o json
   rc triage rules ls -o json
   rc db list -o json          # grounding databases already wired
   rc capabilities             # cataloged brain scripts / tools already available
   rc health                   # source mirrors already mounted (and their freshness)
   ```
   Preserve local work. In a tenant checkout, route tenant-specific distillations to the tenant brain or
   tenant settings unless they clearly apply to the shared project.

2. **Acquire the corpus.** Reuse a fresh export if one exists; otherwise trigger a harvest and wait:
   ```bash
   rc export ls -o json                 # has this mailbox been harvested already?
   rc mailbox harvest <mailbox-id> --max-threads 1000 --wait
   rc export download <export-id> --split .rootcause/exports/<export-id>/
   ```
   `rc mailbox harvest` triggers a **production** provider sweep of the mailbox's sent history into a
   cleaned Markdown corpus; `rc export download` marks the export consumed (starting server-side
   eviction) and lands raw mail on local disk. **Before writing anything else, verify the split dir is
   gitignored** — the default `.rootcause/exports/<id>/` sits under the wholesale-gitignored
   `.rootcause/`, but confirm it:
   ```bash
   git check-ignore .rootcause/exports/<export-id>/INDEX.md
   ```
   If that prints the path, it is ignored. If it prints nothing, stop and add the ignore rule before
   proceeding — raw mail must never be stageable.

3. **Synthesize by progressive disclosure — never load the whole corpus into one context.** Read only
   the index, cluster from metadata, then fan out subagents. This is the local analog of the hosted
   mining phases:
   - **Map** — read `.rootcause/exports/<id>/INDEX.md`; cluster topics from thread metadata (month,
     slug, subject). Do not read thread bodies yet.
   - **Fan out** — spawn one coding-agent subagent per topic cluster. Each reads **only its own
     threads** under `threads/` and returns distilled Markdown proposals (patterns, terminology,
     routing, escalation rules) — never raw mail.
   - **Induce taxonomy** — the orchestrator merges per-topic returns into a candidate brain tree.
   - **Reduce** — a per-topic pass tightens each proposal against the induced taxonomy.
   - **Critic (run on the FIRST draft, not after polishing)** — one review subagent checks the whole
     proposal set against the existing brain tree and the brain contract: no response-mechanics wording,
     no persona/voice, no channel instructions, and every claim traceable to corpus evidence. Running
     the critic early kills contract violations before they get polished into keepers.

4. **Decide the durable home** (same table as dream-cycle):

   | Distilled signal says | Write to |
   |---|---|
   | Product fact, routing, terminology, source-of-truth pointer, repeatable investigation/playbook | Brain files. |
   | Missing reusable script, action instructions, action selection rules | Brain files or `actions/<id>/`. |
   | Voice, language, signature, formality, wording preference, "sound more like us" | Persona settings via `rc config hierarchy`. |
   | Which inbound mail should become a draft, broad draft/no-draft guidance | Triage policy via `rc triage policy`. |
   | Deterministic always-skip or always-process rule based on sender/subject/header | Triage hard rule via `rc triage rules`. |
   | Missing public surface, channel promotion, tenant publish, action wiring, cache divergence | `brain-publish` support request. |

   Onboarding-shaped outputs land where the mechanical seeder already points: `notes/onboarding-inbox.md`-style
   survey facts, `notes/mailbox-patterns.md`-style distilled patterns, and case/terminology files. Start
   from the matching [archetype template](templates/) — product-support or personal/mixed — and **edit**
   its skeleton rather than inventing new top-level structure. Search first:
   ```bash
   rg -n "<customer phrase>|<internal term>|<policy name>" AGENTS.md skills notes playbooks actions terminology.md 2>/dev/null
   ```

5. **Privacy gate (HARD) — before every commit.** Distilled patterns only. Never let raw thread text,
   credentials, patient data, addresses, or payment links reach a brain file. Two mechanical checks back
   the judgment call:
   ```bash
   git add <brain files you wrote>
   uv run --no-project python "$SKILL/scripts/brain_lint.py"          # scans staged *.md; exits non-zero on any secret/raw-thread/payment-link finding
   uv run --no-project python "$SKILL/scripts/brain_lint.py" --all --strict   # sweep the whole tree; address/persona warnings fatal too
   ```
   - (a) `brain_lint.py` must pass before every commit. Secrets, raw-thread shape, and payment
     links/IBANs hard-block (exit 1); the coarse address and persona-wording heuristics are warnings you
     must still review (fatal under `--strict`).
   - (b) **History-rewrite decision must be explicit.** If a legacy onboarding path ever committed raw
     mail (e.g. a `past-replies.md`), deleting the file leaves it in git history. Do **not** silently
     `git rm` and move on — escalate to the operator with the exact path and commit, because scrubbing
     history is a deliberate, coordinated rewrite. (Precedent: a `past-replies.md` was deleted
     post-onboarding for exactly this, and a real credential had been committed.)

   The operator reviews the full diff before push — that human gate is not optional.

6. **Verify with the cheapest check that proves the synthesis.** Push a dev ref and replay a real
   historical case through the production loop:
   ```bash
   git push origin dev/<branch>
   rc ask "<a representative historical case from the corpus>" --brain-ref dev/<branch>
   rc run <new-run-id> --debug
   ```
   Read the debug markdown index first; open JSONL only for exact commands, reasoning, or reply payload.
   A good result answers the historical case the way the human once did, grounded in the new brain files.

7. **Publish, then clean up.**
   - Brain files changed: commit, push, then use [`brain-publish`](../brain-publish/SKILL.md).
   - Settings changed only: record the exact `rc` commands and the verification run id.
   - Public surface missing: use the `brain-publish` support-request template with evidence.
   - **Delete the local corpus** — raw mail does not persist on the laptop beyond the session:
     ```bash
     rm -rf .rootcause/exports/<export-id>/
     ```
     Server-side eviction (started when you downloaded) handles the stored copy.

## Discipline

- Never load the full corpus into one context; always cluster from the index, then fan out per-topic
  subagents that read only their threads.
- Never commit raw thread text, credentials, patient data, addresses, or payment links — distilled
  patterns only. `brain_lint.py` gates every commit; a HARD finding blocks the commit, full stop.
- Inventory persona/triage/grounding via `rc` before proposing homes. Do not conclude "no grounding
  exists" from a local repo search — the first bollen-klara build got this wrong.
- Run the critic/contract review on the FIRST draft, before polishing.
- Make the history-rewrite decision explicit and escalate it; deleting a file does not scrub git history.
- Edit an archetype template's skeleton; do not invent new top-level brain structure per harvest.
- Do not hide draft/no-draft policy in persona; use triage for it and actions for confirmed mutations.
- Delete the local export dir at the end of the session.
- Do not use private rootcause `db.py`, raw production SQL, host scripts, or support-only credentials.
