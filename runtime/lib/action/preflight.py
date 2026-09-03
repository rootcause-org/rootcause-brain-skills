"""Small helpers for action preflight scripts."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

_PARAMS_ENV = "PREFLIGHT_PARAMS"


def params(argv: Sequence[str] | None = None) -> dict[str, Any]:
    """Load the preflight JSON object from ``$PREFLIGHT_PARAMS`` or local ``--params``."""
    raw = os.environ.get(_PARAMS_ENV)
    if raw is None:
        args = sys.argv[1:] if argv is None else list(argv)
        try:
            raw = args[args.index("--params") + 1]
        except (ValueError, IndexError) as exc:
            raise SystemExit(
                "preflight: no params — set $PREFLIGHT_PARAMS or pass --params '<json>'"
            ) from exc

    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("preflight params must be a JSON object")
    return value


def result(
    ok: bool,
    summary: str,
    reason: str = "",
    observed: Mapping[str, Any] | None = None,
    failure_class: str = "",
    resource_url: str = "",
) -> dict[str, Any]:
    """Build the host preflight verdict envelope, including only supplied optional fields."""
    out: dict[str, Any] = {"ok": bool(ok), "summary": summary, "reason": reason}
    if observed is not None:
        out["observed"] = dict(observed)
    if failure_class:
        out["class"] = failure_class
    if resource_url:
        out["resource_url"] = resource_url
    return out


__all__ = ["params", "result"]
