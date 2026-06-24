---
name: rc-thread
description: Trace one rootcause thread or session using `rc thread ID` and explain why a draft did or did not go out. Use inside a brain checkout for "why did this email not get a reply?", delivery/callback questions, stalled run questions tied to a thread id, or when given a thread id or session UUID rather than a run UUID.
---

# rc-thread - trace one thread/session

Use the `rc` CLI from inside the brain checkout. Scope comes from the logged-in OAuth token and brain
metadata; no project argument, SSM, or operator access.

## Workflow

Require a thread id or session UUID. If absent, ask for one and stop.

Run:

```bash
rc thread <thread-or-session-id>
```

Read the output and explain the processor-side outcome in prose:

- Real draft went out.
- Guardrail fallback: the run hit its cost budget and did not answer.
- Blocked egress: the agent needed a domain that is not allowlisted.
- Stalled run: timeout or max iterations.
- Answer produced but callback rejected: rootcause answered, but channel delivery was refused.

For the run's full reasoning/tool trail, use `rc-inspect` on the run UUID shown by `rc thread`.

Scope note: `rc thread` covers the rootcause processor side. If there is no run at all, the cause may
be upstream in the email channel; flag that for an operator.
