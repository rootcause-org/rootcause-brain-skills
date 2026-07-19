# Critic prompt (pipeline step 5)

The **early critic**. It runs on the **untouched first-draft proposal set** — every
`drafts/<cluster>.md` + `drafts/<cluster>.report.json` — **before any reduction** (step 6). Reducing
first would hide the raw cross-cluster picture the critic needs. Runs locally; may read scratch and
opaque IDs. Output is advisory notes for reduction, written under `critic/`.

Numeric knobs referenced here (era bands, prose-reply threshold, risk cap) are **tunable defaults from
the prepare config**, not constants.

---

## Prompt

You are the critic for a sent-mail harvest. Read the **whole** first-draft proposal set plus the
existing brain; do **not** edit proposals, resolve contradictions, or reduce anything — you only judge
and flag. Check every proposal on these axes and record findings per cluster and cross-cluster.

### 1. Brain contract (home correctness)
Against [`../../../docs/brain-model.md`](../../../docs/brain-model.md): no response-mechanics, persona,
or channel wording sitting in brain-fact deltas — those belong on the persona surface. A brain-fact that
is really tone/voice is a **misfile**, flag it.

### 2. §5 evidence class — every skip / force_process proposal
For each skip/no-draft, sender block, or hard skip rule, confirm it rests on **presence-without-prose-
reply** evidence that is repeated, unambiguous, and machine-countable, with a stated **occurrence
count** from the manifest. Reject any skip proposed from **absence** or from **frequency alone** — that
inference is prohibited. For each `force_process`, confirm **deterministic repeated positive** evidence.
Flag every proposal that fails this gate.

### 3. §5a era flags
Confirm each durable rule (fact, price, address, product name, policy) carries an era tag and that
`stale-era` is set wherever all supporting evidence is outside the trailing (`recent`) era. Flag missing
or wrong era tags so reduction can apply supersessions.

### 4. §6 scope matrix — settings changes
Check every proposed settings change against the writable-scope reality:

| Signal | Narrowest writable target | Rule |
|---|---|---|
| Persona | mailbox | Apply at the harvested mailbox. |
| Triage policy | tenant or project (no mailbox scope) | Mailbox-derived evidence necessarily widens; widen **only** with explicit scope authority, else emit a **pending recommendation**. |
| Hard rules | tenant or project | Same widening rule; require deterministic evidence per §5. |
| Brain facts | tenant or project brain | Match the business scope of the fact. |

Flag any triage/hard-rule change that would widen mailbox-derived evidence to tenant/project **without
explicit scope authority** — it must become a pending recommendation, not a silent write.

### 5. Cross-cluster contradictions
Collate contradictions reported across clusters, plus any you spot between proposals (two clusters
asserting conflicting facts/handling). Name the clusters and topic; leave resolution to reduction.

### 6. Privacy leaks
Scan proposals for raw quotes, names, addresses, identifiers, counterparties, links, raw filenames, or
opaque IDs bleeding into proposal prose. Anything found is a hard finding to fix before reduction.

---

## Output — `{{SCRATCH_ROOT}}/critic/critic.md`

One document, grouped by axis, each finding tagged with the cluster(s) and severity. Suggested layout:

```markdown
# Critic report

## Misfiled homes (contract)          — <cluster>: <what belongs on persona/triage instead>
## §5 evidence failures               — <cluster>: <skip/force_process to reject or fix>
## Era / stale-era gaps               — <cluster>: <missing or wrong era tag>
## Scope violations (§6)              — <cluster>: <must become pending recommendation>
## Cross-cluster contradictions       — <clusters>: <topic + conflict>
## Privacy leaks                      — <cluster>: <what must be scrubbed>
## Verdict per cluster                — pass | fix-then-reduce | drop
```

Reduction (step 6) consumes this file: it drops or fixes everything the critic rejected and applies the
era supersessions the critic surfaced.
