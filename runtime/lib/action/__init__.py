"""Hosted Python action harness and write-plane client helpers.

Action scripts run after human confirmation, in a one-shot executor that receives params and writes
its authoritative result through files. This package keeps those scripts focused on deterministic
orchestration while preserving the credential split: write clients resolve only ``RC_ACTION_*``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lib import api

_RESULT_ENV = "RC_ACTION_RESULT"
_PARAMS_ENV = "RC_ACTION_PARAMS"
_CONNECTIONS_ENV = "RC_ACTION_CONNECTIONS"
_NON_SECRET_ACTION_ENV = {
    "RC_ACTION_CONNECTIONS",
    "RC_ACTION_DRY_RUN",
    "RC_ACTION_PARAMS",
    "RC_ACTION_RESULT",
}
_EXCEPHOOK_INSTALLED = False


class ActionError(RuntimeError):
    """Handled hard failure: show ``message`` to the reviewer without a Python backtrace."""


@dataclass(frozen=True)
class FileParam:
    path: Path
    filename: str
    mime_type: str = ""
    size_bytes: int | None = None
    attachment_id: str = ""
    sha256: str = ""

    def open(self, mode: str = "rb"):
        return self.path.open(mode)

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()


class Params(Mapping[str, Any]):
    def __init__(self, values: Mapping[str, Any]):
        self._values = dict(values)

    def __getitem__(self, key: str) -> Any:
        try:
            return self._values[key]
        except KeyError as e:
            raise ActionError(f"missing required action param {key!r}") from e

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def file(self, key: str) -> FileParam:
        raw = self[key]
        if not isinstance(raw, Mapping):
            raise ActionError(f"action param {key!r} is not a file descriptor")
        path = raw.get("path") or raw.get("local_path") or raw.get("tmp_path")
        if not path:
            raise ActionError(f"file action param {key!r} is missing a path")
        return FileParam(
            path=Path(str(path)),
            filename=str(raw.get("filename") or raw.get("name") or Path(str(path)).name),
            mime_type=str(raw.get("mime_type") or raw.get("content_type") or ""),
            size_bytes=_maybe_int(raw.get("size_bytes") if "size_bytes" in raw else raw.get("size")),
            attachment_id=str(raw.get("attachment_id") or raw.get("id") or ""),
            sha256=str(raw.get("sha256") or ""),
        )


def params(argv: list[str] | None = None) -> Params:
    """Load action params from ``$RC_ACTION_PARAMS`` or local ``--params`` JSON."""
    install_excepthook()
    return Params(_load_params(argv))


def ok(summary_md: str, data: Mapping[str, Any] | None = None) -> None:
    rv = {"summary": summary_md}
    if data:
        rv.update(dict(data))
    _finish({"ok": True, "return_value": rv}, exit_code=0)


def fail(summary_md: str, data: Mapping[str, Any] | None = None) -> None:
    rv = {"ok": False, "summary": summary_md}
    if data:
        rv.update(dict(data))
    _finish({"ok": True, "return_value": rv}, exit_code=0)


def main(fn: Callable[[Params], Any]) -> Callable[[], Any]:
    """Decorator/runner for function-style actions.

    ``@action.main`` executes immediately when used in a ``__main__`` script. When imported by tests
    it returns a zero-arg runner that can be called directly.
    """

    def runner() -> Any:
        install_excepthook()
        try:
            p = params()
            value = fn(p)
            _finish(_coerce_result(value), exit_code=0)
        except SystemExit:
            raise
        except ActionError as e:
            _write_exception(e, include_backtrace=False)
            raise SystemExit(1) from e
        except BaseException as e:
            _write_exception(e, include_backtrace=True)
            raise SystemExit(1) from e
        return None

    if fn.__module__ == "__main__":
        runner()
    return runner


def client(capability: str, **kw) -> api.Client:
    """Build the one write-capable HTTP client, resolving only ``RC_ACTION_*`` credentials."""
    manifest = kw.pop("manifest", None)
    cap = capability.strip()
    if not cap:
        raise ActionError("action capability is required")
    env = _env_var(cap)
    token = os.environ.get(env)
    if not token:
        raise ActionError(_missing_capability_message(cap, env))
    base = _capability_base(cap)
    if manifest is None:
        api.load_manifests()
        manifest = api.MANIFESTS.get(base)
    if manifest is None:
        raise ActionError(f"capability {cap!r} uses unknown connector {base!r}; add a runtime manifest first")
    return api.Client(manifest=manifest, credential=token, allow_writes=True, **kw)


def dry_run(argv: list[str] | None = None) -> bool:
    """Local/action dry-run flag: ``--commit`` wins, then ``--dry-run``, then env."""
    args = sys.argv[1:] if argv is None else list(argv)
    if "--commit" in args:
        return False
    if "--dry-run" in args:
        return True
    return str(os.environ.get("RC_ACTION_DRY_RUN", "")).strip().lower() in {"1", "true", "yes", "on"}


def tenant() -> tuple[str, str]:
    return os.environ.get("RC_TENANT_ID", ""), os.environ.get("RC_TENANT_SLUG", "")


def require_tenant() -> tuple[str, str]:
    tid, slug = tenant()
    if not tid:
        raise ActionError("tenant scope is required for this action but RC_TENANT_ID is unset")
    return tid, slug


def install_excepthook() -> None:
    global _EXCEPHOOK_INSTALLED
    if _EXCEPHOOK_INSTALLED:
        return

    def hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, SystemExit):
            return
        _write_exception(exc, include_backtrace=not isinstance(exc, ActionError), tb=tb)

    sys.excepthook = hook
    _EXCEPHOOK_INSTALLED = True


def _load_params(argv: list[str] | None = None) -> dict[str, Any]:
    path = os.environ.get(_PARAMS_ENV)
    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise ActionError(f"failed to read ${_PARAMS_ENV}: {e}") from e
        return _parse_params(raw, f"${_PARAMS_ENV}")

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--params", default="")
    parsed, _ = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    if parsed.params:
        return _parse_params(parsed.params, "--params")
    raise ActionError(f"no action params found: set ${_PARAMS_ENV} or pass --params JSON")


def _parse_params(raw: str, source: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ActionError(f"invalid action params JSON from {source}: {e}") from e
    if not isinstance(obj, dict):
        raise ActionError(f"action params from {source} must be a JSON object")
    return obj


def _finish(result: Mapping[str, Any], *, exit_code: int) -> None:
    _write_result(dict(result))
    raise SystemExit(exit_code)


def _write_result(result: Mapping[str, Any]) -> None:
    redacted = _redact(result)
    text = json.dumps(redacted, default=str, ensure_ascii=False)
    result_path = os.environ.get(_RESULT_ENV)
    if result_path:
        Path(result_path).write_text(text, encoding="utf-8")
    print(text)


def _write_exception(exc: BaseException, *, include_backtrace: bool, tb=None) -> None:
    err = {"class": exc.__class__.__name__, "message": str(exc)}
    if include_backtrace:
        err["backtrace"] = "".join(traceback.format_exception(type(exc), exc, tb or exc.__traceback__))
    _write_result({"ok": False, "error": err})


def _coerce_result(value: Any) -> dict[str, Any]:
    if value is None:
        return {"ok": True, "return_value": {"summary": ""}}
    if isinstance(value, str):
        return {"ok": True, "return_value": {"summary": value}}
    if isinstance(value, Mapping):
        out = dict(value)
        if "return_value" in out or "error" in out or "stdout" in out:
            return out
        if "summary" in out:
            summary = str(out.pop("summary", ""))
            rv = {"summary": summary}
            rv.update(out)
            return {"ok": True, "return_value": rv}
        if "ok" in out:
            ok_value = bool(out.pop("ok"))
            rv = {"ok": ok_value, "summary": str(out.pop("summary", ""))}
            rv.update(out)
            return {"ok": True, "return_value": rv}
        summary = str(out.pop("summary", ""))
        rv = {"summary": summary}
        rv.update(out)
        return {"ok": True, "return_value": rv}
    return {"ok": True, "return_value": {"summary": "", "value": value}}


def _env_var(capability: str) -> str:
    base = _capability_base(capability)
    out = ["RC_ACTION_"]
    for ch in base.upper():
        out.append(ch if ("A" <= ch <= "Z" or "0" <= ch <= "9") else "_")
    return "".join(out)


def _capability_base(capability: str) -> str:
    return capability.strip().split(".", 1)[0]


def _missing_capability_message(capability: str, env: str) -> str:
    declared = _declared_connections()
    if _PARAMS_ENV not in os.environ:
        return (
            f"capability {capability!r} is not available: not running inside an action execution "
            f"(missing ${_PARAMS_ENV} and {env})"
        )
    if declared and capability not in declared:
        return (
            f"capability {capability!r} is not declared for this action; declared capabilities: "
            f"{', '.join(sorted(declared))}"
        )
    return (
        f"capability {capability!r} is not available: declare it under `connections:` in "
        "manifest.yaml and connect a write grant (label=actions) for this project"
    )


def _declared_connections() -> set[str]:
    raw = os.environ.get(_CONNECTIONS_ENV, "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _redact(value: Any) -> Any:
    secrets = _secret_values()
    return _redact_value(value, secrets)


def _secret_values() -> list[str]:
    vals: list[str] = []
    for key, value in os.environ.items():
        if not value or len(value) < 4:
            continue
        if key.startswith("RC_ACTION_") and key not in _NON_SECRET_ACTION_ENV:
            vals.append(value)
        elif key.endswith("_DSN") or "_WRITE" in key:
            vals.append(value)
    return sorted(set(vals), key=len, reverse=True)


def _redact_value(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        out = value
        for secret in secrets:
            out = out.replace(secret, "[redacted]")
        return out
    if isinstance(value, list):
        return [_redact_value(v, secrets) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(v, secrets) for v in value)
    if isinstance(value, dict):
        return {k: _redact_value(v, secrets) for k, v in value.items()}
    return value


def _maybe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ActionError",
    "FileParam",
    "Params",
    "client",
    "dry_run",
    "fail",
    "install_excepthook",
    "main",
    "ok",
    "params",
    "require_tenant",
    "tenant",
]
