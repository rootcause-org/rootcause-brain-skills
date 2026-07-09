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
rc dream evidence --limit 50 -o json
rc runs [--limit N] [--kind email|prompt|mcp|analysis] [--category ok|timeout|...]
rc status
rc fleet [--days N] [--kind ...]
rc patterns [--days N]
rc health [--hours N]
rc thread <id>
rc mailbox ls
rc mailbox harvest <mailbox-id> [--max-threads N] [--clean=true] [--wait]
# Future deep/local IMAP path; verify the command exists before relying on it.
rc mailbox imap-env <mailbox-id> --out .rootcause/imap/<mailbox-id>.env
rc export ls [-o json]
rc export get <export-id>
rc export download <export-id> [--out <file>] [--split <dir>]
rc config get
rc config set max_run_usd=5 default_tier=pro
rc config hierarchy get
rc config hierarchy set persona.guidance="..."
rc triage policy get
rc triage policy set "..."
rc triage rules ls
rc triage rules add effect=skip match_kind=subject_contains pattern="newsletter"
rc triage rules add effect=force_process match_kind=sender_domain pattern="example.com"
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
outside any brain                  -> default profile / built-in default (https://app.replypen.com)
base_url: ROOTCAUSE_BASE_URL > .rootcause.toml base_url > profile base_url > built-in default (https://app.replypen.com)
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

## Dream Evidence

```bash
rc dream evidence --limit 50 -o json
```

Use this for a local dream-cycle pass from a brain checkout. It returns two ranked planes: human
feedback on runs and sent-vs-proposed deltas. Drill only the runs that justify an edit with
`rc run <id> --debug`. Use [`brain-dream-cycle`](../skills/brain-dream-cycle/SKILL.md) for the full
brain/persona/triage decision workflow.

## Mailbox Harvest And Exports

```bash
rc mailbox ls -o json
rc mailbox harvest <mailbox-id> --max-threads 1000
rc export ls -o json
rc export get <export-id>
rc export download <export-id> --split .rootcause/exports/<export-id>/
```

`rc mailbox harvest` triggers a **production** provider sweep of a mailbox's sent history into a stored,
cleaned Markdown corpus; `--wait` polls to completion, `--max-threads` caps the sweep, `--clean=true`
(default) strips quoting/signatures. `rc export ls` answers "has this mailbox been harvested?" (id, kind,
status, thread_count, truncated, created/completed); `rc export get` shows one export's status. `rc export
download` fetches the corpus and marks it consumed — `--split <dir>` materializes it as
`<dir>/INDEX.md` + `<dir>/threads/<yyyy-mm>--<slug>--<idx>.md`. The default split dir
`.rootcause/exports/<export-id>/` sits under the wholesale-gitignored `.rootcause/` and MUST stay
gitignored: it is raw customer mail, never brain content.

Hosted IMAP harvest is shallow/smoke-only when the server applies the IMAP cap (currently 100 refs) and
returns a warning. Deep IMAP requires a local exporter path: `rc mailbox imap-env` to write a
gitignored secret env file, plus `scripts/local_imap_harvest.py` to produce the `.rootcause/exports/...`
tree. If either piece is absent, do not reveal credentials or use private stores; report the missing
public surface.

Use [`brain-harvest`](../skills/brain-harvest/SKILL.md) for the full acquire → cluster → distil →
verify → publish → delete workflow. See [docs/side-effects.md](side-effects.md) for the harvest/download
side effects before running either.

## Persona And Triage

```bash
rc config hierarchy get -o json
rc config hierarchy set persona.tone="..." persona.guidance="..."
rc triage policy get -o json
rc triage policy set "..."
rc triage rules ls -o json
rc triage rules add effect=skip match_kind=subject_contains pattern="newsletter" reason="marketing mail"
rc triage rules add effect=force_process match_kind=sender_domain pattern="example.com" reason="VIP support domain"
```

Persona settings own voice, language, formality, signature, and wording preferences. Triage policy/rules
own whether inbound mail deserves a draft. Product facts and runbooks belong in the brain.

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

## DB And Bash Examples

Discover before querying:

```bash
rc whoami -o json
rc db list -o json
rc db schema <db> --table <table> -o json
rc db schema <db> --table <table> -o json |
  jq -r '.. | objects | select(has("name") and has("type")) | [.name,.type] | @tsv'
```

Then run narrow read-only SQL:

```bash
rc db query <db> "select count(*) as row_count from <table>" -o json | jq '.rows[0]'
rc db query <db> "select id::text, created_at from <table> order by created_at desc limit 5" -o json
```

If a query fails with an unknown column, go back to `rc db schema`; do not keep guessing names. For
large result handling, keep SQL narrow and post-process JSON locally with `jq`.

Use `rc bash run` for workspace files and mounted context:

```bash
rc bash run 'find /brain -maxdepth 2 -type f | sed -n "1,80p"'
rc bash run 'find /kb -maxdepth 3 -type d -print | sed -n "1,120p"'
rc bash run 'rg -n -i "invoice|payment|refund" /kb /brain/knowledge -g "*.md" 2>/dev/null | sed -n "1,60p"'
```

For KB title/frontmatter indexes, see [knowledge-base.md](knowledge-base.md).

Prefer `rc db` and `rc bash` for debugging, tool parity, and "does this script/query work?" loops.
They run the production primitive directly and return faster than `rc ask`, which adds the LLM wrapper
and is best for full-loop behavior checks or ambiguous investigations.

## Author -> Verify Loop

Before changing a brain or action blindly:

1. Find real cases with `rc runs`, `rc fleet`, or `rc patterns`.
2. Inspect evidence with `rc run <id> --events` or `rc-debug`.
3. Edit the brain from evidence.
4. Verify locally with `local-brain-work`.
5. Verify production behavior with `rc ask --brain-ref dev/<branch>` when needed.
6. Finish with `brain-publish`.
