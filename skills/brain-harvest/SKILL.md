---
name: brain-harvest
description: Run the full local harvest cycle for a rootcause project from a brain checkout using only the public rc CLI — the sent-history analog of brain-dream-cycle. Use when asked to onboard a mailbox's past replies, bootstrap a brain from historical sent mail, harvest/mine/synthesize a mailbox's answered-email corpus, or seed brain knowledge from a production export. Trigger a production harvest export, download the cleaned Markdown corpus, prepare a deterministic opaque-ID manifest with coverage ledger, fan out per-topic subagents to distil patterns (never raw mail), critic the first draft, reduce, decide durable homes across brain files / persona / triage, run a privacy+contract lint, evaluate against held-out threads, gate on operator diff approval, publish, then delete the local corpus.
---

# brain-harvest - synthesize a brain from historical sent mail

Use this from inside a project or tenant brain checkout to bootstrap or grow a brain from a mailbox's
own answered-email history. It is the harvest analog of [`brain-dream-cycle`](../brain-dream-cycle/SKILL.md):
where dream-cycle mines a handful of recent runs, harvest sweeps the full sent corpus once, partitions it
deterministically, and distils durable patterns. Synthesis runs **locally in your coding-agent session**
(Claude Code / Codex) so it uses your stronger-model subscription instead of a hosted tier — the corpus
lands on the laptop only for the session and is deleted **after** the operator approves the diff.

The workflow is public-CLI only: no RootCause private source, host shell, SSM, `db.py`, raw registry
SQL, or private operator scripts. If a needed read/write is not exposed through `rc`, finish with a
RootCause support request through [`brain-publish`](../brain-publish/SKILL.md).

**Privacy is the hard rule of this skill.** The corpus is real customer mail. Only *distilled patterns*
land in tracked brain files — never raw thread text, credentials, patient data, addresses, payment
links, correspondent names, or the opaque IDs that index the scratch corpus. The corpus, manifests,
cluster drafts, critic notes, review brief, and evidence filenames are all sensitive until reduced;
they live under one gitignored scratch root and are deleted only after the human gate.

## Required Context

Read when relevant:

- [docs/brain-model.md](../../docs/brain-model.md) for what belongs in the brain and its layout.
- [docs/rc-cli.md](../../docs/rc-cli.md) for public command scope, including the harvest/export commands.
- [docs/side-effects.md](../../docs/side-effects.md) before triggering a harvest, download, or `rc ask`.
- [docs/support-boundary.md](../../docs/support-boundary.md) when a needed surface is missing.
- [docs/specs/brain-harvest-long-horizon-v2.md](../../docs/specs/brain-harvest-long-horizon-v2.md) — the
  design this pipeline implements; consult §3/§5/§5a/§6/§7/§10 when a step's rationale is unclear.
- [`brain-ask`](../brain-ask/SKILL.md) for production-loop verification.
- [`brain-publish`](../brain-publish/SKILL.md) for the final sync/publish/support request.

Templates in [`templates/`](templates/) hold the pipeline prompts (cluster / critic / reduction), the
operator report formats (review brief, harvest record), and two archetype brain skeletons
(product-support and personal/mixed). Read [`templates/README.md`](templates/README.md) first; edit the
matching archetype skeleton in step 7 instead of inventing structure.

## Scratch root

Preparation creates one gitignored root that holds **everything sensitive** for the run:

```
.rootcause/harvest/<tag>/     # <tag> = export id or an operator-chosen run tag
  corpus/                     # raw downloaded corpus file(s)
  threads/H000001.md          # one file per thread, named by opaque ID only
  manifest.jsonl              # per-thread machine facts, first key always "id"
  clusters.json               # cluster reading plans (sample_ids + deep_read_ids)
  ledger.json                 # machine-verified coverage ledger
  holdout.json                # reserved held-out eval threads
  drafts/                     # per-cluster <cluster>.md + <cluster>.report.json
  critic/                     # critic report + reduced deltas
  brief/                      # review brief + holdout scorecard (local, ephemeral)
```

`.rootcause/` is wholesale-gitignored in a brain checkout, but the scripts still re-verify with
`git check-ignore` and refuse to write a stageable root. Raw subjects, filenames, and opaque IDs never
leave this root; only the reduced, opaque-ID-free result reaches the tracked diff. Set `SKILL` once so
the bundled scripts are easy to invoke:

```bash
SKILL=<absolute path to skills/brain-harvest>
LBW=<absolute path to skills/local-brain-work>   # brain_structure.py lives here
TAG=<export-id or run tag>
SCRATCH=.rootcause/harvest/$TAG
```

## Workflow (spec §8 — exact ordering)

The twelve steps below are the §8 pipeline. Steps marked ⚙ are script invocations with
machine-checkable output; keep prose judgement in the fan-out/critic/reduce steps minimal. State that
persists between steps is exactly the scratch root plus the tracked working diff, so **any step is
re-entrant** — the only irreversible transition is scratch deletion in step 12, which is why it happens
only after operator approval.

### 1. ⚙ Preflight and acquire

Inventory scope and existing configuration first so project/tenant mistakes fail early, and so
synthesis knows what grounding, persona, and triage already exist before proposing homes — never infer
"no grounding" from a local repo search alone:

```bash
rc auth status
rc project mailbox ls
git status --short --branch
git pull --ff-only

rc project settings behavior get -o json
rc project triage policy get -o json
rc project triage rules ls -o json
rc dev console database list -o json   # grounding databases already wired
rc dev console capabilities            # cataloged brain scripts / tools already available
rc fleet health                        # source mirrors already mounted (and their freshness)
```

In a tenant checkout, route tenant-specific distillations to the tenant brain or tenant settings unless
they clearly apply to the shared project. Preserve local work.

**Acquire the corpus.** Reuse a fresh export if one exists; otherwise branch by mailbox provider.

Hosted provider path (Gmail/Microsoft):

```bash
rc project corpus ls -o json                       # has this mailbox been harvested already?
rc project mailbox harvest <mailbox-id> --max-threads 1000
rc project corpus get <export-id>                  # poll until terminal
rc project corpus download <export-id> --out "$SCRATCH/corpus/corpus.md"
```

`rc project mailbox harvest` triggers a **production** provider sweep of the mailbox's sent history into
a cleaned Markdown corpus; `rc project corpus download` marks the export consumed (starting the
server-side eviction window, ~48h) and lands raw mail on local disk. Download with `--out` and let
`prepare_harvest.py` parse the raw bytes: it reads corpus **v1 and v2** itself, so `rc corpus download
--split` is a convenience, not a dependency. **This matters today:** the server emits v2, and released
`rc` hard-rejects v2 in its splitter, so `--out` + local parse is the working path. If you already have
a `--split` directory from an older export, point `prepare` at that directory instead.

IMAP guard rails: for IMAP mailboxes, treat hosted harvest as a shallow/smoke path only (the server may
cap rendered IMAP refs, currently 100, and warn that deep IMAP belongs in local tooling). Before any
deep/local IMAP run, prove **both** public surfaces exist:

```bash
rc project mailbox imap-env --help
test -f "$SKILL/scripts/local_imap_harvest.py"
```

If either is missing, do **not** reveal credentials, scrape private stores, or invent env-file handling
— stop with an implementation/ops gap (via `brain-publish`), or run only the capped hosted harvest as a
smoke test. When both exist:

```bash
rc project mailbox imap-env <mailbox-id> --out "$SCRATCH/imap.env"
git check-ignore "$SCRATCH/imap.env"
uv run "$SKILL/scripts/local_imap_harvest.py" --env "$SCRATCH/imap.env" --out "$SCRATCH/imap-export/"
```

The IMAP env file is secret material: never print it, commit it, or keep it after the session (cleanup
in step 12 removes it with the rest of scratch).

**Note — the IMAP exporter feeds the manual fallback, not `prepare` yet.** `local_imap_harvest.py`
writes a split directory (`INDEX.md` + `threads/YYYY-MM--slug--n.md`), not a v1/v2 corpus blob with
`harvest_format` front-matter, so `prepare_harvest.py` cannot parse it today. For a deep IMAP corpus,
use the [manual fallback path](#fallback-the-v1-manual-path-one-release-only) below (read `INDEX.md`,
cluster from metadata, fan out) until the exporter emits a v1/v2 blob. The hosted provider path above is
the one that flows through `prepare`.

Then run the preflight check — local git/gitignore/format checks plus best-effort `rc` environment
checks (it degrades to WARN when `rc` is absent):

```bash
uv run --no-project python "$SKILL/scripts/prepare_harvest.py" preflight --scratch "$SCRATCH"
```

It sniffs the corpus format under `$SCRATCH/corpus/`, so run it after the download. A `FAIL` (not inside
a git checkout, stageable scratch root, or an unsupported corpus format) must be fixed before proceeding.

### 2. ⚙ Deterministic prepare, verify

```bash
uv run --no-project python "$SKILL/scripts/prepare_harvest.py" prepare \
  --corpus "$SCRATCH/corpus/" --scratch "$SCRATCH"
uv run --no-project python "$SKILL/scripts/prepare_harvest.py" verify --scratch "$SCRATCH"
```

`prepare` parses the raw corpus into an opaque-ID manifest and writes `threads/`, `manifest.jsonl`,
`clusters.json`, `ledger.json`, and `holdout.json` atomically (re-running replaces them; it is
deterministic and idempotent over the same corpus bytes). It:

- assigns each thread an opaque `H000001` ID in stable date order — raw filenames stay internal;
- extracts metadata without LLM synthesis: date span, subject family, language, message/reply counts,
  direction, form source, attachments, the **prose-reply-present** flag (§5 evidence), era band (§5a),
  and safe risk markers;
- clusters **deliberately dumbly** (direction, form source, subject family, light content markers) with
  a mandatory `mixed` bucket — generic subjects like "contact", "order", "invoice" never determine a
  topic alone; clustering is a work-partitioning unit, not routing truth;
- emits a coverage ledger proving every thread is assigned exactly once (`assigned` / `holdout` /
  `excluded_noise`), with per-cluster stratified `sample_ids` (single-pass reading plan) and
  `deep_read_ids` (risk-flagged, always read);
- reserves a small stratified holdout (default 8, tunable 5–10) of threads with substantive inbound
  questions and real human answers, excluded from all synthesis reads (§10);
- reports the **risk-flag distribution**: if flagged share exceeds the cap (default 15%), the ledger
  marks `over_cap: true` with the per-marker breakdown so you prune marker rules **before** fan-out
  rather than silently reintroducing read-everything.

`verify` (also run automatically inside `prepare`) checks the ledger invariants and exits non-zero on
any violation. Inspect `ledger.json` → `risk`: if `over_cap` is true, lower/prune risk markers via a
`--config` JSON and re-run `prepare` before fanning out. Every numeric knob (sample cap, era bands,
holdout count, risk cap, prose-reply threshold) is a **tunable default**, overridable with
`--config <json>`, `--holdout N`, or `--seed N` — never a hard constant.

### 3. Bounded per-cluster drafts

Fan out one coding-agent subagent per cluster using
[`templates/cluster-agent-prompt.md`](templates/cluster-agent-prompt.md), substituting the `{{…}}`
slots from `clusters.json`. Each subagent reads **only** its assigned threads under `$SCRATCH/threads/`
— the stratified `sample_ids` in a **single pass** (no incremental batch rounds) **plus every**
`deep_read_ids` (all risk-flagged threads) — and the relevant tracked brain files, then returns deltas
against the existing brain (never a from-scratch rewrite). Each subagent must:

- separate brain-fact, persona, and triage candidates;
- tag every durable rule with the era of its supporting evidence, marking `stale-era` where all evidence
  sits outside the trailing era (§5a);
- obey the §5 skip-evidence gate (presence-without-prose-reply only; occurrence count stated);
- report saturation honesty (`still_yielding`), route-elsewhere items, discovered contradictions,
  current-source verification needs, and coverage counts;
- emit **no** raw quotes, names, addresses, identifiers, counterparties, links, raw filenames, or opaque
  IDs into the proposal prose — opaque IDs live only in the report JSON;
- self-lint the proposal before marking the cluster complete (load-bearing; names leaked into proposals
  in run 1):
  ```bash
  uv run --no-project python "$SKILL/scripts/brain_lint.py" --scratch "$SCRATCH/drafts/<cluster>.md"
  ```

Each cluster produces two files: `$SCRATCH/drafts/<cluster>.md` (the proposal) and
`$SCRATCH/drafts/<cluster>.report.json` (machine coverage report).

**Resume granularity is the cluster, not the batch.** A cluster's draft either exists complete (both
files present, self-lint clean) or the cluster reruns — there are no per-batch checkpoints. Adjacent
small clusters may be merged into one assignment; a large diverse cluster should be split into bounded
assignments and reduced once, rather than handed wholesale to a single agent. If a subagent honestly
reports `still_yielding: true` at the cap, issue **one** orchestrator-controlled follow-up assignment on
that cluster — do not let the agent silently read more.

Fold each report back into the ledger so coverage and route-elsewhere reassignments are recorded:

```bash
uv run --no-project python "$SKILL/scripts/prepare_harvest.py" ledger apply \
  --scratch "$SCRATCH" "$SCRATCH"/drafts/*.report.json
```

`ledger apply` re-verifies invariants and persists nothing if the merge would break them.

### 4. Induce one candidate taxonomy

Merge the per-cluster returns into a single candidate brain tree: one induced taxonomy of topics/homes
that the reduction step will tighten each proposal against. This is orchestrator work over the draft
proposals only — no new corpus reads.

### 5. Early critic on the untouched first-draft set

Run one critic subagent over the **untouched** first-draft proposal set — every `drafts/<cluster>.md`
plus its `.report.json` — using [`templates/critic-prompt.md`](templates/critic-prompt.md), **before any
reduction**. Reducing first would hide the raw cross-cluster picture the critic needs. The critic judges
and flags only (it never edits proposals): brain-contract home correctness, the §5 skip/`force_process`
evidence gate, §5a era flags, the §6 scope matrix on every settings change, cross-cluster contradictions,
and privacy leaks. It writes advisory notes to `$SCRATCH/critic/critic.md`.

(This resolves the v1 skill's self-contradiction: the critic runs on the first draft, **before**
reduction, not after polishing.)

### 6. Per-topic reduction

Run reduction per topic against the critic report using
[`templates/reduction-prompt.md`](templates/reduction-prompt.md). It turns the critic-judged first draft
into tight final deltas against the one induced taxonomy: drop or fix what the critic rejected, resolve
contradictions where evidence reconciles (and **surface** the rest for the review brief), apply era
supersessions (prefer recent evidence, record what was superseded), collapse per-cluster restatements
into one delta per fact/rule, and keep the brain/persona/triage home split clean. Output is
`$SCRATCH/critic/reduced.md`; nothing tracked is written yet.

### 7. Tracked edits plus narrow settings changes

Apply the reduced deltas to the tracked working tree and the settings surfaces. Start from the matching
[archetype template](templates/) — product-support or personal/mixed — and **edit** its skeleton rather
than inventing new top-level structure. Search before adding:

```bash
rg -n "<customer phrase>|<internal term>|<policy name>" AGENTS.md skills notes playbooks actions terminology.md 2>/dev/null
```

**Durable home** for each reduced signal:

| Distilled signal says | Write to |
|---|---|
| Product fact, routing, terminology, source-of-truth pointer, repeatable investigation/playbook | Brain files. |
| Missing reusable script, action instructions, action selection rules | Brain files or `actions/<id>/`. |
| Voice, language, signature, formality, wording preference, "sound more like us" | Persona settings via `rc project settings behavior`. |
| Which inbound mail should become a draft, broad draft/no-draft guidance | Triage policy via `rc project triage policy`. |
| Deterministic always-skip or always-process rule based on sender/subject/header | Triage hard rule via `rc project triage rules`. |
| Shared project channel promotion | `brain-publish` exact-SHA public `rc` flow. |
| Missing public surface, tenant publish, action wiring, cache divergence | `brain-publish` support request. |

Onboarding-shaped outputs land where the mechanical seeder points: `notes/onboarding-inbox.md`-style
survey facts and `notes/mailbox-patterns.md`-style distilled patterns, plus case/terminology files.

**Apply persona + triage at the narrowest writable scope (§6 scope matrix).** Re-read the current values
(step 1) immediately before mutating, and verify the resolved source afterward. Writable scope is not
uniform across surfaces:

| Signal | Narrowest writable target today | Rule |
|---|---|---|
| Persona | **mailbox** (also tenant, project) | Apply at the harvested mailbox. |
| Triage policy | **tenant or project only — no mailbox scope** | Mailbox-derived evidence necessarily widens; widen only with explicit scope authority, else emit a **pending recommendation**. |
| Hard rules | **tenant or project only — no mailbox scope** | Same widening rule; require deterministic evidence per §5. |
| Brain facts | tenant or project brain | Match the business scope of the fact. |

Because triage policy and hard rules have **no mailbox scope**, a rule learned from one mailbox
necessarily widens to tenant/project. Widen only with explicit scope authority; otherwise carry it as a
pending recommendation in the review brief, not a silent write. If the needed narrow scope is
unavailable, produce a pending recommendation / support gap rather than writing a broader setting.

```bash
# Voice the sent history reveals → persona (mailbox scope exists here). Keys: persona.tone,
# persona.signature, persona.language, persona.formality, persona.guidance (free-text catch-all).
rc project mailbox settings set <mailbox-id> persona.guidance="..." persona.tone="..."
rc project settings behavior set persona.signature="— The Support Team"   # project scope
rc project tenant settings set <slug> persona.guidance="..."              # tenant-specific voice

# Broad draft / no-draft judgement the corpus shows → triage policy (tenant/project only)
rc project triage policy set "Draft customer support + billing questions; skip vendor newsletters and automated alerts."

# Deterministic per-sender / per-subject rules the corpus proves → triage HARD rules (tenant/project only)
rc project triage rules add effect=force_process match_kind=sender_domain pattern="partner.com" reason="partner mailbox — always answered"
rc project triage rules add effect=skip match_kind=subject_contains pattern="unsubscribe" reason="presence-without-prose-reply, N occurrences"
```

**Skip-evidence rules (§5) — this deliberately narrows the old skill.** A sent-history corpus proves
only what the mailbox answered; **absence from the corpus proves nothing** (unanswered inbound mail is
never exported), so no skip rule may be inferred from a sender or subject merely being missing or rare.
The one legitimate negative signal is **presence-without-prose-reply**: mail that recurs in-corpus with
`prose_reply=false` across multiple threads (the `prose-reply-present` manifest flag). Therefore:

- persona, terminology, intake, routing, and historical handling are supported outputs;
- `effect=force_process` may be proposed only when repeated positive evidence is deterministic;
- `effect=skip`, sender blocks, and hard skip rules may be proposed **only** from
  presence-without-prose-reply evidence that is repeated, unambiguous, and machine-countable — frequency
  of a subject/domain alone is not evidence of actionability;
- every skip proposal is surfaced **individually** in the review brief with its occurrence count, never
  applied silently;
- absence-based skip inference stays prohibited (it needs a paired inbound/no-reply export that does not
  exist — out of scope for v2).

This narrows the v1 guidance that routed "mail you never answer" to `effect=skip` freely. A temporary
rule created just to verify the contract must be removed with `rc project triage rules rm <id>` before
finishing.

### 8. ⚙ Lint — scratch drafts and staged brain

Two lint passes back the privacy judgement:

```bash
# scratch drafts: opaque IDs/raw filenames are expected there, so those classes are suppressed,
# but secrets/raw-thread/payment/identifier/name classes still apply (names downgrade HARD→SOFT).
uv run --no-project python "$SKILL/scripts/brain_lint.py" --scratch "$SCRATCH/drafts/"

# tracked brain text you wrote: nothing opaque may reach a tracked file.
git add <brain files you wrote>
uv run --no-project python "$SKILL/scripts/brain_lint.py"                 # staged pre-commit gate
uv run --no-project python "$SKILL/scripts/brain_lint.py" --all --strict  # whole-tree, address/persona warnings fatal too
```

Secrets, raw-thread shape, payment links/IBANs, contact details, order/invoice/tracking/account
identifiers, opaque-ID/raw-filename leakage into tracked files, and known `rc` command roots hard-block
(exit 1); coarse address/persona-wording heuristics are warnings, fatal under `--strict`. A HARD finding
blocks the commit, full stop. (The kit checkout is exempt from the `rc`-command rule because its
authenticated local-development skills intentionally document the CLI; a brain checkout is not.)

**History-rewrite decision must be explicit.** If a legacy onboarding path ever committed raw mail (e.g.
a `past-replies.md`), deleting the file leaves it in git history. Do **not** silently `git rm` and move
on — escalate to the operator with the exact path and commit, because scrubbing history is a deliberate,
coordinated rewrite. (Precedent: a `past-replies.md` was deleted post-onboarding for exactly this, and a
real credential had been committed.)

### 9. Independent staged-diff review and fixes

Review the full staged diff independently (a fresh reviewer subagent is ideal): confirm every claim is a
distilled pattern traceable to corpus evidence, homes are correct, no raw data or opaque IDs slipped in,
and settings changes respect the §6 scope matrix. Fix findings against the still-present corpus before
the gate.

### 10. ⚙ Review brief, sanitized replay, and held-out evaluation

Generate the operator brief and prove the output before the gate — the corpus is still present, so this
is the moment to measure quality:

- Write the review brief per [`templates/review-brief.md`](templates/review-brief.md) into
  `$SCRATCH/brief/` — **local, ignored, ephemeral**. It may reference opaque IDs and short evidence
  context so the operator can check evidence behind every rule. It carries the coverage summary, every
  settings change with scope, every skip proposal with its §5 occurrence counts, notable durable rules
  with evidence strength and era flags, contradictions and resolutions, the holdout scorecard, and run
  cost / wall clock.
- **Held-out evaluation:** commit the staged edits to a local `dev/<branch>` (a WIP commit is fine —
  the mandatory gate guards the push to `main`/publish, not dev refs), push it, and replay each
  reserved holdout question (from `holdout.json`) against that dev ref; then have a comparison agent
  score each answer against the historical human answer on factual agreement, routing, and tone, and
  write the scorecard into the brief:
  ```bash
  git checkout -b dev/<branch> && git commit -m "wip: harvest draft"
  git push origin dev/<branch>
  rc ask "<held-out inbound question>" --brain-ref dev/<branch>
  rc run debug <run-id>
  ```
- **One representative production replay stays required:** pick one new route, replay it on the pushed
  dev ref, and record the run id, status, cost, trace URL, resolved brain SHA, and brain diff.
- Run the structural validator (skipping lint, already run in step 8):
  ```bash
  uv run --no-project python "$LBW/scripts/brain_structure.py" --skip lint
  ```
  It checks relative link/route targets resolve, skill frontmatter is valid, routed case files are
  reachable from the router, and no raw-harvest path is tracked now or in history.

### 11. Mandatory operator diff approval

Pause for the single human diff-approval gate **with the local evidence brief still present**. The
operator consults `$SCRATCH/brief/review-brief.md` and, via opaque IDs, the local corpus, then approves
each settings change and skip proposal. This gate is not optional and is never a rubber stamp — which is
exactly why cleanup happens **after** it, not before (reversed from the v1 skill): deleting the corpus
pre-review would leave the gate with no evidence to check, and re-fetching after eviction is a
production operation.

### 12. ⚙ Cleanup, then publish

Only **after** approval, delete all sensitive scratch and verify it is gone, then publish:

```bash
uv run --no-project python "$SKILL/scripts/prepare_harvest.py" cleanup --scratch "$SCRATCH" --yes
uv run --no-project python "$LBW/scripts/brain_structure.py" --expect-clean
```

`cleanup` removes the whole scratch root (corpus, manifests, proposals, critic notes, brief, IMAP env)
and refuses without `--yes`; `brain_structure.py --expect-clean` confirms no `.rootcause/harvest/`
scratch root remains. Then:

- commit the reduced brain edits and the committed **harvest record** per
  [`templates/harvest-record.md`](templates/harvest-record.md) (counts, dates, holdout scores, kit
  version only — no opaque IDs or raw data; suggested home `notes/harvest-records/`);
- run the full [`brain-publish`](../brain-publish/SKILL.md) flow: `brain-git-sync` precondition, dev-ref
  replay/debug, server sync, channel promote where applicable, and exact-SHA verification;
- settings-only changes: record the exact `rc` commands and the verification run id;
- missing public surface: use the `brain-publish` support-request template with evidence.

The harvest record is the watermark for a future incremental `--since` re-harvest (spec §10).

## Fallback: the v1 manual path (one release only)

For a corpus `prepare_harvest.py` cannot parse (an unsupported `harvest_format`, or a shape neither v1
nor v2), fall back for one release to the pre-v2 manual flow: `rc project corpus download --split` into
a gitignored dir, read its `INDEX.md`, cluster from thread metadata, fan out per-topic subagents that
read only their own `threads/`, then critic-on-first-draft → reduce → durable homes → lint → verify →
publish → delete the export dir. The privacy, critic-before-reduce, and post-approval-cleanup rules
above all still apply; only the deterministic manifest/ledger/holdout machinery is unavailable. Prefer
`prepare_harvest.py` whenever it can parse the corpus — the manual path exists only to unblock an
unparseable export while v2 splitting/rescue lands in `rc` (see the migration note).

## Discipline

- Never load the full corpus into one context; cluster from `manifest.jsonl`/`clusters.json`, then fan
  out per-topic subagents that read only their assigned threads.
- Never commit raw thread text, credentials, patient data, addresses, payment links, correspondent
  names, or opaque IDs — distilled patterns only. `brain_lint.py` gates every commit; a HARD finding
  blocks it, full stop.
- Inventory persona/triage/grounding via `rc` before proposing homes. Do not conclude "no grounding
  exists" from a local repo search.
- Run the critic on the **first draft, before reduction** — not after polishing.
- Skip rules only from presence-without-prose-reply evidence with occurrence counts (§5); never from
  absence or raw frequency. Persona has mailbox scope; triage policy and hard rules do not — widen only
  with explicit scope authority, else a pending recommendation (§6).
- Edit an archetype template's skeleton; do not invent new top-level brain structure per harvest.
- Do not hide draft/no-draft policy in persona; use triage for it and actions for confirmed mutations.
- Make the history-rewrite decision explicit and escalate it; deleting a file does not scrub history.
- **Cleanup happens AFTER operator approval**, never before — the gate needs the evidence. Resume
  granularity is the cluster (scratch root + tracked diff); any step is re-entrant; there are no
  per-batch checkpoints.
- Do not use private rootcause `db.py`, raw production SQL, host scripts, or support-only credentials.
