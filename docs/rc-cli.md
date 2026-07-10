# `rc` CLI

`rc` is the public CLI over RootCause's JSON API. In a brain checkout, it uses `.rootcause.toml` plus
your `rc auth login` OAuth token to scope commands to the project/tenant. Use it instead of private
RootCause source or host tooling.

If a capability is not exposed through `rc`/API, use [`brain-publish`](../skills/brain-publish/SKILL.md)
to prepare a support request.

## Commands

```bash
rc ask "<customer-style question>"
rc ask "<direct investigation>" --scenario raw
rc ask "<q>" --brain-ref dev/<branch>
rc ask "<q>" --effort pro
rc run show <id>
rc run events <id>
rc run brain-diff <id>
rc run debug <id>
rc run trace <id> -o json
rc dev learning evidence --limit 50 -o json
rc run list [--limit N] [--kind email|prompt|mcp|analysis] [--category ok|timeout|...]
rc status
rc fleet runs [--days N] [--kind ...]
rc fleet patterns [--days N]
rc fleet health [--hours N]
rc run thread <id>
rc project mailbox ls
rc project mailbox harvest <mailbox-id> [--max-threads N] [--clean=true] [--wait]
rc project mailbox imap-env <mailbox-id> --out .rootcause/imap/<mailbox-id>.env
rc project corpus ls [-o json]
rc project corpus get <export-id>
rc project corpus download <export-id> [--out <file>] [--split <dir>]
rc project settings runtime get
rc project settings runtime set max_run_usd=5 default_tier=pro
rc project settings behavior get
rc project settings behavior set persona.guidance="..."
rc project triage policy get
rc project triage policy set "..."
rc project triage rules ls
rc project triage rules add effect=skip match_kind=subject_contains pattern="newsletter"
rc project triage rules add effect=force_process match_kind=sender_domain pattern="example.com"
rc dev brain status
rc dev brain sync
rc project env keys
rc project env pull
rc project env diff
printf %s "$SECRET_VALUE" | rc project env set key=FOO_API_TOKEN
rc project env rm FOO_API_TOKEN
rc project env reveal FOO_API_TOKEN
rc auth login
rc auth status
rc self update
```

API-backed commands support `-o json` for scripting.

## Upgrade

```bash
rc self update            # non-Homebrew installs; Homebrew installs print the brew command
rc self update --check    # report only
brew update && brew upgrade rc  # Homebrew install
```

The current CLI no longer has the top-level `rc upgrade` command. If an older installed client does not
recognize `rc self update`, run its legacy `rc upgrade` once (or use Homebrew) to reach the current
command surface.

## Auth And Scope

`rc` is OAuth-only. `rc auth login` stores tokens under `~/.config/rootcause/tokens.json` with mode `0600`.
There is no API key file for this kit.

In a brain checkout, `.rootcause.toml` carries the non-secret project binding. Legacy `base_url` values
in brain/profile/token files no longer steer commands. Resolution:

```text
explicit --profile <name>          -> that profile's stored token
inside a brain + project token     -> profile named by .rootcause.toml project
inside a brain + no project token  -> default profile + .rootcause.toml project as ?project=
outside any brain                  -> default profile / built-in default (https://app.replypen.com)
base_url: ROOTCAUSE_BASE_URL > built-in production (https://app.replypen.com)
```

Use `rc auth status` to confirm profile and login-bound project/tenant; use `rc auth access` to inspect
token capabilities. On tenant-enabled projects, a tenant-pinned token scopes commands automatically.
A project-pinned login must pass `--tenant <slug>` to workspace-producing commands such as `rc ask`.
The global `--tenant` flag scopes supported runtime and collection commands; it is not the identifier
argument for editing a tenant record. Tenant settings and projection profiles take the slug positionally:

```bash
rc project tenant settings get <slug>
rc project tenant settings set <slug> persona.tone=direct
rc project tenant profile get <slug>
rc project tenant profile set <slug> key=value
```

Unsupported project/tenant selectors fail locally instead of being silently ignored. Per-key
`rc project env set/rm/reveal` only reaches a tenant env when the OAuth token itself is tenant-bound;
`--tenant` does not retarget those writes.

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
rc run debug <id>
```

This writes `.rootcause/debug/<run8>-<project>.{md,jsonl}` by default. Read the markdown index first, then query JSONL
with `jq`. Use [`rc-debug`](../skills/rc-debug/SKILL.md) for the analysis-first workflow and
[docs/run-trace-model.md](run-trace-model.md) for the mental model.

## Dream Evidence

```bash
rc dev learning evidence --limit 50 -o json
```

Use this for a local dream-cycle pass from a brain checkout. It returns two ranked planes: human
feedback on runs and sent-vs-proposed deltas. Drill only the runs that justify an edit with
`rc run debug <id>`. Use [`brain-dream-cycle`](../skills/brain-dream-cycle/SKILL.md) for the full
brain/persona/triage decision workflow.

## Mailbox Harvest And Exports

```bash
rc project mailbox ls -o json
rc project mailbox harvest <mailbox-id> --max-threads 1000
rc project corpus ls -o json
rc project corpus get <export-id>
rc project corpus download <export-id> --split .rootcause/exports/<export-id>/
```

`rc project mailbox harvest` triggers a **production** provider sweep of a mailbox's sent history into a stored,
cleaned Markdown corpus; `--wait` polls to completion, `--max-threads` caps the sweep, `--clean=true`
(default) strips quoting/signatures. `rc project corpus ls` answers "has this mailbox been harvested?"
(id, kind, status, thread_count, truncated, created/completed); `rc project corpus get` shows one
export's status. `rc project corpus download` fetches the corpus and marks it consumed — `--split
<dir>` materializes it as
`<dir>/INDEX.md` + `<dir>/threads/<yyyy-mm>--<slug>--<idx>.md`. The default split dir
`.rootcause/exports/<export-id>/` sits under the wholesale-gitignored `.rootcause/` and MUST stay
gitignored: it is raw customer mail, never brain content.

Hosted IMAP harvest is shallow/smoke-only when the server applies the IMAP cap (currently 100 refs) and
returns a warning. Deep IMAP uses `rc project mailbox imap-env` to write a gitignored secret env file, then the
`brain-harvest` script `$SKILL/scripts/local_imap_harvest.py` to produce the
`.rootcause/exports/...` tree. The exporter v1 reads the sent folder with safe caps and groups messages
locally; it does not full-expand every referenced inbound message across folders yet.

Use [`brain-harvest`](../skills/brain-harvest/SKILL.md) for the full acquire → cluster → distil →
verify → publish → delete workflow. See [docs/side-effects.md](side-effects.md) for the harvest/download
side effects before running either.

## Persona And Triage

```bash
rc project settings behavior get -o json
rc project settings behavior set persona.tone="..." persona.guidance="..."
rc project triage policy get -o json
rc project triage policy set "..."
rc project triage rules ls -o json
rc project triage rules add effect=skip match_kind=subject_contains pattern="newsletter" reason="marketing mail"
rc project triage rules add effect=force_process match_kind=sender_domain pattern="example.com" reason="VIP support domain"
```

Persona settings own voice, language, formality, signature, and wording preferences. Triage policy/rules
own whether inbound mail deserves a draft. Product facts and runbooks belong in the brain.

## Env

```bash
rc project env keys
rc project env pull
rc project env diff
printf %s "$SECRET_VALUE" | rc project env set key=FOO_API_TOKEN
rc project env rm FOO_API_TOKEN
rc project env reveal FOO_API_TOKEN
```

`rc project env pull` writes the production grounding `.env` to the brain root with mode `0600`; values
are not printed. `rc project env set` adds or rotates one grounding secret, reading the value from STDIN
by default and never echoing it. `rc project env reveal` is the deliberate exception: it prints one live value for copy/pipe use
and is audited. The file is gitignored and contains real secrets.

For the full choose-the-store flow, tenant behavior, and action write-plane rules, read
[docs/secrets.md](secrets.md).

## Brain Cache

```bash
rc dev brain status
rc dev brain sync
rc dev console bash list
```

Use after pushing a brain commit. `status` fetches `origin/main` and reports mounted SHA, origin SHA,
staleness, and sync time. `sync` fast-forwards the deployed cache when safe and expires warm console
workspaces; the next `rc dev console bash run` remounts the refreshed `/brain`. If sync reports manual reconcile,
use `brain-publish` with the status output.

## DB And Bash Examples

Discover before querying:

```bash
rc auth status -o json
rc dev console database list -o json
rc dev console database schema <db> --table <table> -o json
rc dev console database schema <db> --table <table> -o json |
  jq -r '.. | objects | select(has("name") and has("type")) | [.name,.type] | @tsv'
```

Then run narrow read-only SQL:

```bash
rc dev console database query <db> "select count(*) as row_count from <table>" -o json | jq '.rows[0]'
rc dev console database query <db> "select id::text, created_at from <table> order by created_at desc limit 5" -o json
```

If a query fails with an unknown column, go back to `rc dev console database schema`; do not keep guessing names. For
large result handling, keep SQL narrow and post-process JSON locally with `jq`.

Use `rc dev console bash run` for workspace files and mounted context:

```bash
rc dev console bash run 'find /brain -maxdepth 2 -type f | sed -n "1,80p"'
rc dev console bash run 'find /kb -maxdepth 3 -type d -print | sed -n "1,120p"'
rc dev console bash run 'rg -n -i "invoice|payment|refund" /kb /brain/knowledge -g "*.md" 2>/dev/null | sed -n "1,60p"'
```

For KB title/frontmatter indexes, see [knowledge-base.md](knowledge-base.md).

Prefer `rc dev console database` and `rc dev console bash` for debugging, tool parity, and "does this script/query work?" loops.
They run the production primitive directly and return faster than `rc ask`, which adds the LLM wrapper
and is best for full-loop behavior checks or ambiguous investigations.

## Author -> Verify Loop

Before changing a brain or action blindly:

1. Find real cases with `rc run list`, `rc fleet runs`, or `rc fleet patterns`.
2. Inspect evidence with `rc run events <id>` or `rc-debug`.
3. Edit the brain from evidence.
4. Verify locally with `local-brain-work`.
5. Verify production behavior with `rc ask --brain-ref dev/<branch>` when needed.
6. Finish with `brain-publish`.
