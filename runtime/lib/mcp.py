"""Remote-only (hosted HTTP) MCP client — the ``lib.api`` of Model Context Protocol servers.

Some vendors expose a hosted MCP endpoint instead of (or alongside) a REST surface. This module
lets a run call such a server's tools as read-only grounding, with the SAME posture as ``lib.api``:
timeouts always set, retry/backoff with full jitter at ONE layer, every failure normalized into one
loud ``McpError`` (never a silent empty), and the bearer taken from the injected env so it never
lands in argv, logs, or model context.

Transport is Streamable-HTTP / JSON-RPC 2.0: a single ``POST`` whose response is either one JSON
object or an SSE stream (``text/event-stream``) whose ``data:`` lines carry the JSON-RPC envelope.
We read both. This is remote-only by design — no local stdio server spawning (that would be an
arbitrary-process plane, out of scope for read-only grounding).

Cross-boundary env contract (the Go host injects these for a ``kind=mcp`` connection):
    bearer    RC_CONN_<KEY>_MCP
    endpoint  RC_CONN_<KEY>_MCP_URL   (resolved URL; absent ⇒ fall back to a static manifest
                                       ``mcp_url_template`` when it has no ``{…}`` placeholders)

CLI (concise markdown, like the connectors):
    python -m lib.mcp tools <key>
    python -m lib.mcp call <key> <method> [--params '{"name": "...", "arguments": {...}}']

``requests`` is imported lazily so ``from lib import mcp`` loads on a bare host (CLI ``--help`` works).
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from lib import _http_audit, api  # reuse the shared timing primitives + posture

# Mirror lib.api's timeout posture: both halves always set so a hung MCP server can't wedge a run.
DEFAULT_CONNECT_TIMEOUT = api.DEFAULT_CONNECT_TIMEOUT
DEFAULT_READ_TIMEOUT = api.DEFAULT_READ_TIMEOUT
DEFAULT_MAX_RETRIES = api.DEFAULT_MAX_RETRIES
DEFAULT_BACKOFF_BASE = api.DEFAULT_BACKOFF_BASE
DEFAULT_BACKOFF_CAP = api.DEFAULT_BACKOFF_CAP

# Transient HTTP statuses worth retrying — the POST itself is the JSON-RPC envelope, and a read
# (tools/list, tools/call against read-only tools) is idempotent enough to retry on a 5xx/429.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class McpError(RuntimeError):
    """One normalized exception for every MCP failure — transport non-2xx OR a JSON-RPC ``error``.

    Carries the JSON-RPC ``code`` (None for a pure transport error) and the server message/body so
    the caller sees the failing detail instead of a silent empty (mirrors ``api.ApiError``)."""

    def __init__(self, message: str, *, code: int | None = None, status: int | None = None, url: str = ""):
        self.code = code
        self.status = status
        self.url = url
        where = f" for {url}" if url else ""
        bits = []
        if status is not None:
            bits.append(f"HTTP {status}")
        if code is not None:
            bits.append(f"JSON-RPC {code}")
        prefix = ("[" + ", ".join(bits) + "] ") if bits else ""
        super().__init__(f"{prefix}MCP error{where}: {message}")


def _bearer_env(key: str) -> str:
    return f"{api_env_base(key)}_MCP"


def _url_env(key: str) -> str:
    return f"{api_env_base(key)}_MCP_URL"


def api_env_base(key: str) -> str:
    """``RC_CONN_<KEY>`` base for an MCP connection, reusing ``oauth.env_var`` slug rules so a key
    like ``linear`` maps to ``RC_CONN_LINEAR`` (then ``_MCP`` / ``_MCP_URL`` are appended)."""
    from lib import oauth

    return oauth.env_var(key)  # RC_CONN_<KEY>


def resolve_endpoint(key: str) -> str:
    """Resolve the MCP endpoint URL for ``key``.

    Primary: ``RC_CONN_<KEY>_MCP_URL`` (host-resolved). Fallback: a STATIC manifest
    ``mcp_url_template`` (one with no ``{…}`` placeholder — a templated one needs host resolution we
    don't have in-container). Raises loudly when neither is available."""
    env_url = os.environ.get(_url_env(key), "").strip()
    if env_url:
        return env_url
    tmpl = _manifest_mcp_url_template(key)
    if tmpl and "{" not in tmpl and "}" not in tmpl:
        return tmpl
    raise McpError(
        f"no MCP endpoint for {key!r}: set {_url_env(key)} (or a static manifest mcp_url_template)"
    )


def _manifest_mcp_url_template(key: str) -> str:
    """Read a connector manifest's ``mcp_url_template`` leniently; "" when absent/unparseable.

    lib.api's loader intentionally ignores this catalog-only field, so read the raw YAML here. Never
    raises — a missing/broken template just means "no static fallback"."""
    try:
        import yaml
        from pathlib import Path

        slug = key.lower().replace("rc_conn_", "").strip("_")
        path = Path(api.__file__).resolve().parent / "connectors" / slug / "manifest.yaml"
        if not path.exists():
            return ""
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return str(raw.get("mcp_url_template", "") or "") if isinstance(raw, dict) else ""
    except Exception:  # noqa: BLE001 — a static fallback is best-effort, never fatal
        return ""


@dataclass
class Client:
    """A configured MCP caller for one connection. Holds the resolved endpoint + bearer; retry,
    backoff, and rate-limit live HERE and nowhere else (retry at one layer only)."""

    key: str
    url: str
    bearer: str = ""
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_cap: float = DEFAULT_BACKOFF_CAP
    # Injected only so tests can pin timing/jitter; production uses the real clock + RNG.
    _sleeper: Callable[[float], None] = time.sleep
    _rng: random.Random = field(default_factory=lambda: random.Random())
    _id: int = 0

    def rpc(self, method: str, params: dict | None = None) -> Any:
        """Issue one JSON-RPC 2.0 request and return the ``result``.

        Raises ``McpError`` on a non-2xx (after retries), a malformed envelope, or a JSON-RPC
        ``error`` — never a silent empty."""
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        headers = {
            "Content-Type": "application/json",
            # Streamable-HTTP: the server may answer with one JSON object OR an SSE stream.
            "Accept": "application/json, text/event-stream",
        }
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"

        attempt = 0
        reason = "initial"
        while True:
            resp = _http_audit.request(
                "POST",
                self.url,
                json_body=payload,
                headers=headers,
                timeout=(self.connect_timeout, self.read_timeout),
                attempt=attempt + 1,
                reason=reason,
                known_secrets=(self.bearer,),
            )
            if 200 <= resp.status_code < 300:
                return self._unwrap(self._read_envelope(resp))
            if resp.status_code in _RETRYABLE_STATUS and attempt < self.max_retries:
                self._sleep_before_retry(resp, attempt)
                reason = f"retry_status_{resp.status_code}"
                attempt += 1
                continue
            raise McpError(_truncate(_body_text(resp)), status=resp.status_code, url=self.url)

    def _sleep_before_retry(self, resp, attempt: int) -> None:
        delay = None
        if resp.status_code == 429:
            delay = api.parse_retry_after(resp.headers.get("Retry-After"))
            if delay is not None:
                delay = min(delay, api.MAX_RETRY_AFTER)
        if delay is None:
            delay = api._full_jitter(attempt, self.backoff_base, self.backoff_cap, self._rng)
        api._sleep(delay, self._sleeper)

    def _read_envelope(self, resp) -> dict:
        """Parse the response body into a JSON-RPC envelope, accepting both a single JSON object and
        an SSE (``text/event-stream``) body whose ``data:`` line(s) carry the envelope."""
        ctype = (resp.headers.get("Content-Type") or "").lower()
        text = _body_text(resp)
        if "text/event-stream" in ctype:
            env = _parse_sse_jsonrpc(text)
            if env is None:
                raise McpError("SSE response carried no JSON-RPC data frame", url=self.url)
            return env
        try:
            env = resp.json()
        except ValueError:
            # Some servers send SSE bytes without the matching content-type — try SSE as a fallback.
            env = _parse_sse_jsonrpc(text)
            if env is None:
                raise McpError(f"non-JSON response body: {_truncate(text)}", url=self.url)
        if not isinstance(env, dict):
            raise McpError(f"unexpected JSON-RPC envelope (not an object): {_truncate(text)}", url=self.url)
        return env

    def _unwrap(self, env: dict) -> Any:
        """Return ``result`` or raise the JSON-RPC ``error`` as a normalized ``McpError``."""
        if "error" in env and env["error"] is not None:
            err = env["error"]
            if isinstance(err, dict):
                raise McpError(str(err.get("message", err)), code=err.get("code"), url=self.url)
            raise McpError(str(err), url=self.url)
        if "result" not in env:
            raise McpError(f"JSON-RPC envelope had neither result nor error: {_truncate(json.dumps(env))}", url=self.url)
        return env["result"]


def client(key: str, *, url: str | None = None, **kw) -> Client:
    """Build a ``Client`` from the injected env contract.

    Bearer ← ``RC_CONN_<KEY>_MCP`` (optional — some hosted endpoints are pre-authed by URL); endpoint
    ← ``url`` arg, else ``RC_CONN_<KEY>_MCP_URL``, else a static manifest ``mcp_url_template``."""
    endpoint = url or resolve_endpoint(key)
    bearer = os.environ.get(_bearer_env(key), "").strip()
    return Client(key=key, url=endpoint, bearer=bearer, **kw)


def call(key: str, method: str, params: dict | None = None, *, url: str | None = None) -> Any:
    """Invoke an MCP method (default ``tools/call``) and return the parsed result.

    ``method`` is taken verbatim so a caller can hit any JSON-RPC method; for the common case pass a
    tool name via ``params={"name": "...", "arguments": {...}}`` with ``method="tools/call"``."""
    return client(key, url=url).rpc(method, params)


def tools(key: str, *, url: str | None = None) -> list:
    """List the server's tools (``tools/list``). Returns the ``tools`` array."""
    result = client(key, url=url).rpc("tools/list")
    if isinstance(result, dict):
        return result.get("tools", []) or []
    return result or []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _body_text(resp) -> str:
    try:
        return resp.text
    except Exception:  # noqa: BLE001 — never let error-rendering itself raise
        return ""


def _truncate(text: str, limit: int = 800) -> str:
    t = (text or "").strip()
    return t[:limit] + "…(truncated)" if len(t) > limit else t


def _parse_sse_jsonrpc(text: str) -> dict | None:
    """Pull the JSON-RPC envelope out of an SSE body. Concatenates ``data:`` lines per the SSE spec
    and returns the LAST parseable JSON object (the response frame). None when none parse."""
    if not text:
        return None
    found: dict | None = None
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal found
        if not data_lines:
            return
        blob = "\n".join(data_lines)
        try:
            obj = json.loads(blob)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            found = obj

    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
        elif line.strip() == "":
            flush()
            data_lines = []
    flush()
    return found


def _tools_to_markdown(key: str, tool_list: list) -> str:
    lines = [f"# MCP tools: {key}"]
    if not tool_list:
        lines.append("- (no tools advertised)")
        return "\n".join(lines)
    for t in tool_list:
        if not isinstance(t, dict):
            lines.append(f"- {t}")
            continue
        name = t.get("name", "?")
        desc = (t.get("description") or "").strip().splitlines()
        first = desc[0] if desc else ""
        lines.append(f"- **{name}**" + (f" — {first}" if first else ""))
    return "\n".join(lines)


def _result_to_markdown(key: str, method: str, result: Any) -> str:
    """Render a call result as concise markdown. MCP ``tools/call`` results carry a ``content`` array
    of typed blocks; surface text blocks plainly and dump the rest as JSON."""
    lines = [f"# MCP {method}: {key}"]
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        for block in result["content"]:
            if isinstance(block, dict) and block.get("type") == "text":
                lines.append(block.get("text", ""))
            else:
                lines.append("```json")
                lines.append(json.dumps(block, indent=2, default=str))
                lines.append("```")
        if result.get("isError"):
            lines.append("\n> server marked this result as an error (isError=true)")
        return "\n".join(lines)
    lines.append("```json")
    lines.append(json.dumps(result, indent=2, default=str))
    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI: python -m lib.mcp tools <key> | call <key> <method> [--params '{...}']
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m lib.mcp", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tools", help="list a remote MCP server's tools (tools/list)")
    t.add_argument("key", help="connection key (RC_CONN_<KEY>_MCP / _MCP_URL must be injected)")

    c = sub.add_parser("call", help="invoke an MCP method (default tools/call) and print the result")
    c.add_argument("key")
    c.add_argument("method", help="JSON-RPC method, e.g. tools/call")
    c.add_argument("--params", default="", help="JSON object of params (e.g. tool name + arguments)")

    args = p.parse_args(argv)
    if args.cmd == "tools":
        print(_tools_to_markdown(args.key, tools(args.key)))
        return 0
    if args.cmd == "call":
        params = None
        if args.params:
            try:
                params = json.loads(args.params)
            except ValueError as e:
                p.error(f"--params is not valid JSON: {e}")
        print(_result_to_markdown(args.key, args.method, call(args.key, args.method, params)))
        return 0
    p.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
