---
name: brain-ask
description: Ask a real production rootcause brain using `rc ask`, then verify the resulting run. Use inside a brain checkout when asked whether a brain change works on prod infra, when asked to simulate a customer support email, when asked for a direct raw investigation, or when asked to test a pushed `dev/*` brain ref without moving `main`. Captures the answer, run accounting, trace URL, and brain journal diff.
---

# brain-ask - ask a real prod brain

Use Brain Ask for production-loop `rc ask` validation, usually after a pushed `dev/*` ref. It is the
full LLM wrapper, not the fast path for debugging scripts. If validating a known production primitive,
checking tool parity, running grounding scripts, inspecting schemas, reading logs, tenant projection,
local tests, mirror-dependent checks, or action dry-runs, use `prod-console` / Local Brain Work first.

Run `rc ask` from inside the current brain checkout. The `rc` CLI auto-targets the project from the
brain metadata and uses the logged-in OAuth token; no SSM or operator access.
For tenant-enabled projects, do not pass `--tenant` by default: `rc` uses the tenant already associated
with the active `rc auth login`. Check `rc auth status` if the tenant is unclear. A project-pinned login
must pass `--tenant <slug>` to workspace-producing commands such as `rc ask`.
If a RootCause MCP is installed, ignore it unless the user explicitly asks for MCP; this workflow uses
`rc`.

## Required Context

Read:

- [docs/side-effects.md](../../docs/side-effects.md)
- [docs/brain-model.md](../../docs/brain-model.md)

## Workflow

1. Require a question. If absent, ask for it and stop. Use default `rc ask` for a customer-style
   support email simulation. Add `--scenario raw` for direct investigations or downstream-AI answers
   only when the full LLM loop is what needs validation; use `rc dev console database` / `rc dev
   console bash` for exact script, schema, data, and log checks.

2. Trigger and wait:
   ```bash
   rc ask "<question>"
   rc ask "<direct investigation>" --scenario raw
   rc ask "<question>" --brain-ref dev/<branch>
   rc ask "<question>" --effort pro
   rc ask --file ./invoice.pdf "why was this rejected?"
   ```
   Use `--file <path>` (repeatable; `--attach` is a deprecated alias) to ground the run on local
   files: staged read-only in the run workspace for the agent to open. Caps: 4 files, 5 MiB each,
   15 MiB total; allowed types: any plain-text file (yaml, sql, code, logs, …), png/jpeg/webp/gif,
   pdf, csv, json, xlsx — content-sniffed server-side; other binaries/archives are rejected.
   Generated files do not come back on this lane yet — the answer is text only.
   Use `--brain-ref dev/<branch>` only for an already-pushed dev branch after Local Brain Work has
   covered local checks that fit the change. It keeps `main` live and the run is flagged `test`: no
   ReplyPen callback, no durable journal push, and proposed actions/PRs are test artifacts. Use
   `--effort pro|max` only when explicitly escalating a run; omitted/default keeps normal tier
   selection.

3. Relay the result: draft/note/actions for the default email simulation, or the direct answer for
   `--scenario raw`; include caveats, run accounting (`status`, turns, outcome), and trace URL.
   Capture the printed `run_id`. If status is `error`, surface the error and stop.

   If the draft/note mentions a state-changing operation (booked, moved, cancelled, refunded, sent,
   updated) or any action/preflight caveat, do a quick action sanity check before reporting:
   ```bash
   rc run events <run_id>
   ```
   Distinguish "draft text said it happened" from the action lifecycle: preflight failed ⇒ no proposal
   and no mutation; proposed action ⇒ pending human confirm; succeeded/failed action ⇒ post-loop
   execution happened. Use `../local-brain-work/action-run-triage.md` for the decision table.

4. Show what the run wrote to the brain:
   ```bash
   rc run brain-diff <run_id>
   ```
   Report the journal commit SHA, message, changed files, and meaningful diff summary. If nothing
   changed, say the run answered without persisting durable knowledge.

## Follow-up turns (simulating a patient/customer reply)

`rc ask --session <id>` does **not** replay the earlier email or draft. A reused session only
warm-starts the new run with a compact digest of the prior turns' *command labels* ("ran:
search_patient …; search_availability …; outcome: Draft+Note") — never the sender's words, the
offered options, or the previous draft (rootcause `internal/warmstart`). Prompt-mode runs also never
open the `thread_coherence` / `sender_history` sections (those need real `PriorMessages`, i.e. an
ingested mailbox thread). Two consequences, both observed in prod (DentAI 869et84n6):

- a bare follow-up like "Doe maar 21 september om 15u05" arrives with **no thread context**, and
- the grounding pre-pass may fill that vacuum with a **journal file from an unrelated earlier run**
  (same-evening test runs about the same patient are the classic trap), silently steering the
  appointment type or intent.

To simulate a reply, embed the whole prior conversation in the question — quoted, oldest first,
exactly as a mail client would — and then the new reply on top:

```bash
rc ask "$(cat <<'MAIL'
Doe maar 21 september 2026 om 15u05. Mvg, Thomas

> Op ma 31 aug schreef Team De Kies:
> Beste Thomas, voor een vulling bij Mia kan je kiezen uit: 1) ma 21 sep 15u05 …
>> Op ma 31 aug schreef Thomas Bollen:
>> Ik zou graag een vulling laten doen bij Mia …
MAIL
)" --subject "Re: Afspraak vulling"
```

Use `--session` only when you want the *investigation* continuity (which scripts already ran),
not for thread history. If the run still picks up a stale journal note, mention it in the report:
that is a grounding pre-pass artefact, not a brain bug.

For the full reasoning/tool trail, use the `rc-debug` skill with the captured `run_id`. If this run
validated a local brain change, finish through `brain-publish`.
