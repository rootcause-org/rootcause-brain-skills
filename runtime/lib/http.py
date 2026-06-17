"""Plain HTTP grounding helper.

A thin wrapper over ``requests`` for read-only HTTP grounding (e.g. a project's status page or
read API). Egress is governed at the container level (network policy), not here; this
just keeps calls terse and read-shaped. A ``GITHUB_READ_TOKEN`` (the sealed per-project mirror-read
PAT), when injected, is available for authenticated GitHub reads via ``github_get``.

Only read verbs are exposed — we never write to customer systems.
"""

import os

DEFAULT_TIMEOUT = 15


def get(url: str, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT):
    """HTTP GET. Raises for a 4xx/5xx so grounding code fails loudly rather than parsing an error body.

    ``requests`` is imported lazily so the whole ``lib`` package loads even where it isn't installed.
    """
    import requests

    resp = requests.get(url, headers=headers or {}, timeout=timeout)
    resp.raise_for_status()
    return resp


def get_json(url: str, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict | list:
    """HTTP GET returning parsed JSON."""
    return get(url, headers=headers, timeout=timeout).json()


def github_get(path: str, timeout: int = DEFAULT_TIMEOUT) -> dict | list:
    """Authenticated GitHub API GET (read scope), using ``GITHUB_READ_TOKEN`` when present.

    ``path`` may be a full URL or an API path like ``repos/org/repo/contents/x``.
    """
    url = path if path.startswith("http") else f"https://api.github.com/{path.lstrip('/')}"
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_READ_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return get_json(url, headers=headers, timeout=timeout)
