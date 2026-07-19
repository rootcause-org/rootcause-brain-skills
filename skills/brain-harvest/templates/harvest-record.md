# Committed harvest record format (pipeline step 12)

The one small **tracked** file written per harvest, after operator approval and just before scratch is
deleted. It is the sanitized subset of the review brief: it provides auditability once the corpus and
brief are gone, and it is the **watermark for incremental re-harvest** — a future `--since` run exports
and processes only threads newer than this record's date span, turning a re-run from a full harvest into
a small delta. For this to hold, opaque IDs and manifests stay stable across runs, so the watermark is
the date span plus export id, never a thread handle.

**Tracked-safe, hard rule.** This file lands in a brain checkout and is scanned by the privacy linter.
It carries **only counts, dates, and scores** — no opaque IDs, no raw thread text, no names, addresses,
identifiers, or counterparty links, and **no local control-plane command lines** (the linter flags those
as HARD in brain content). The values below are illustrative placeholders.

Suggested home: one file per harvest under `notes/harvest-records/`.

---

## Fields

| Field | Meaning |
|---|---|
| `harvest_date` | Date the harvest was approved and committed. |
| `export_id` | The export job handle this harvest consumed — the `--since` watermark anchor. |
| `threads` | Total threads in the corpus (all clusters). |
| `date_span` | First → last message date across the corpus. |
| `coverage` | Scanned / deep-read / sampled / noise / rerouted totals (must reconcile to the ledger). |
| `holdout` | Per-holdout scores: factual agreement / routing / tone vs the historical human answer. |
| `kit_version` | The brain-harvest kit version that produced this record. |

## Example

```yaml
harvest_record:
  harvest_date: 2026-07-19
  export_id: exp-2026-07-19-batch01
  threads: 911
  date_span: 2007-03 -> 2026-07
  coverage:
    scanned: 911
    deep_read: 74
    sampled: 512
    noise_excluded: 168
    rerouted: 21
  holdout:                 # scores only, no thread handles
    count: 8
    factual_agreement: 7/8
    routing: 8/8
    tone: 6/8
  kit_version: v0.2.0
  watermark: date_span.upper is the --since anchor for the next incremental re-harvest
```

Coverage totals must match the coverage ledger produced by preparation; `count` and the holdout scores
are copied verbatim from the review brief's committed subset. Nothing here identifies a correspondent or
a thread.
