# Support Boundary

Use this kit to change the brain when the evidence points to project knowledge, grounding, tests, or
actions. Escalate to RootCause support when the missing capability lives outside the public `rc`/API
surface or in managed infrastructure.

| Symptom | Likely owner |
|---|---|
| Wrong query, missing playbook, bad prompt knowledge, missing action description | Brain change. |
| `.rootcause.toml` missing or wrong | Brain repo setup; ask RootCause support if you cannot edit it. |
| OAuth/login/scope failure | Project auth setup or RootCause support. Include `rc auth status` output. |
| Private DB unreachable from laptop | Expected infra boundary. Local `lib.db` fails after a 15s connect timeout; verify with `rc dev console database` / `rc dev console bash`, not local live tests. |
| Stale or failed mirror | RootCause mirror pipeline/support. Brain changes do not refresh mirrors. |
| No inbound email run at all | Upstream channel/ReplyPen routing or RootCause support. |
| Callback rejected or dead-lettered | RootCause/channel integration. Use `rc run thread`/`rc fleet health` evidence. |
| Action plane 404, disabled, or not wired | RootCause support plus the customer app owner for app-side receivers. |
| Pushed brain commit not mounted | Run `rc dev brain status`; then `rc dev brain sync` if behind/stale. |
| `rc dev brain sync` reports manual reconcile | RootCause support request with `rc dev brain status -o json`. |
| Channel promote / tenant publish / action wiring not exposed through public `rc` | RootCause support request; product gap to close. |

## Support Request Template

```text
Project/brain:
Tenant, if any:
Brain repo path:
Branch/ref:
Commit SHA:
Requested outcome:
Verification already run:
Run ids / trace URLs:
Why this is not a brain-only change:
```

Keep support requests in product terms: "sync origin", "promote shared project brain to stable", or
"wire/diagnose action execution", not private host commands.
