---
description: Trace ONE thread/session on the rootcause side and explain why a draft did (or didn't) go out.
argument-hint: <thread-id | session-uuid>
---

Use the **observability** skill to trace one production thread for the current project, over the `rc
login` OAuth token — no SSM/operator access.

`$ARGUMENTS` is a **thread id or session UUID**. If empty, ask for one and stop.

## What to do

```bash
rc thread <thread-or-session-id>
```

Read it and explain, in prose, the processor-side outcome for that thread — is there a run, what did
the agent do, did our callback to the email channel land? Common verdicts:

- **a real draft** went out — all good.
- **guardrail fallback** (the run hit its cost budget and never actually answered).
- **blocked egress** — the agent needed a domain that isn't allowlisted.
- **stalled run** — hit a timeout / max iterations.
- **answer produced but callback rejected** — we answered, but delivery to the email channel was
  refused (a contract issue on the channel side).

For the run's full reasoning/tool-trail behind the verdict, drill with [`/rc-inspect <uuid>`](rc-inspect.md).

> **Scope note.** `rc thread` covers the **rootcause (processor) side**. The email-channel (ReplyPen)
> side of the trace is operator-only for now; if the thread never produced a run at all, the cause may
> be upstream (the channel never sent us the turn) — flag that for an operator.

**When to use:** a specific "why didn't this email get a reply?" — when you have the thread/session id
in hand.
