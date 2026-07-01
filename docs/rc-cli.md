# `rc` CLI

`rc` is the public CLI over RootCause's JSON API. In a brain checkout, it uses `.rootcause.toml` plus
your `rc login` OAuth token to scope commands to the project/tenant. Use it instead of private
RootCause source or host tooling.

If a capability is not exposed through `rc`/API, use [`brain-publish`](../skills/brain-publish/SKILL.md)
to prepare a support request.

## Commands

```bash
rc ask "<customer-style question>"
rc ask "<direct investigation>" --scenario raw
rc ask "<q>" --brain-ref dev/<branch>
rc ask "<q>" --effort pro
rc run <id>
rc run <id> --events
rc run <id> --brain-diff
rc run <id> --debug
rc run <id> --full -o json
rc runs [--limit N] [--kind email|prompt|mcp|analysis] [--category ok|timeout|...]
rc status
rc fleet [--days N] [--kind ...]
rc patterns [--days N]
rc health [--hours N]
rc thread <id>
rc config get
rc config set max_run_usd=5 default_tier=pro
rc brain status
rc brain sync
rc env keys
rc env pull
rc env diff
printf %s "$SECRET_VALUE" | rc env set key=FOO_API_TOKEN
rc env rm FOO_API_TOKEN
rc env reveal FOO_API_TOKEN
rc login
rc whoami
```

Every command supports `-o json` for scripting.

## Auth And Scope

`rc` is OAuth-only. `rc login` stores tokens under `~/.config/rootcause/tokens.json` with mode `0600`.
There is no API key file for this kit.

In a brain checkout, `.rootcause.toml` carries the non-secret project/base URL binding. Resolution:

```text
explicit --profile <name>          -> that profile's stored token
inside a brain + project token     -> profile named by .rootcause.toml project
inside a brain + no project token  -> default profile + .rootcause.toml project as ?project=
outside any brain                  -> default profile / built-in default
base_url: ROOTCAUSE_BASE_URL > .rootcause.toml base_url > profile base_url > built-in default
```

Use `rc whoami` to confirm project, tenant, profile, and sign-in status. On tenant-enabled projects,
plain `rc ask` normally uses the tenant bound to the login; use `--tenant` only as an explicit
override/debug aid.

## `rc ask`

`rc ask` triggers a real production run and waits by default.

- Default scenario is email simulation: draft/note/actions/PRs as a reviewer would see them.
- `--scenario raw` returns a direct investigation answer.
- `--brain-ref dev/<branch>` tests a pushed dev branch on production infrastructure without moving
  live refs. It creates a test run: no callback, no durable journal push, and proposals are test
  artifacts.
- `--effort pro|max` is an explicit stronger-tier retry.

Use [docs/side-effects.md](side-effects.md) when reporting what a run did.

## Debug A Run

```bash
rc run <id> --debug
```

This writes `rc-debug/<run8>-<project>.{md,jsonl}`. Read the markdown index first, then query JSONL
with `jq`. Use [`rc-debug`](../skills/rc-debug/SKILL.md) for the analysis-first workflow and
[docs/run-trace-model.md](run-trace-model.md) for the mental model.

## Env

```bash
rc env keys
rc env pull
rc env diff
printf %s "$SECRET_VALUE" | rc env set key=FOO_API_TOKEN
rc env rm FOO_API_TOKEN
rc env reveal FOO_API_TOKEN
```

`rc env pull` writes the production grounding `.env` to the brain root with mode `0600`; values are not
printed. `rc env set` adds or rotates one grounding secret, reading the value from STDIN by default and
never echoing it. `rc env reveal` is the deliberate exception: it prints one live value for copy/pipe use
and is audited. The file is gitignored and contains real secrets.

For the full choose-the-store flow, tenant behavior, and action write-plane rules, read
[docs/secrets.md](secrets.md).

## Brain Cache

```bash
rc brain status
rc brain sync
rc bash list
```

Use after pushing a brain commit. `status` fetches `origin/main` and reports mounted SHA, origin SHA,
staleness, and sync time. `sync` fast-forwards the deployed cache when safe and expires warm `rc bash`
workspaces; the next `rc bash run` remounts the refreshed `/brain`. If sync reports manual reconcile,
use `brain-publish` with the status output.

## Author -> Verify Loop

Before changing a brain or action blindly:

1. Find real cases with `rc runs`, `rc fleet`, or `rc patterns`.
2. Inspect evidence with `rc run <id> --events` or `rc-debug`.
3. Edit the brain from evidence.
4. Verify locally with `local-brain-work`.
5. Verify production behavior with `rc ask --brain-ref dev/<branch>` when needed.
6. Finish with `brain-publish`.
