# Side Effects

Diagnosis is read-only by default. Test-run creation and action execution are explicit exceptions.

| Surface | Side effect |
|---|---|
| `brain_run.py`, `brain_test.py`, `rc run list/show/events/trace/debug/brain-diff/thread`, `rc fleet runs/patterns/health`, `rc status` | Read-only. |
| `rc project mailbox harvest <id>` | Creates a **production** export job: a heavy provider sweep of the mailbox's sent history into a stored cleaned Markdown corpus body. Bills provider/API usage. |
| `rc project corpus download <id>` | Marks the export **consumed** (starts server-side eviction grace) and lands the raw mail corpus on local disk. Read-only server-side, but raw customer mail now exists on the laptop. |
| `rc project mailbox imap-env <id> --out ...` | Writes mailbox IMAP/SMTP credential material to a local env file. Must be under a gitignored path, never printed, and deleted after the session. |
| `uv run scripts/local_imap_harvest.py ...` | Connects to the IMAP server from the laptop and writes a raw local corpus. Must be gitignored, treated as customer mail, and deleted after synthesis. |
| `rc ask` against `main` | Creates a real production run. It may create a draft, journal/test artifacts, proposed actions, and bill model/API usage. |
| `rc ask --brain-ref dev/<branch>` | Creates a test run against a pushed dev ref. It does not post a callback or durable journal push; proposed actions/PRs are test artifacts. |
| `rc run feedback <id>` | Records score/comment feedback for consolidation. |
| `rc run retry <id>` | Creates a replacement production run, optionally at a different model tier. |
| Action proposal | LLM proposes only. No customer mutation. |
| Action confirmation or `rc dev console action run` | Real mutation path. Human/product-gated, outside the LLM loop. |
| `brain_action.py --commit` | Local real write to whatever `./.env.action` points at. Use only against local/staging or intentionally safe targets. |

When reporting results, distinguish "the draft says it happened" from the action lifecycle:
preflight blocked, action proposed, action confirmed, or action execution succeeded/failed.
