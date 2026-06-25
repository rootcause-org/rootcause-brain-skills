# Side Effects

Diagnosis is read-only by default. Test-run creation and action execution are explicit exceptions.

| Surface | Side effect |
|---|---|
| `brain_run.py`, `brain_test.py`, `rc run`, `rc fleet`, `rc health`, `rc thread` | Read-only. |
| `rc ask` against `main` | Creates a real production run. It may create a draft, journal/test artifacts, proposed actions, and bill model/API usage. |
| `rc ask --brain-ref dev/<branch>` | Creates a test run against a pushed dev ref. It does not post a callback or durable journal push; proposed actions/PRs are test artifacts. |
| Action proposal | LLM proposes only. No customer mutation. |
| Action confirmation or public dev-trigger when exposed | Real mutation path. Human/product-gated, outside the LLM loop. |
| `brain_action.py --commit` | Local real write to whatever `./.env.action` points at. Use only against local/staging or intentionally safe targets. |

When reporting results, distinguish "the draft says it happened" from the action lifecycle:
preflight blocked, action proposed, action confirmed, or action execution succeeded/failed.
