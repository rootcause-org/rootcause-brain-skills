# Generated review brief (pipeline step 10)

Input contracts for `prepare_harvest.py review` (invocation in step 10 of [`../SKILL.md`](../SKILL.md); repeat
`--agent-report` when shell expansion is unavailable).

The generator is the real gate: it refuses to write unless every non-empty original cluster has exactly
one final report, planned sampled/deep reads reconcile to the ledger, all risk-marked threads were
deep-read, no report is still `still_yielding`, every contradiction is resolved or surfaced, and no
reserved holdout handle or copied replay-content fingerprint appears in synthesis artifacts. Applied
settings changes additionally need matching before/after snapshots under `$SCRATCH/settings-verification/`
(digests, exact target/scope, five-minute window) bound to preflight state.

## Evaluation input

Write `$SCRATCH/brief/evaluation.json`. Score every reserved holdout exactly once on the fixed integer
scale 0 (failure) through 4 (strong match). Every holdout needs a distinct replay ID and trace URL; the
representative production replay must be distinct from all of them. Keep notes local; they are omitted
from the record.

```json
{
  "holdouts": [
    {"id":"H0123456789abcdef0123456789abcdef", "replay_id":"local replay handle",
     "status":"succeeded", "trace_url":"https://trace.example/holdout",
     "brain_sha":"40 lowercase hex characters",
     "scores":{"factual_agreement":4,"routing":3,"tone":4}, "notes":"private comparison note"}
  ],
  "production_replay": {
    "run_id":"run handle", "status":"succeeded", "turns":14,
    "trace_url":"https://trace.example/run", "brain_sha":"40 lowercase hex characters",
    "brain_diff":"distilled description of the resolved brain diff"
  }
}
```

## Metrics input

Write `$SCRATCH/brief/metrics.json`. Preparation time cannot exceed the full wall clock.

```json
{
  "turns":42,
  "wall_clock_seconds":90.25,
  "preparation_seconds":0.25
}
```

## Generated outputs

`review` validates and privacy-lints a temporary bundle before atomically replacing each local ignored
file; a failed validation leaves the prior files untouched. Publication is **not** a multi-file
transaction, so `bundle-manifest.json` is published last as the commit marker and `record` rejects an
interrupted old/new mixture until `review` is rerun.

- `brief/review-brief.md` — full operator evidence: effective config/corpus digest, reconciled per-cluster
  coverage, counts-only source diagnostics, saturation, settings scope, skip evidence, durable
  rules/eras, contradictions, holdout scorecard, production replay metadata, turns/wall clock;
- `brief/record-source.json` — sanitized machine source with ordinal holdouts only;
- `brief/record-candidate.json` — exact tracked-safe candidate the operator approves.

The full brief may contain opaque handles and private notes. The candidate contains only the spec's
audit fields and cannot contain thread handles, contacts, links, replay/run handles, trace metadata, or
raw text. Keep all three until approval.
