# Reading a tenant setting from Python

Applies to **templated project brains only** — the ones with `projection.yaml` + `tenant.schema.json`.
A flat project has no settings surface: `lib.tenant.get` just returns your fallback, so the same
script runs in both worlds. See [project and tenant brains](brain-model.md#project-and-tenant-brains).

In **any** Python a run or an action executes — a grounding script under `skills/*/scripts/`, or an
action's `script.src.py` / `preflight.py` / `policy.py` (`lib` is baked into the hosted harness too):

```python
from lib import tenant

hours = tenant.get("latecancel_min_hours", 48)   # fallback for an unset key / flat project
hours = tenant.require("latecancel_min_hours")   # raises when the script cannot degrade
```

Rules:

- Resolution is `/brain/tenant_profile.json` (the compiled view) → the injected `RC_TENANT_PROFILE_JSON`
  env document (all an action container gets — it mounts the raw clone) → `{}`. Malformed JSON from
  either source raises `TenantProfileError`.
- **Never let a playbook tell the model to copy a `{{ key }}` into a script argument or an action
  param.** The model copies wrong; placeholders are for prose the customer reads. A CLI flag may exist
  only as an explicit human/agent *override* of what `tenant.get` already reads.
- The key must exist in `tenant.schema.json`, and carry a default in `projection.yaml`, so `get` sees
  the tenant's *effective* value rather than the fallback.
- Test locally with `RC_TENANT_PROFILE_PATH=<file>`, or fetch a real tenant's document:
  `rc dev brain render --tenant <slug> --path tenant_profile.json` →
  `.rootcause/output/brain-render-<slug>/tree/tenant_profile.json`.
