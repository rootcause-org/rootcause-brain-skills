# Generated committed harvest record (pipeline steps 10 and 12)

`review` renders the exact tracked-safe candidate at `$SCRATCH/brief/record-candidate.json` — the only
record the operator approves. `record --approved` then promotes it into the tracked brain **before**
scratch is deleted (invocation in step 12 of [`../SKILL.md`](../SKILL.md)).

`record` recomputes the expected candidate from the sanitized source and refuses a tampered one,
revalidates the ledger/run/preflight binding, requires a non-ignored destination inside the Git root,
privacy-lints it, and writes the reviewed bytes unchanged. An identical existing record is a no-op; a
different existing file is never overwritten. One file per harvest under `notes/harvest-records/`; its
upper date span plus export handle is the future incremental `--since` watermark.

## Tracked-safe JSON shape

```json
{
  "harvest_record": {
    "schema_version": 1,
    "harvest_date": "2026-07-22",
    "export_id": "safe-export-handle",
    "threads": 911,
    "date_span": ["2007-03-01", "2026-07-19"],
    "coverage": {
      "scanned": 911,
      "assigned": 735,
      "deep_read": 74,
      "sampled": 512,
      "noise_excluded": 168,
      "holdout": 8,
      "rerouted": 21
    },
    "holdout": {
      "count": 8,
      "cases": [
        {"case": 1, "scores": {"factual_agreement": 4, "routing": 4, "tone": 3}}
      ]
    },
    "run_metrics": {
      "turns": 42,
      "wall_clock_seconds": 480.0,
      "preparation_seconds": 2.1
    },
    "kit_version": "v0.2.3"
  }
}
```

The record carries no thread opaque IDs, replay/run IDs, trace URLs, brain SHAs/diffs, raw question or
answer text, names, addresses, contacts, counterparties, or local control-plane commands. Holdouts are
sequential sanitized ordinals only; scores use the fixed 0–4 scale.
