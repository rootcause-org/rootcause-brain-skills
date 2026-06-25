# Tenant projection — what `/brain` will look like

This is the short brain-dev view of rootcause's tenant projection. The authoritative host docs live in
rootcause's `architecture/tenant-templating.md` and `features/tenant_settings.md`; this page is the
operational memory for an agent working inside a brain checkout.

## The invariant

For a templated project brain, the model does **not** see the committed source tree. The host first
renders an ephemeral per-tenant view, then mounts that view read-only at `/brain`.

Committed source:

- `projection.yaml` — declares placeholders, branch selectors, variants, defaults, gated files.
- Markdown templates — contain `{{ placeholder }}` and `<!-- rc:branch -->` markers.
- `tenant.schema.json` — validates and renders the editing surface for tenant settings.

Runtime input:

- Tenant settings record — the actual values in rootcause DB, under `config["dentai_settings"].settings`.

Runtime output:

- A throwaway directory: placeholders filled, branches collapsed, gated files kept/dropped.
- Mounted `:ro` at `/brain`.
- Never committed, never pushed, never treated as durable knowledge.

## What `tenant.schema.json` is

`tenant.schema.json` is **not** the values file. It is the schema and form/CLI metadata for the values
record.

Prod uses it today:

- rootcause loads `<brainRoot>/<project>/brain/tenant.schema.json` at request time.
- `GET /api/v1/tenants/settings/schema` returns it.
- `PATCH /api/v1/tenants/{slug}/settings` validates the merged settings record against it.
- The operator Configuration form renders from it.
- A project with no schema has no tenant-settings surface.

So the file stays committed because it is the source-of-truth contract; the tenant-specific **values**
stay in the DB.

## What an author should inspect

When editing a templated project brain for a tenant, get three things in view:

```bash
sed -n '1,180p' projection.yaml
rc tenant settings schema -o json
rc tenant settings get --tenant <slug> -o json
```

The useful mental model is:

```text
templates + projection.yaml + tenant settings record
  -> deterministic host compile
  -> resolved /brain view for that one tenant
```

If a placeholder appears in markdown, it must be declared in `projection.yaml` and backed by either a
tenant setting value or a projection default. If a branch selector is absent/null/`unset`, rootcause's
compiler routes it through the branch `default`.

## Local preview helper

`brain_projection.py` is a **preview/audit helper**, not a new source of truth. Its job is to answer,
for one tenant: "what would the host compile do?"

```bash
uv run "$SKILL/scripts/brain_projection.py" --tenant <slug>
uv run "$SKILL/scripts/brain_projection.py" --tenant <slug> --write-summary
```

Good behavior:

- Read local `projection.yaml`.
- Fetch tenant settings via `rc`.
- Print a concise markdown summary: settings version, branch choices, placeholder defaults used,
  gated files kept/dropped.
- With `--write-summary`, write only the summary and fetched settings snapshot under
  `.rootcause/projection/<tenant>/`, which is gitignored.

Bad behavior:

- Writing compiled files into the brain tree.
- Committing tenant-specific values.
- Teaching the model to choose variants itself.

For production confidence, use `rc ask --tenant <slug> --brain-ref dev/x ...` and inspect the dump; that
shows the actual loop and the exact settings snapshot the run rendered from.
