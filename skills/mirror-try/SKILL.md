---
name: mirror-try
description: Try a read-only Python helper change from a mirror checkout against real scoped production data, comparing base and proposed bytes without changing the live mirror ref.
---

Run from a brain checkout with `rc` login. Inspect the helper and its imports for read-only behavior
first: no actions, sends, customer writes or write credentials.

```bash
MT="$PWD/.agents/skills/mirror-try"
uv run "$MT/scripts/mirror_try.py" --repo /path/to/mirror --ref review/fix \
  --project PROJECT --tenant TENANT --read-only-reviewed \
  --cmd 'python skills/topic/scripts/helper.py ARGS'
```

Fetch the mirror first. `--ref` compares against local `origin/main`; `--base COMMIT` overrides it.
`--diff` instead compares tracked working-tree bytes (including staged changes) against HEAD;
add new files to the index first. `--json` emits reduced stdout/stderr, exit codes, exact revisions,
staged paths and console run IDs. A working-tree result uses a content hash, not a commit claim.
For principal scope, supply `--principal-kind KIND --principal-id ID` (`--principal ID` is an alias).
Omitting `--tenant` explicitly selects project scope.

Changed non-test grounding files (excluding hidden/action paths), the command's Python script and recursively resolved static sibling/root
imports are staged into isolated revision directories under `/tmp/try/<repo>/`. Package initializers
are included. Use repeated `--path relative/file` for runtime data or dynamic imports. Both versions
run with identical arguments and scope; scratch is removed afterwards. New helpers truthfully fail
in Before. After failure exits nonzero; transport/truncation failures are not helper evidence.

This proves **the staged helper on real scoped data**. It does not prove an agent run, retrieval,
tree-wide greps, git history, or deployed mirror bytes. Absolute `/mirrors` and `/brain` reads still
see live files; inspect/repoint these dependencies before claiming the changed code was exercised.
Dynamic imports and runtime path manipulation are not inferred. Data may change between reads.

The console accepts at most 256 KiB stdin; this recipe caps base64/JSON files at 240,000 bytes and
its compressed generated command at 120,000 bytes (the console shell also hits Linux’s
128 KiB per-argument limit before the stdin cap). Narrow oversized changes; never silently omit dependencies.
Explicit/imported hidden files and actions, plus symlinks and submodules, are refused. Output uses the fleet report's heuristic
privacy reducer, not a PII guarantee: prefer aggregate/identifier-only commands and inspect before
sharing. No merge, refresh or deployment is performed.

For review cards, [helper-prototype](../brain-fleet-report/helper-prototype.md) owns branch/capture
and publication. For brain content use [brain-ask](../brain-ask/SKILL.md)'s dev ref with simulation;
mirror refs remain project-wide. See [prod-console](../prod-console/SKILL.md) for scope and access.
