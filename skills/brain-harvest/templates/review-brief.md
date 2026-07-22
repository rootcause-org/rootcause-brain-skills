# Generated review brief (pipeline step 10)

Run the deterministic generator after replaying every reserved holdout against the pushed dev ref.
The command validates the ledger, every final agent report, reduced proposals, scores, representative
production replay, token usage, cost, and wall clock before writing anything:

```bash
uv run --no-project python "$SKILL/scripts/prepare_harvest.py" review \
  --scratch "$SCRATCH" \
  --agent-report "$SCRATCH"/drafts/*.report.json \
  --reduction "$SCRATCH/critic/reduced.json" \
  --evaluation "$SCRATCH/brief/evaluation.json" \
  --metrics "$SCRATCH/brief/metrics.json" \
  --harvest-date YYYY-MM-DD --kit-version vX.Y.Z
```

Repeat `--agent-report` when shell expansion is unavailable. Generation fails unless every non-empty
original cluster has exactly one final report, all planned sampled/deep reads reconcile to the ledger,
all risk-marked threads were deep-read, no report remains `still_yielding`, every contradiction is
resolved or surfaced, and no reserved holdout handle or copied replay-content fingerprint appears in
synthesis artifacts. Applied settings changes additionally require before/after snapshots under
`$SCRATCH/settings-verification/`; their digests, exact target/scope, and five-minute read/write window
must match `reduced.json` and bound preflight state.

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
    "run_id":"run handle", "status":"succeeded", "cost_usd":0.12,
    "trace_url":"https://trace.example/run", "brain_sha":"40 lowercase hex characters",
    "brain_diff":"distilled description of the resolved brain diff"
  }
}
```

## Metrics input

Write `$SCRATCH/brief/metrics.json`. `total` must equal `input + output`; preparation time cannot exceed
the full wall clock; production replay cost cannot exceed total cost.

```json
{
  "token_usage":{"input":1200,"output":300,"total":1500},
  "cost_usd":0.50,
  "wall_clock_seconds":90.25,
  "preparation_seconds":0.25
}
```

## Generated outputs

`review` fully validates and privacy-lints a temporary bundle before replacing each local, ignored file
with an atomic rename. A failed validation leaves prior files untouched. Publication is not a
multi-file transaction, so it publishes `bundle-manifest.json` last as a commit marker; `record` rejects
an interrupted old/new mixture until `review` is rerun:

- `brief/review-brief.md` — full operator evidence: effective config/corpus digest, reconciled per-cluster
  coverage, saturation, settings scope, skip evidence, durable rules/eras, contradictions, holdout
  scorecard, production replay metadata, tokens/cost/wall clock;
- `brief/record-source.json` — sanitized machine source with ordinal holdouts only;
- `brief/record-candidate.json` — exact tracked-safe candidate the operator approves.

The full brief may contain opaque handles and private notes. The candidate contains only the spec's
audit fields and cannot contain thread handles, contacts, links, replay/run handles, trace metadata, or
raw text. Keep all three until approval.
