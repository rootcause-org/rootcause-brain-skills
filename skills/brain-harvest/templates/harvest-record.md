# Generated committed harvest record (pipeline steps 10 and 12)

`prepare_harvest.py review` renders the exact tracked-safe JSON candidate under
`$SCRATCH/brief/record-candidate.json` before the operator gate. It is the only record the operator
approves. After approval, promote it into the tracked brain **before** deleting scratch:

```bash
uv run --no-project python "$SKILL/scripts/prepare_harvest.py" record \
  --scratch "$SCRATCH" \
  --out "notes/harvest-records/YYYY-MM-DD.json" \
  --approved
```

`record` revalidates current ledger/run/preflight binding, recomputes the expected candidate from the sanitized
source, refuses a changed/tampered candidate, requires a non-ignored destination inside the Git root,
privacy-lints it, and writes the reviewed bytes unchanged. An identical existing record is a no-op; a
different existing file is never overwritten. Use one file per
harvest under `notes/harvest-records/`; the upper date span plus export handle is the future incremental
`--since` watermark.

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
      "token_usage": {"input": 120000, "output": 18000, "total": 138000},
      "cost_usd": 3.25,
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
