"""Multi-field connector credentials.

Most connectors carry ONE secret, so ``RC_CONN_<KEY>`` holds the raw token and ``lib.oauth.token``
is enough. A few providers need several values that only make sense together (a Google refresh
token + its OAuth client pair; a Meta system-user token + the ad account it may read). Those declare
``token_fields`` in their manifest and the host injects a single JSON object into the same env slot:

    RC_CONN_META_ADS={"META_ACCESS_TOKEN":"...","META_AD_ACCOUNT_ID":"act_1"}

``fields()`` parses that object and, when the slot is absent, falls back to the individual env vars
by name — which keeps a developer's pre-existing local setup (and ``RC_INTEGRATION_SMOKE_*``, which
copies into the same slot) working unchanged.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence

from lib import oauth


def fields(key: str, names: Sequence[str] = ()) -> dict[str, str]:
    """Return the credential fields for ``key``.

    ``names`` is the manifest's declared field list; it is only needed for the individual-env-var
    fallback (the JSON object is self-describing). Values are returned verbatim — callers must never
    print them.
    """
    slot = oauth.env_var(key)
    raw = os.environ.get(slot, "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"connection {key!r} is configured but its credential is not a JSON object of "
                f"fields ({exc}); re-save the connection in ReplyPen"
            ) from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"connection {key!r} credential must be a JSON object of fields")
        return {str(k): "" if v is None else str(v) for k, v in parsed.items()}

    found = {n: os.environ[n] for n in names if os.environ.get(n)}
    if found:
        return found
    raise RuntimeError(f"connection not configured: {key} (no credential fields available)")


def require(values: dict[str, str], name: str, *, key: str = "") -> str:
    """Return ``values[name]`` or raise naming the MISSING FIELD (never its value)."""
    value = (values.get(name) or "").strip()
    if not value:
        where = f" for connection {key}" if key else ""
        raise RuntimeError(f"missing credential field {name}{where}")
    return value
