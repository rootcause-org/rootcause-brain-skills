"""Secret-safe audited HTTP transport shared by the runtime HTTP helpers.

Every network attempt emits one ``RC_HTTP_AUDIT `` JSON line on stderr. The host treats the line as
untrusted: it allowlists fields and supplies run/project/action correlation from its own execution
context. Audit emission is deliberately fail-open so observability can never change request behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Mapping
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

AUDIT_PREFIX = "RC_HTTP_AUDIT "

_MAX_REQUEST_BODY_BYTES = 16 * 1024
_SECRET_ENV_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "API_KEY",
    "AUTHORIZATION",
    "CREDENTIAL",
    "COOKIE",
    "DSN",
)
_SECRET_KEY_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "client_key",
)
_PII_KEY_MARKERS = (
    "email",
    "phone",
    "mobile",
    "first_name",
    "firstname",
    "last_name",
    "lastname",
    "full_name",
    "address",
    "postal",
    "postcode",
    "birth",
    "iban",
    "card_number",
    "querystring",
    "patient",
    "customer",
    "person",
    "member",
    "user_id",
    "account_id",
    "appointment",
    "slot_id",
    "subject",
    "title",
    "description",
    "note",
    "message",
    "query",
    "filter",
    "search",
    "subdivision",
    "type_appointment",
    "label_id",
)
_PII_KEY_EXACT = frozenset(
    {
        "id",
        "ids",
        "q",
        "ref",
        "reference",
        "params",
        "variables",
        "arguments",
        "input",
        "start",
        "end",
        "date",
        "time",
        "timestamp",
    }
)
_SAFE_RC_ACTION_ENVS = frozenset(
    {
        "RC_ACTION_CONNECTIONS",
        "RC_ACTION_DRY_RUN",
        "RC_ACTION_ID",
        "RC_ACTION_PARAMS",
        "RC_ACTION_RESULT",
        "RC_ACTION_RUN_ID",
    }
)
_UUID_SEGMENT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_LONG_OPAQUE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_VERSION_SEGMENT = re.compile(r"^(?:v\d+(?:\.\d+)*|\d{4}-\d{2}(?:-\d{2})?)$", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]{6,})?\b")
_BEARER = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")


def request(
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    json_body: Any | None = None,
    data: Any | None = None,
    files: Mapping[str, Any] | None = None,
    timeout: Any = None,
    attempt: int = 1,
    reason: str = "initial",
    endpoint_template: str | None = None,
    audit_url: str | None = None,
    known_secrets: tuple[str, ...] | list[str] = (),
    sender: Callable[..., Any] | None = None,
):
    """Send one HTTP attempt and emit its allowlisted audit event.

    ``audit_url`` is the provider URL when ``url`` is an internal broker URL. Query strings and
    headers never enter the event. ``endpoint_template`` lets connector code replace concrete path
    params; the fallback masks obviously dynamic path segments.
    """
    import requests

    request_id = str(uuid.uuid4())
    req_headers = dict(headers or {})
    # Correlation belongs to this exact attempt; callers cannot preserve/spoof an earlier value.
    req_headers["X-Request-ID"] = request_id
    try:
        payload, request_body = _canonical_payload(json_body=json_body, data=data, files=files)
    except BaseException:
        # Request handling remains authoritative. An exotic body must not fail merely because its
        # audit representation could not be canonicalized.
        payload, request_body = b"", {"_omitted": "canonicalization_failed"}
    target = audit_url or url
    started = time.monotonic()
    status_code = None
    event_reason = reason or "initial"
    try:
        send = sender or requests.request
        response = send(
            method.upper(),
            url,
            params=params,
            headers=req_headers,
            json=json_body,
            data=data,
            files=files,
            timeout=timeout,
        )
        status_code = int(response.status_code)
        return response
    except BaseException as exc:
        # Exception messages often contain credential-bearing URLs. The class is enough to explain
        # why there is no status while the ordinary caller retains its pre-existing exception.
        event_reason = f"{event_reason}:transport_{type(exc).__name__}"
        raise
    finally:
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        emit_prepared_attempt(
            method=method,
            url=target,
            payload=payload,
            request_body=request_body,
            status_code=status_code,
            duration_ms=duration_ms,
            attempt=attempt,
            reason=event_reason,
            request_id=request_id,
            endpoint_template=endpoint_template,
            known_secrets=known_secrets,
        )


def emit_prepared_attempt(
    *,
    method: str,
    url: str,
    payload: bytes,
    request_body: Any,
    status_code: int | None,
    duration_ms: int,
    attempt: int,
    reason: str,
    request_id: str,
    endpoint_template: str | None = None,
    known_secrets: tuple[str, ...] | list[str] = (),
) -> None:
    """Emit an attempt already sent by an SDK-owned transport (for example botocore).

    SDK adapters must add ``request_id`` to ``X-Request-ID`` before sending. This function keeps
    their stderr contract/redaction identical to the requests-backed primitive.
    """
    try:
        secrets = _known_secret_values(known_secrets)
        _emit(
            {
                "method": method.upper(),
                "endpoint": _endpoint(url, endpoint_template, secrets),
                "host": (urlsplit(url).hostname or "").lower(),
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "request_body": _bounded_redacted_body(request_body, secrets, payload),
                "status_code": status_code,
                "duration_ms": max(0, int(duration_ms)),
                "attempt": max(1, int(attempt)),
                "reason": reason or "initial",
                "request_id": request_id,
                "bytes": len(payload),
            }
        )
    except BaseException:
        # Audit is evidence, never request/SDK control flow.
        return


def _canonical_payload(*, json_body: Any, data: Any, files: Mapping[str, Any] | None) -> tuple[bytes, Any]:
    """Return deterministic audit bytes plus a body-shaped value safe to pass through redaction.

    JSON is canonicalized by sorted keys and compact separators. Form and multipart payloads use a
    stable metadata representation; file contents are represented by size/hash, never copied into
    ``request_body``. The request itself still receives the caller's original values.
    """
    if json_body is not None:
        payload = json.dumps(
            json_body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        return payload, json_body

    if files:
        form = _form_value(data)
        file_meta: dict[str, Any] = {}
        for name in sorted(files):
            file_meta[str(name)] = _file_metadata(files[name])
        body = {"form": form, "files": file_meta}
        payload = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return payload, body

    if data is None:
        return b"", None
    if isinstance(data, bytes):
        return data, "[binary body omitted]"
    if isinstance(data, bytearray):
        raw = bytes(data)
        return raw, "[binary body omitted]"
    if isinstance(data, memoryview):
        raw = data.tobytes()
        return raw, "[binary body omitted]"
    if isinstance(data, str):
        return data.encode("utf-8"), data
    if isinstance(data, Mapping):
        normalized = sorted((str(k), v) for k, v in data.items())
        encoded = urlencode(normalized, doseq=True).encode("utf-8")
        return encoded, dict(normalized)
    # Streams/iterables cannot be consumed for auditing without changing the request. Record only a
    # stable type marker; callers needing exact hashes should pass bytes.
    marker = f"[stream body omitted: {type(data).__name__}]"
    return marker.encode("utf-8"), marker


def _form_value(data: Any) -> Any:
    if data is None:
        return None
    if isinstance(data, Mapping):
        return {str(k): v for k, v in sorted(data.items(), key=lambda item: str(item[0]))}
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray, memoryview)):
        return "[binary form body omitted]"
    return f"[form body omitted: {type(data).__name__}]"


def _file_metadata(value: Any) -> dict[str, Any]:
    filename = ""
    content = value
    content_type = ""
    if isinstance(value, tuple):
        if value:
            filename = str(value[0] or "")
        if len(value) > 1:
            content = value[1]
        if len(value) > 2:
            content_type = str(value[2] or "")
    meta: dict[str, Any] = {"filename": _safe_filename(filename), "content_type": content_type}
    raw = _file_bytes_without_consuming(content)
    if raw is not None:
        meta["bytes"] = len(raw)
        meta["sha256"] = hashlib.sha256(raw).hexdigest()
    else:
        meta["bytes"] = None
        meta["sha256"] = ""
    return meta


def _file_bytes_without_consuming(value: Any) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    return None


def _safe_filename(filename: str) -> str:
    if not filename:
        return ""
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return f"[redacted].{suffix}" if suffix and re.fullmatch(r"[a-z0-9]{1,10}", suffix) else "[redacted]"


def _known_secret_values(extra: tuple[str, ...] | list[str]) -> list[str]:
    values = {str(v) for v in extra if isinstance(v, str) and len(v) >= 4}
    for key, value in os.environ.items():
        upper = key.upper()
        secret_env = any(marker in upper for marker in _SECRET_ENV_MARKERS)
        action_secret = upper.startswith("RC_ACTION_") and upper not in _SAFE_RC_ACTION_ENVS
        connection_secret = upper.startswith("RC_CONN_") and upper not in {
            "RC_CONNECTIONS",
            "RC_CONN_KEYS",
        }
        if (secret_env or action_secret or connection_secret) and len(value) >= 4:
            values.add(value)
    # Longest first prevents a shorter credential substring from leaving a revealing remainder.
    return sorted(values, key=len, reverse=True)


def _bounded_redacted_body(value: Any, secrets: list[str], payload: bytes) -> Any:
    redacted = _redact(value, secrets)
    try:
        encoded = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    except BaseException:
        return {"_omitted": "redaction_failed", "sha256": hashlib.sha256(payload).hexdigest()}
    if len(encoded) <= _MAX_REQUEST_BODY_BYTES:
        return redacted
    return {
        "_omitted": "body_too_large",
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _redact(value: Any, secrets: list[str], *, key: str = "") -> Any:
    low_key = key.lower()
    if (
        low_key in _PII_KEY_EXACT
        or low_key.endswith(("_at", "_date", "_time", "_timestamp"))
        or any(marker in low_key for marker in _SECRET_KEY_MARKERS + _PII_KEY_MARKERS)
    ):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {str(k): _redact(v, secrets, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, secrets) for v in value]
    if isinstance(value, str):
        out = value
        for secret in secrets:
            out = out.replace(secret, "[redacted]")
        out = _JWT.sub("[redacted]", out)
        out = _BEARER.sub("[redacted]", out)
        return _EMAIL.sub("[redacted]", out)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[binary body omitted]"
    return value


def _endpoint(url: str, template: str | None, secrets: list[str]) -> str:
    raw = template if template is not None else (urlsplit(url).path or "/")
    if "://" in raw:
        raw = urlsplit(raw).path or "/"
    raw = raw.split("?", 1)[0].split("#", 1)[0] or "/"
    for secret in secrets:
        raw = raw.replace(secret, "{redacted}")
    parts = []
    for segment in raw.split("/"):
        if not segment:
            parts.append(segment)
            continue
        if _dynamic_segment(segment):
            parts.append("{param}")
        else:
            parts.append(segment)
    result = "/".join(parts)
    return result if result.startswith("/") else "/" + result


def _dynamic_segment(segment: str) -> bool:
    if (segment.startswith("{") and segment.endswith("}")) or segment.startswith(":"):
        return False
    if "[redacted]" in segment or "{redacted}" in segment:
        return True
    if _VERSION_SEGMENT.fullmatch(segment):
        return False
    if _UUID_SEGMENT.fullmatch(segment) or _LONG_OPAQUE_SEGMENT.fullmatch(segment):
        return True
    if any(ch.isdigit() for ch in segment) or "%" in segment or "@" in segment:
        return True
    return False


def _emit(event: dict[str, Any]) -> None:
    try:
        line = json.dumps(event, separators=(",", ":"), ensure_ascii=False, sort_keys=True, default=str)
        print(AUDIT_PREFIX + line, file=sys.stderr, flush=True)
    except BaseException:
        # Audit is evidence, never request control flow.
        return
