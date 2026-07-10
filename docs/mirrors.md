# Mirrors

A source mirror is a read-only snapshot of an external source repo or knowledge source mounted at
`/mirrors/<name>` during a run. Brain scripts should read mirrors through `lib.fs` or explicit
`/mirrors/<name>` paths; they should never write them.

## Mental Model

- Brain freshness and mirror freshness are independent. `rc ask --brain-ref dev/x` changes which brain
  ref production mounts, but it does not refresh mirrors or knowledge-base sync state.
- In production, mirror refresh is managed by RootCause. A stale or failed mirror is usually a support
  issue, not a brain content issue.
- Locally, pass mirrors explicitly:

```bash
uv run "$SKILL/scripts/brain_run.py" --mirrors-root ~/mirrors ...
uv run "$SKILL/scripts/brain_run.py" --mirror app=~/code/customer-app ...
```

- `brain_run.py --brief` shows which local mirrors are visible.
- `rc fleet health` reports stale/failed mirrors and dead-lettered runs from the public API.

## Triage

| Evidence | Interpretation |
|---|---|
| Local script fails because `/mirrors/<name>` is absent | Add `--mirrors-root`/`--mirror`, or skip local mirror-dependent checks. |
| `rc fleet health` reports a stale/failed mirror | Escalate with the mirror name and staleness; brain edits will not fix freshness. |
| A prod run read old source content | Check run trace "Files the run read" and `rc fleet health`; mirror freshness may lag brain deploy. |
| A dev-ref run still sees old source content | Expected if only the brain changed. Dev refs do not change mirror snapshots. |
