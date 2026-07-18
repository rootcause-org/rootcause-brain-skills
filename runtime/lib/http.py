"""Plain HTTP grounding helper.

A thin wrapper over ``requests`` for read-only HTTP grounding (e.g. a project's status page or
read API). Egress is governed at the container level (network policy), not here; this
just keeps calls terse and read-shaped. A ``GITHUB_READ_TOKEN`` (the sealed per-project mirror-read
PAT), when injected, is available for authenticated GitHub reads via ``github_get``.

Ordinary helpers expose only GET. ``action_request`` is the explicit write-capable variant and asserts
the hosted-action harness env is present; action scripts use it when no catalogued
``lib.action.client`` manifest fits.
"""

import os

from lib import _http_audit

DEFAULT_TIMEOUT = 15
_ACTION_CONTEXT_ENVS = ("RC_ACTION_PARAMS", "RC_ACTION_RESULT")


def get(
    url: str,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    *,
    endpoint_template: str | None = None,
):
    """HTTP GET. Raises for a 4xx/5xx so grounding code fails loudly rather than parsing an error body.

    ``requests`` is imported lazily so the whole ``lib`` package loads even where it isn't installed.
    """
    resp = _http_audit.request(
        "GET",
        url,
        headers=headers or {},
        timeout=timeout,
        endpoint_template=endpoint_template,
    )
    resp.raise_for_status()
    return resp


def get_json(
    url: str,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    *,
    endpoint_template: str | None = None,
) -> dict | list:
    """HTTP GET returning parsed JSON."""
    return get(url, headers=headers, timeout=timeout, endpoint_template=endpoint_template).json()


def github_get(path: str, timeout: int = DEFAULT_TIMEOUT) -> dict | list:
    """Authenticated GitHub API GET (read scope), using ``GITHUB_READ_TOKEN`` when present.

    ``path`` may be a full URL or an API path like ``repos/org/repo/contents/x``.
    """
    url = path if path.startswith("http") else f"https://api.github.com/{path.lstrip('/')}"
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_READ_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return get_json(url, headers=headers, timeout=timeout, endpoint_template=path)


def action_request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    json=None,
    data=None,
    files: dict | None = None,
    timeout=DEFAULT_TIMEOUT,
    endpoint_template: str | None = None,
    attempt: int = 1,
    reason: str = "initial",
    redact_values: tuple[str, ...] | list[str] = (),
):
    """One audited action-plane HTTP attempt, returning the raw ``requests.Response``.

    Both action harness files must be present as a misuse check. This is not an authorization gate
    (a process can set env vars); credential isolation and the egress gateway remain the security
    boundary. It does not retry writes; callers may make another explicit attempt and supply
    ``attempt``/``reason`` only when their provider/idempotency contract makes that safe.
    """
    missing = [name for name in _ACTION_CONTEXT_ENVS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "lib.http.action_request is available only inside a hosted action "
            f"(missing {', '.join(missing)})"
        )
    return _http_audit.request(
        method,
        url,
        params=params,
        headers=headers,
        json_body=json,
        data=data,
        files=files,
        timeout=timeout,
        endpoint_template=endpoint_template,
        attempt=attempt,
        reason=reason,
        known_secrets=redact_values,
    )
