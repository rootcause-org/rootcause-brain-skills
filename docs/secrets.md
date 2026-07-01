# Secrets

Use public `rc` surfaces only. You need a login with secrets access for grounding env changes and
operator access for the action plane. Do not ask for RootCause host shell, SSM, registry DB access, or
private operator scripts.

## Choose The Store

- Catalog integration exists (`rc connection ls` shows it, or RootCause docs name one): use
  `rc connection add/rotate/reveal/rm`. Brain code should import the central connector or use
  `lib.oauth` by connector key.
- Custom read-only API key or cloud token needed by grounding scripts: use the grounding env with
  `rc env set`. Normal runs receive this plane.
- New read-only database DSN: use the grounding env, but keep the raw env var name out of brain prose
  unless a script must reference it directly; the host-injected DB roster should carry database names
  and purposes.
- Hosted action write credential: use `rc env set --plane action` only when you are an operator with the
  required access. This writes `.env.action`; normal diagnosis runs never receive it.

## Add Or Rotate A Grounding Secret

For non-DSN secrets, document the env var **name only** in the relevant brain skill and in
`AGENTS.md`'s non-DSN env table. Never commit or paste the value into the brain.

```bash
rc whoami
rc env keys
printf %s "$SECRET_VALUE" | rc env set key=FOO_API_TOKEN
rc env keys
rc env pull
rc env diff
```

`rc env set` reads the value from STDIN by default and never echoes it. Inline `value=...` works, but
puts the secret in shell history and process arguments; avoid it.

After adding the key, update the script to read `os.environ["FOO_API_TOKEN"]` (or the helper that expects
that name), run local checks, then verify production behavior with `rc ask --brain-ref dev/<branch>` when
needed.

## Delete Or Inspect

```bash
rc env rm FOO_API_TOKEN
rc env reveal FOO_API_TOKEN
```

`keys`, `pull`, `diff`, `set`, and `rm` do not print values. `reveal` intentionally prints one live
secret value for copy/pipe use and is audited by key name.

## Tenant And Action Planes

On tenant-enabled projects, bulk `rc env keys/pull/diff` can use the active login's tenant or an explicit
`--tenant`. Per-key `set/rm/reveal` target a tenant env only when the OAuth token itself is tenant-bound;
`--tenant` does not retarget those collection writes.

Action-plane credentials are project-level and operator-only:

```bash
printf %s "$WRITE_SECRET" | rc env set key=FOO_WRITE_TOKEN --plane action
rc env reveal FOO_WRITE_TOKEN --plane action
```

Use this only for `actions/<id>/` credentials. If an action body also needs a read DSN at execution time,
that read DSN must exist in `.env.action` too, because the hosted action executor loads only the action
plane.
