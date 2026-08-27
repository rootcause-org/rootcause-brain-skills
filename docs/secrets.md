# Secrets

Use public `rc` surfaces only. You need a login with secrets access for grounding env changes and
operator access for the action plane. Do not ask for RootCause host shell, SSM, registry DB access, or
private operator scripts.

## Choose The Store

- Catalog integration exists (`rc project connection ls` shows it, or RootCause docs name one): use
  `rc project connection add/rotate/reveal/rm`. Brain code should import the central connector or use
  `lib.oauth` by connector key.
- Custom read-only API key or cloud token needed by grounding scripts: use the grounding env with
  `rc project env set`. Normal runs receive this plane.
- New read-only database DSN: use the grounding env — sealing the key creates the database, annotating
  it surfaces it. See [Register A New Grounding Database](#register-a-new-grounding-database). Keep the
  raw env var name out of brain prose unless a script must reference it directly; the host-injected DB
  roster carries the database names and purposes.
- Hosted action write credential: use `rc project env set --plane action` only when you are an operator
  with the required access. This writes `.env.action`; normal diagnosis runs never receive it.

## Add Or Rotate A Grounding Secret

For non-DSN secrets, document the env var **name only** in the relevant brain skill and in
`AGENTS.md`'s non-DSN env table. Never commit or paste the value into the brain.

```bash
rc auth status
rc project env keys
printf %s "$SECRET_VALUE" | rc project env set key=FOO_API_TOKEN
rc project env keys
rc project env pull
rc project env diff
```

`rc project env set` reads the value from STDIN by default and never echoes it. Inline `value=...` works, but
puts the secret in shell history and process arguments; avoid it.

After adding the key, update the script to read `os.environ["FOO_API_TOKEN"]` (or the helper that expects
that name), run local checks, then verify production behavior with `rc ask --brain-ref dev/<branch>` when
needed.

## Register A New Grounding Database

There is no `rc project database add`. A database **exists** because a sealed grounding env key
`<PROJECT>_<DBKEY>_DSN` exists; it becomes **visible** in `rc project database ls` once you annotate it.
Those are two different steps — `ls` lists the DSNs the project has *configured* (description, scope
manifest, or PII columns), not the sealed env keys. `rc project database --help` (rc >= 1.18.1) states
the same convention.

Naming is load-bearing: `<PROJECT>_<DBKEY>_DSN`. `<DBKEY>` lowercased is the short name brain scripts
pass to `lib.db` (`ACME_BILLING_DSN` -> `lib.db.query(..., db="billing")`) and the `name` column of
`rc dev console database list`.

Prerequisites, both on the customer side:

- the DSN uses a **read-only** role — grounding is read-only by contract, `lib.db` never writes;
- the database host allows connections from the RootCause box (network / security-group allowlist). A
  DSN that only works from your laptop fails every production run.

`*_WRITE_DSN` is the action/write plane, not a grounding database: it never joins the roster and never
enters a run container. See [Tenant And Action Planes](#tenant-and-action-planes).

```bash
rc auth status
rc project database ls                                  # what is already configured

# 1. seal the DSN — this is what creates the database
printf %s "$DSN" | rc project env set key=ACME_BILLING_DSN

# 2. verify it physically resolves and holds the tables you expect (authoritative view)
rc dev console database list                            # expect name=billing
rc dev console database schema billing
rc dev console database query billing "select 1 as ok"

# 3. annotate it — this is what surfaces it in `rc project database ls`
rc project database set ACME_BILLING_DSN description="Invoices, subscriptions, payment state."
rc project database ls                                  # now includes ACME_BILLING_DSN
rc project database controls get ACME_BILLING_DSN       # pii + scope_manifest
```

`rc dev console database list` is the authoritative view of which DSNs actually exist and connect —
check there first when a database seems missing, not in `rc project database ls`. Verify from
production, not the laptop: grounding DSNs are usually IP-allowlisted to the box. See
[`prod-console`](../skills/prod-console/SKILL.md).

`description` is the only field `rc project database set` accepts, and it is what the production model
reads in its DB roster: one line naming the tables and purpose. Scope manifest and PII columns go
through `rc project database controls set`; those rules are operator-owned, so propose changes through a
RootCause support request ([support-boundary.md](support-boundary.md)).

4. Document the database in the brain's `skills/databases/` map (what it holds, which tables matter,
which script reads it) so the production model knows when to reach for it, and ship with `brain-publish`.

For local live checks, `rc project env pull` after sealing. `rc project env rm ACME_BILLING_DSN` removes
the DSN again.

## Delete Or Inspect

```bash
rc project env rm FOO_API_TOKEN
rc project env reveal FOO_API_TOKEN
```

`keys`, `pull`, `diff`, `set`, and `rm` do not print values. `reveal` intentionally prints one live
secret value for copy/pipe use and is audited by key name.

## Tenant And Action Planes

On tenant-enabled projects, bulk `rc project env keys/pull/diff` can use the active login's tenant or an explicit
`--tenant`. Per-key `set/rm/reveal` target a tenant env only when the OAuth token itself is tenant-bound;
`--tenant` does not retarget those collection writes.

Action-plane credentials are project-level and operator-only:

```bash
printf %s "$WRITE_SECRET" | rc project env set key=FOO_WRITE_TOKEN --plane action
rc project env reveal FOO_WRITE_TOKEN --plane action
```

Use this only for `actions/<id>/` credentials. If an action body also needs a read DSN at execution time,
that read DSN must exist in `.env.action` too, because the hosted action executor loads only the action
plane.
