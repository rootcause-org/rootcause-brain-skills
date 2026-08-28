---
name: rc-script-wrapper
description: Wrap the public `rc` CLI safely from deterministic local Python or shell scripts. Use for repeatable production-console queries, workspace commands, full exports, typed failures, or fetching a console artifact; not for code that runs inside a production brain container.
---

# rc-script-wrapper - deterministic local wrappers

Use this skill only from a developer's machine/brain checkout. Production brain scripts do not have
`rc`; use their injected `lib.db`, `lib.fs`, and other runtime helpers instead.

## Preferred Python API

Install a published, pinned `rootcause-runtime` tag into the script environment; do not copy its
wrapper or float `main`. Use the current published kit tag:

```bash
uv run --with "rootcause-runtime @ git+https://github.com/rootcause-org/rootcause-brain-skills@v0.3.31#subdirectory=runtime" script.py
```

For a standalone script, pin the same dependency in its PEP 723 header:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["rootcause-runtime @ git+https://github.com/rootcause-org/rootcause-brain-skills@v0.3.31#subdirectory=runtime"]
# ///
from lib import rc_client
```

Run `uv lock --script script.py` once and commit the resulting `script.py.lock`: it freezes the tag
to a commit sha plus the whole transitive closure, so every later `uv run` resolves from cache
instead of re-fetching the git tag over the network (re-run it after bumping the tag).

See [the migration runbook](../../docs/migration-rootcause.md) when moving an existing wrapper.
`rootcause-runtime` preserves the CLI's JSON contract and maps its exit codes to exceptions. Default
database resolution is `RC_CONSOLE_DATABASE`, then `RC_DB_DEFAULT`, then `prod`; pass `database=` when
the script needs a specific connection.

```python
from lib import rc_client

result = rc_client.query(
    "select id, state from jobs where created_at >= @since order by id",
    {"since": "2026-08-01"},
    all=True,
    database="billing",
)
for row in result.rows:  # values align with result.columns; duplicate columns are retained
    print(dict(zip(result.columns, row)))

rc_client.query_to_csv("select id, state from jobs order by id", "./jobs.csv", all=True)
code, stdout, stderr = rc_client.bash("python /brain/skills/reconcile/scripts/check.py", timeout=120)
rc_client.file_get("/tmp/rootcause-out/jobs-abc123.csv", "./jobs.csv")
```

Catch only the recovery you can actually perform: `AuthenticationError` means login/scope,
`TruncatedError` means use `all=True` or intentionally opt into `allow_truncated=True`,
`RemoteCommandError` means the guarded workspace command could not be started, and `TransportError`
means server/network trouble. `bash()` returns `BashResult` even when the remote command exits non-zero
or times out; inspect `exit_code`, `stderr`, and `timed_out`. `query_to_csv()` streams CLI-rendered CSV
to its destination and returns that `Path`, rather than buffering rows. Do not parse table output, spill
manifests, or shell-quoted JSON.

`bash()` assumes the server's standard 120-second remote default when `timeout` is omitted and gives the
local CLI another 30 seconds for setup/transport. Pass `timeout=` explicitly if a project's advertised
console capability uses another limit.

## CLI contract for non-Python scripts

Use the machine envelope and force direct stdout when a program parses it:

```bash
rc -o json dev console database query billing 'select id from jobs where state = @state order by id' \
  --param state=queued --all --format json --out - > jobs.json
rc -o json dev console bash run 'python /brain/skills/reconcile/scripts/check.py' --out -
```

Exit codes are stable: 0 success; 1 local usage/input; 2 OAuth/authz; 3 truncated query; 4 remote bash
non-zero or timeout; 5 server/network. With `-o json`, regular errors are a JSON
`{error:{code,message,status,fields}}` envelope on stdout. `bash run` instead retains its structured
result payload on exit 4 so callers can read `exit_code`, `stderr`, and `timed_out`. Branch on the exit
code; never treat a partial response as success.

Use `--all` for a complete export: it is one streaming request over a server-side cursor in a single
repeatable-read transaction, so concurrent changes cannot duplicate or omit rows. `ORDER BY` is optional
for completeness; use it when deterministic output order matters. The ordinary inline limit is deliberately
small; no partial result may be consumed after `truncated:true` unless the script explicitly allows it. Stream a large
result to a local file with `--out ./rows.csv --format csv`; use `--out auto` only for a human-readable
local artifact manifest. `--out -` is for a parser that needs stdout.

Pass SQL and workspace text directly as one argument, from stdin (`-`), or with `@file`; never invent
base64/character-substitution quoting. Use `@key` placeholders and repeat `--param key=value` for values;
parameterization is the only supported way to interpolate data into SQL.

## Remote artifacts

The production runtime's `emit_rows` previews name a fetchable `/tmp/rootcause-out/...` file. Fetch it
without reprinting the whole artifact:

```bash
rc dev console file get /tmp/rootcause-out/report-abc123.csv --out ./report.csv
```

Use files for complete machine processing; use the preview for shape/orientation. The console only
permits its session workspace and `/tmp`, so a rejected path is a boundary, not a reason to fall back to
base64 over bash output.
