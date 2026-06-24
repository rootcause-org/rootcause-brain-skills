---
name: rc-health
description: Check whether a rootcause project is quietly unhealthy using `rc health` for stale or failed source mirrors plus dead-lettered runs. Use inside a brain checkout for periodic sweeps, CI/cron-style gates, or "is anything broken?" questions. Exits non-zero when unhealthy and points flagged run UUIDs toward `rc-inspect`.
---

# rc-health - project health sweep

Use the `rc` CLI from inside the brain checkout. Scope comes from the logged-in OAuth token and brain
metadata; no project argument, SSM, or operator access.

## Workflow

Run:

```bash
rc health
rc health --hours 72
```

Pass through an explicit `--hours <n>` if supplied; default is a 24-hour dead-letter window.

Relay the verdict plainly:

- Mirrors: name any source mirror that failed sync or went stale, including how long it has been stale.
- Dead-lettered runs: surface run UUIDs; these are urgent because a draft never reached the customer.

`rc health` exits non-zero when unhealthy, so do not treat non-zero as a tool failure by itself. Read
the output and report what is unhealthy.

Use `rc-inspect` for flagged UUIDs. Use `rc-thread` instead when the question starts from a specific
thread/session id.
