# Tenant projection — what `/brain` will look like

This is the short Local Brain Work view of tenant projection for an agent working inside a brain checkout. For
the broader model, read [docs/brain-model.md](../../docs/brain-model.md).

## The invariant

For a templated project brain, the model does **not** see the committed source tree. The host first
renders an ephemeral per-tenant view, then mounts that view read-only at `/brain`.

Committed source:

- `projection.yaml` — declares placeholders, branch selectors, variants, defaults, gated files.
- Markdown templates — contain `{{ placeholder }}` and `<!-- rc:branch -->` markers.
- `tenant.schema.json` — validates and renders the editing surface for tenant profile values.

Runtime input:

- Tenant profile record — the actual projection values in rootcause DB, under
  `config["dentai_settings"].settings`.

Runtime output:

- A throwaway directory: placeholders filled, branches collapsed, gated files kept/dropped.
- Mounted `:ro` at `/brain`.
- Never committed, never pushed, never treated as durable knowledge.

## What `tenant.schema.json` is

`tenant.schema.json` is **not** the values file. It is the schema and form/CLI metadata for the values
record.

Prod uses it today:

- rootcause loads `<brainRoot>/<project>/brain/tenant.schema.json` at request time.
- `rc project tenant profile schema` returns it.
- `rc project tenant profile set <slug> ...` validates the merged profile record against it.
- The operator Configuration form renders from it.
- A project with no schema has no tenant-settings surface.

So the file stays committed because it is the source-of-truth contract; the tenant-specific **values**
stay in the DB.

## What an author should inspect

When editing a templated project brain for a tenant, get three things in view:

```bash
sed -n '1,180p' projection.yaml
rc project tenant profile schema -o json
rc project tenant profile get <slug> -o json
```

The useful mental model is:

```text
templates + projection.yaml + tenant profile record
  -> deterministic projection compile
  -> resolved /brain view for that one tenant
```

If a placeholder appears in markdown, it must be declared in `projection.yaml` and backed by either a
tenant setting value or a projection default. A branch selector is matched as the exact string its value
renders as (`true`/`false` for a bool, the decimal form for an integral number); an unmatched value goes
through the branch `default`, and with no default the region is dropped.

Two variant names are **reserved**:

- `unset` — absent, null, or a **blank/whitespace-only** string.
- `present` — the catch-all for "the tenant filled this in", so a free-text key (a hand-maintained
  policy) can gate prose without enumerating values. `false` and `0` are values and count as present;
  only emptiness is `unset`. Exact-value variants are matched first, so `when=mollie` beats
  `when=present`. Pair `present` with an EMPTY `unset` body and the whole section — heading included —
  disappears for a tenant that never configured it, instead of reading a "(not configured)" default
  out loud.

```yaml
cancellation_policy: { select: cancellation_policy, variants: [present, unset], default: unset }
```

## Local preview helper

`brain_projection.py` is a **preview/audit helper**, not a new source of truth. Its job is to answer,
for one tenant: "what would production compile?"

```bash
uv run "$SKILL/scripts/brain_projection.py" --tenant <slug>
uv run "$SKILL/scripts/brain_projection.py" --tenant <slug> --write-summary
```

Good behavior:

- Read local `projection.yaml`.
- Fetch tenant profile values via `rc`.
- Print a concise markdown summary: settings version, branch choices, placeholder defaults used,
  gated files kept/dropped.
- With `--write-summary`, write only the summary and fetched settings snapshot under
  `.rootcause/projection/<tenant>/`, which is gitignored.

Bad behavior:

- Writing compiled files into the brain tree.
- Committing tenant-specific values.
- Teaching the model to choose variants itself.

## Server-compiled view

`rc dev brain render` (rc >= 1.17.0) returns the **compiled** projection from the server —
placeholders filled, `rc:branch` regions collapsed — exactly as `/brain` mounts it for that tenant,
with a header (sha, channel, filled/branches/degraded counts and degradations):

```bash
rc dev brain render --project <project> --tenant <slug> --path AGENTS.md
rc dev brain render --project <project> --tenant <slug> --all -o json
```

Pin `--sha <40-hex>` or `--channel stable|edge` as needed. Output artifacts are keyed by TENANT
(`.rootcause/output/brain-render-<tenant>/`): a second render of another sha/channel overwrites the
first, so move it aside if you want to compare two versions. Template-editing flow: author → `render`
for 1–2 tenants → `rc dev brain preflight` (pass/fail across all tenants) → publish. Requires a
project-level login (`brain:promote` scope).

For production confidence, use `rc ask --brain-ref dev/x ...` with an `rc auth login` bound to the target
tenant and inspect the dump; that shows the actual loop and the exact settings snapshot the run rendered
from.
