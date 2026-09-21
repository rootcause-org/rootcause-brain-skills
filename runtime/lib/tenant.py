"""The tenant's effective onboarding-profile values, read by the script that needs them.

On a templated project the host compiles a per-tenant `/brain` view and drops the tenant's effective
profile alongside it as `/brain/tenant_profile.json` (`{"values": {<key>: <typed JSON value>}}`) —
the stored value where the tenant set a well-typed one, the `projection.yaml` default otherwise. So a
grounding script reads its own settings instead of a playbook telling the model to copy a rendered
`{{ latecancel_min_hours }}` into an argument: the model cannot mistype what it never handles, and a
profile change takes effect without re-teaching the prose.

A flat (non-templated) project simply has no profile file — `profile()` is `{}` and every `get` falls
back, so the same script runs in both worlds. Malformed JSON is loud instead: that is a compiled-view
bug, not a missing setting.

Tests/local runs point at a fixture with ``RC_TENANT_PROFILE_PATH``.

CLI:

    python -m lib.tenant                       # all values as JSON
    python -m lib.tenant get latecancel_min_hours
    python -m lib.tenant get free_count --default 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

DEFAULT_PATH = "/brain/tenant_profile.json"


class TenantProfileError(RuntimeError):
    """The tenant profile is unreadable, or a required key is missing from it."""


_cache: dict[str, dict[str, Any]] = {}


def _path() -> str:
    return os.environ.get("RC_TENANT_PROFILE_PATH", "").strip() or DEFAULT_PATH


def profile() -> dict[str, Any]:
    """Return the tenant's effective profile values (a copy); `{}` when the project has no profile."""
    path = _path()
    if path not in _cache:
        _cache[path] = _load(path)
    return dict(_cache[path])


def _load(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise TenantProfileError(f"tenant profile {path} is unreadable: {exc}") from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise TenantProfileError(f"tenant profile {path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise TenantProfileError(f"tenant profile {path} must be a JSON object, got {type(document).__name__}")
    values = document.get("values", {})
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise TenantProfileError(f"tenant profile {path} has a non-object \"values\" key ({type(values).__name__})")
    return values


def has_profile() -> bool:
    """True when this run got a tenant profile with at least one value."""
    return bool(profile())


def get(key: str, default: Any = None) -> Any:
    """Return one profile value, or `default` when the tenant has no value for it."""
    value = profile().get(key)
    return default if value is None else value


def require(key: str) -> Any:
    """Return one profile value; raise when it is absent (a script that cannot degrade)."""
    value = profile().get(key)
    if value is None:
        raise TenantProfileError(
            f"tenant profile has no value for {key!r} "
            f"(known keys: {', '.join(sorted(profile())) or 'none'}; source: {_path()})"
        )
    return value


def _render(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.tenant", description="Read the tenant's effective profile values")
    parser.add_argument("--list", action="store_true", help="print every value as JSON (the default)")
    commands = parser.add_subparsers(dest="command")
    get_parser = commands.add_parser("get", help="print one value")
    get_parser.add_argument("key")
    get_parser.add_argument("--default", dest="default", default=None, help="printed when the tenant has no value")
    args = parser.parse_args(argv)
    try:
        if args.command != "get":
            print(json.dumps(profile(), indent=2, sort_keys=True))
            return 0
        value = get(args.key, args.default)
        if value is None:
            print(f"tenant profile has no value for {args.key!r} (source: {_path()})", file=sys.stderr)
            return 1
        print(_render(value))
        return 0
    except TenantProfileError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
