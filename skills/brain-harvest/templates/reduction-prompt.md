# Reduction prompt (pipeline step 6)

Per-topic reduction, run **after** the critic (step 5) and **before** tracked edits (step 7). It turns
the first-draft proposal set — as judged by `critic/critic.md` — into tight, final deltas against one
induced taxonomy (step 4). Runs locally; may read scratch and opaque IDs; emits nothing tracked yet.

Era bands and other numeric knobs are **tunable defaults from the prepare config**, not constants.

---

## Prompt

You reduce the harvested proposals topic by topic into final brain deltas. Read the induced taxonomy,
every `drafts/<cluster>.md`, the critic report `critic/critic.md`, and the existing brain. For each
topic:

1. **Apply the critic.** Drop anything the critic rejected that cannot be cleanly fixed. Fix what it
   flagged as fixable (misfiled home, missing era tag, scope violation → pending recommendation). Do not
   revive a rejected skip/`force_process` proposal.

2. **Resolve or surface contradictions.** Where evidence reconciles, state the single resolved rule and
   note how the conflict was settled. Where it does not, **surface** the contradiction for the review
   brief rather than inventing a resolution.

3. **Apply era supersessions (§5a).** On conflict, **prefer recent evidence**; when newer evidence
   overrides a `stale-era` rule, keep the recent rule and **record the supersession** (what was replaced,
   by which era) so the review brief can show it. Persona/tone synthesis weights the trailing era.

4. **Tighten into deltas against the taxonomy.** Collapse per-cluster restatements into one delta per
   fact/rule, phrased as add / revise / retire against the specific existing brain file or the persona /
   triage surface. No duplicate rules across topics; no from-scratch rewrites.

5. **Keep the home split.** Brain facts stay brain prose; persona signals go to the persona surface;
   triage decisions to triage — never mixed. Triage/hard-rule widening beyond mailbox scope stays a
   **pending recommendation** unless explicit scope authority exists (§6).

Privacy is unchanged: distilled deltas only — no raw quotes, names, addresses, identifiers,
counterparties, links, filenames, or opaque IDs in anything destined for tracked files.

---

## Output — `{{SCRATCH_ROOT}}/critic/reduced.md`

Final deltas grouped by home and topic, plus the two carry-forwards the review brief needs:

```markdown
# Reduced deltas

## Brain facts        — per topic: add|revise|retire <file>: <delta> · era · [superseded: <old era>]
## Persona            — voice/formality/signature/language deltas (mailbox scope)
## Triage             — draft|escalate rules; skip/no-draft (§5, with occurrence count)
## Pending recommendations — scope-blocked triage/hard-rule widenings (§6)

## Supersessions      — <topic>: recent rule replaced <stale-era rule>
## Unresolved contradictions — <topic>: <both positions> → surface in review brief
```

Step 7 applies these deltas to the tracked working tree and the settings surfaces.
