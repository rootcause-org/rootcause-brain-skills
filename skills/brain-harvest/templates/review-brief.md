# Operator review brief format (pipeline step 10)

The generated brief the operator reads at the single diff-approval gate (step 11). It is written into
the scratch root's `brief/` — **local, ignored, and ephemeral**. It exists so the human gate can check
evidence behind every claimed rule instead of rubber-stamping; it is deleted with the rest of scratch
**after** approval (step 12).

Two privacy tiers, and the brief must mark every section as one or the other:

- **Local + ephemeral** — the full brief. May reference opaque IDs and short evidence context, because
  the operator can open the local corpus by opaque handle. Never committed.
- **Sanitized committed subset** — the [`harvest-record.md`](harvest-record.md) fields only: counts,
  dates, and scores. **No opaque IDs, no raw data.** This is the sole part that may be tracked.

Numeric values (holdout 8, sample cap 50, risk cap 15%) are **tunable defaults from the prepare
config**, not constants — the brief reports the values actually used.

---

## Brief layout (`{{SCRATCH_ROOT}}/brief/review-brief.md`)

Mark each heading `[local+ephemeral]` or `[committed subset]`.

### 1. Coverage summary — per cluster `[local+ephemeral]`
Table, one row per cluster:

| Cluster | Label | Scanned | Deep-read | Sampled | Noise (excluded) | Rerouted |
|---|---|---|---|---|---|---|

Totals row must reconcile to the coverage ledger (every thread accounted for exactly once). Counts only
in the committed subset; opaque IDs allowed here in the local brief.

### 2. Settings changes — every one, with scope `[local+ephemeral]`
Each change: surface (persona / triage policy / hard rule), **scope applied** (mailbox / tenant /
project), and — for triage/hard rules derived from a single mailbox — whether it was applied under
explicit scope authority or held as a **pending recommendation** (§6).

### 3. Skip proposals — each with its §5 evidence `[local+ephemeral]`
One row per skip/no-draft proposal, its subject/sender family, and its **occurrence count** of
presence-without-prose-reply evidence. Surfaced individually and never applied silently — the operator
approves each. (No skip from absence or frequency alone.)

### 4. Notable durable rules `[local+ephemeral]`
The high-value facts/rules with **evidence strength** (supporting thread count) and **era flag**
(`recent` / `mid` / `old`, and any `stale-era`).

### 5. Contradictions and resolutions `[local+ephemeral]`
Each discovered contradiction, how reduction resolved it (with any era supersession recorded), or that
it is **surfaced unresolved** for the operator's call.

### 6. Holdout scorecard `[committed subset]`
Per held-out thread (default 8, tunable), scored by the comparison agent against the historical human
answer:

| Holdout | Factual agreement | Routing | Tone |
|---|---|---|---|

Scores only — the committed record carries the scorecard with **no opaque IDs**. The local brief may add
an opaque-ID column for the operator to open each source.

### 7. Run cost `[committed subset]`
Token cost and wall clock for the run.

---

The operator consults this brief (and, via opaque IDs, the still-present local corpus) at the gate. On
approval, the sanitized subset (sections 6–7 plus coverage counts) is distilled into the committed
[`harvest-record.md`](harvest-record.md); everything else is deleted with scratch.
