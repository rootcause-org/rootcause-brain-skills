---
description: Check whether anything is quietly rotting for this project — stale/failed source mirrors + dead-lettered runs. Exits non-zero when unhealthy.
argument-hint: "[--hours <n>]"
---

Use the **observability** skill. Proactive "is anything broken that no one reported?" for the current
project, over the `rc login` OAuth token — no SSM/operator access.

`$ARGUMENTS` → pass through `--hours` if present (default 24h dead-letter window).

## What to do

```bash
rc health                  # mirrors + dead-letters, last 24h
rc health --hours 72       # widen the window
```

Relay the verdict plainly. Two things to watch:

- **Mirrors** — a source mirror that's failed to sync or gone stale means the agent's source grounding
  is going out of date. Name the mirror + how long it's been stale.
- **Dead-lettered runs** — a draft that kept failing to deliver past the cutoff, so the customer never
  got it. These are the urgent ones; surface the run UUIDs and drill with [`/rc-inspect <uuid>`](rc-inspect.md).

`rc health` **exits non-zero when unhealthy**, so it doubles as a cron/CI gate (`rc health || alert`).

**When to use:** start here when nothing's on fire — a periodic sweep. When something IS on fire for a
specific thread, use [`/rc-thread`](rc-thread.md) instead.
