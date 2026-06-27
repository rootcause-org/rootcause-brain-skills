"""Injected outbound-connection token helper.

Connection credentials are established host-side and injected into a run as env vars such as
``RC_CONN_SENTRY``. This module keeps connector code from spelling env-var plumbing everywhere.
"""

import os
import re


def env_var(key: str) -> str:
    """Return the connection env var for ``key``.

    ``key`` may be a connector key (``sentry``) or an explicit ``RC_CONN_*`` env var name.
    """
    k = (key or "").strip()
    if not k:
        raise RuntimeError("connection key is required")
    if k.startswith("RC_CONN_"):
        return k
    slug = re.sub(r"[^A-Za-z0-9]+", "_", k).strip("_").upper()
    if not slug:
        raise RuntimeError(f"connection key {key!r} does not map to an env var")
    return f"RC_CONN_{slug}"


def token(key: str) -> str:
    """Return the injected access token/PAT for a connector.

    Raises loudly when absent so grounding scripts fail with the exact missing connection instead of
    making anonymous API calls and parsing provider-specific auth errors.
    """
    name = env_var(key)
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"connection token not configured: {name}")
    return value
