"""Automatic, best-effort PostHog error tracking for grounding scripts.

A grounding script runs inside the per-run container as `python …`; an uncaught exception
would otherwise vanish into the bash tool's stderr. This module ships those exceptions to
PostHog with the run's context (project/run/session/thread/model/release) so the operator can
triage them in the same place as host-side errors.

Three properties hold by contract:

- **Automatic.** `lib/__init__.py` calls `install()` at import time, so merely `from lib import db`
  wires the `sys.excepthook` + `atexit` flush — a script needs no telemetry code of its own.
- **Best-effort.** Every entry point swallows its own errors. Telemetry must NEVER break a
  grounding script: a PostHog hiccup, a bad import, a flush timeout — all silent.
- **No-op without a key.** With `POSTHOG_PROJECT_API_KEY` empty/unset (or `posthog` not installed),
  `install()` returns silently and `capture_exception`/`flush` do nothing. Local dev and tests stay
  offline by default.

Secrets are kept out of PostHog two ways: `capture_exception_code_variables = False` stops the SDK
attaching stack-frame locals (which can hold DSNs/keys), and a `before_send` scrub redacts any event
property whose key looks like a credential.
"""

import atexit
import os
import sys

try:
    import posthog
except Exception:  # noqa: BLE001 — posthog absent ⇒ full no-op, never break the import of lib
    posthog = None

_installed = False
_prev_excepthook = None

_DEFAULT_HOST = "https://eu.i.posthog.com"

# Substrings (case-insensitive) that mark a property key as credential-bearing → redact its value.
_SECRET_MARKERS = ("token", "secret", "api_key", "password", "authorization")

# RC_* env → PostHog property name. RC_PROJECT_ID is handled separately (distinct_id + group).
_CONTEXT_ENV = {
    "RC_RUN_ID": "run_id",
    "RC_SESSION_ID": "session_id",
    "RC_THREAD_ID": "thread_id",
    "RC_MODEL": "model",
    "RC_RELEASE": "release",
}


def _enabled() -> bool:
    return posthog is not None and bool(os.environ.get("POSTHOG_PROJECT_API_KEY"))


def _scrub(event):
    """`before_send` hook: redact credential-looking property values on every outbound event."""
    try:
        props = getattr(event, "properties", None)
        if isinstance(props, dict):
            for key in list(props):
                low = key.lower()
                if any(marker in low for marker in _SECRET_MARKERS):
                    props[key] = "[redacted]"
    except Exception:  # noqa: BLE001 — scrub failure must not drop the event-path silently break send
        pass
    return event


def _run_context():
    """Build (distinct_id, properties, groups) from the RC_* run env, skipping absent vars.

    Centralized so the excepthook and the public `capture_exception` ship identical context.
    """
    project_id = os.environ.get("RC_PROJECT_ID")
    distinct_id = project_id or "workspace"
    props = {"component": "workspace", "$exception_level": "error"}
    for env_name, prop in _CONTEXT_ENV.items():
        val = os.environ.get(env_name)
        if val:
            props[prop] = val
    groups = {"project": project_id} if project_id else None
    return distinct_id, props, groups


def _capture(exc, extra_props=None):
    if not _enabled() or exc is None:
        return
    try:
        distinct_id, props, groups = _run_context()
        if extra_props:
            props.update(extra_props)
        kwargs = {"distinct_id": distinct_id, "properties": props}
        if groups:
            kwargs["groups"] = groups
        posthog.capture_exception(exc, **kwargs)
    except Exception:  # noqa: BLE001 — best-effort: never let telemetry break the caller
        pass


def _excepthook(exc_type, exc_value, exc_tb):
    _capture(exc_value)
    try:
        flush()
    finally:
        if _prev_excepthook is not None:
            _prev_excepthook(exc_type, exc_value, exc_tb)
        else:
            sys.__excepthook__(exc_type, exc_value, exc_tb)


def install():
    """Wire PostHog error tracking once. Idempotent; a silent no-op without a key or SDK."""
    global _installed, _prev_excepthook
    if _installed:
        return
    _installed = True
    if not _enabled():
        return
    try:
        posthog.project_api_key = os.environ["POSTHOG_PROJECT_API_KEY"]
        posthog.host = os.environ.get("POSTHOG_HOST") or _DEFAULT_HOST
        posthog.capture_exception_code_variables = False
        posthog.before_send = _scrub
        _prev_excepthook = sys.excepthook
        sys.excepthook = _excepthook
        atexit.register(flush)
    except Exception:  # noqa: BLE001 — a half-wired client is still better than a broken script
        pass


def capture_exception(exc=None, **extra_props):
    """Explicitly report an exception with the run context (for use inside a try/except).

    `exc` defaults to the exception currently being handled. Best-effort; no-op when disabled.
    """
    if exc is None:
        exc = sys.exc_info()[1]
    _capture(exc, extra_props or None)


def flush():
    """Flush queued events (call before process exit, or they are lost). No-op when disabled."""
    if not _enabled():
        return
    try:
        posthog.flush()
    except Exception:  # noqa: BLE001 — best-effort
        pass
