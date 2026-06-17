---
description: Iterate/verify the current brain locally — brief, run a grounding script, or run the test tiers (uv or docker mode).
argument-hint: "[brief | <script-path> [args…] | test [--live] | --mode docker …]"
---

Use the **brain-dev** skill to work on the brain in the current directory (`$ARGUMENTS`).

The engine is at `${CLAUDE_PLUGIN_ROOT}/scripts`:
- `brain_run.py --brief` — map the brain (env keys, DBs, mirrors, skills).
- `brain_run.py <script-path> [args…]` / `-m lib.db …` — run a grounding script or the DB CLI.
- `brain_test.py [--live | --require-live]` — run the pytest tiers.
- add `--mode docker` to any of the above for the faithful pre-push gate.

Default to `uv` mode for iteration; finish with `--mode docker` before recommending a push. Everything
is read-only. When you report a `uv`-mode result, include the fidelity caveat the runner prints.
