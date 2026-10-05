# Developer escalation tickets — setup and verification

When a run decides a developer must act, the host can file a ticket in the project's own tracker
(ClickUp today). The brain only teaches the agent *when* something is a bug and what evidence to
collect; the host owns the gate, the ticket body, redaction and delivery. This page is the operator
recipe from a brain checkout with public `rc`.

## When a ticket is filed (the gate)

All of these must hold, resolved once per run:

| Condition | Meaning |
|---|---|
| No human reviewer | Effective `autonomy_mode=send` for the project/tenant. In `draft` mode the run only gets the `Dev escalation needed: …` note line; nothing is filed. |
| Eligible surface | `email`, `intercom`, `whatsapp`, `dashboard_chat`, `mcp`, `prompt_api`, `embassy`. Never consumer `chat`, `compose`, `console`. |
| Delivery allowed | A `shadow` mailbox never files. `watch`/`off` mailboxes produce no runs at all. |
| Destination configured | Project or tenant setting `escalation.destination` (see below). |
| Not a test | `--simulation` and `--brain-ref dev/…` runs never stage a ticket. |
| Filable dossier | The agent's `escalation` object has `kind=bug` **and** non-empty `actual`, `expected` and at least one `evidence`. `kind=request|other` or thin symptoms are discarded (`reason=kind_other`, `insufficient_symptoms`). |

The reviewer note line keeps its broad meaning (any work the reviewer cannot finish); only the ticket
is gated. Delivered tickets carry the marker `replypen-run:<uuid>` for dedupe and coalescing.

## Setup (once per project)

1. **A pasted-token connection** for the tracker. The sender refuses OAuth rows:
   ```bash
   rc project connection add integration_key=clickup label=escalation kind=token token="$CLICKUP_TOKEN"
   rc project connection ls -o json        # note the new id; kind must be "token"
   ```
   Keep **one** active connection per integration: two active ClickUp rows (e.g. an older OAuth one
   plus the token) make every run fail at boot with
   `load integration connections: multiple active connections use the same env var`. Remove the
   extra with `rc project connection rm <id>`.
2. **The destination** (project scope; a tenant may shadow it or set `{"disabled": true}`):
   ```bash
   rc project settings behavior set 'escalation.destination={"connection_id":"<token-connection-id>","adapter":"clickup","destination_id":"<list id>","attributes":{"assignee_id":<member id>,"status":"to do","priority":2}}'
   ```
   `status` must exist on that list, `assignee_id` must be a list member, `priority` 1–4. The
   dashboard page `/projects/<p>/settings/integrations/clickup` validates these live and shows the last
   delivery; `rc` only shape-checks.
3. **Teach the brain** what a bug is: a routing row ("something is wrong / not working") that tells the
   agent to ground the symptom (ids, timestamps, table rows), fill `escalation{kind: bug, actual,
   expected, evidence[]}`, and keep requests as `kind: request`. Never put tracker ids or `rc` commands
   in the brain.

## Verify end-to-end without touching customers

A `draft` project needs a short `send` window. Protect the inbox first:

```bash
MB=<mailbox id from: rc project mailbox ls>
rc project mailbox mode "$MB" watch                      # inbound runs stop; sync continues
rc project settings runtime set autonomy_mode=send
rc ask --project <p> --scenario email --from <known customer address> \
  "…a realistic bug report the brain can ground…"       # NOT --simulation, NOT --brain-ref
rc project settings runtime set autonomy_mode=draft      # restore immediately, even on failure
rc project mailbox mode "$MB" live
```

Then prove it, in order:

- `rc run debug <run id>`: the `escalation_dossier` prompt section is `yes` (gate eligible) and the
  reply line shows `has_escalation=yes`.
- The sender sweeps once a minute after a 30 s settle: within ~2 minutes the ticket exists on the list
  with the `replypen-run:<uuid>` marker, the configured status, assignee and priority. Mark a synthetic
  test ticket as dropped afterwards so it does not pollute the backlog.
- No ticket? The host log has one line per run, `escalation gate … eligible= human_reviewed= surface=
  destination=`, a second `filed= reason=` line, and on failure `escalation not delivered … err=`.
  Ask RootCause support for those lines if you cannot read host logs; the integration page shows the
  last delivery error too.

Known limits: the ticket body passes through PII redaction, which also masks dates and times as
`[redacted:number]`; the run URL in the ticket is the operator's way back to the full evidence.
