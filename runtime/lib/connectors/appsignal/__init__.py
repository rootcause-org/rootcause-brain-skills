"""AppSignal read-only connector.

Examples:
    from lib.connectors import appsignal
    samples = appsignal.error_samples("5114f7e38c5ce90000000011", limit=5)

CLI:
    python -m lib.connectors.appsignal errors 5114f7e38c5ce90000000011 --limit 5
    python -m lib.connectors.appsignal sample 5114f7e38c5ce90000000011 SAMPLE_ID
"""

from __future__ import annotations

import argparse
from typing import Any
from urllib.parse import urlencode

from lib import http, oauth

API_BASE = "https://appsignal.com/api"


def _url(path: str, params: dict[str, Any] | None = None, *, token_key: str = "appsignal") -> str:
    query = {"token": oauth.token(token_key)}
    for key, value in (params or {}).items():
        if value not in (None, ""):
            query[key] = value
    return f"{API_BASE}/{path.lstrip('/')}?{urlencode(query)}"


def error_samples(
    app_id: str,
    *,
    action_id: str | None = None,
    exception: str | None = None,
    since: int | str | None = None,
    before: int | str | None = None,
    limit: int = 10,
    token_key: str = "appsignal",
) -> dict[str, Any]:
    """List recent AppSignal error samples for one application."""
    app = (app_id or "").strip()
    if not app:
        raise RuntimeError("AppSignal app_id is required")
    return http.get_json(
        _url(
            f"{app}/samples/errors.json",
            {
                "action_id": action_id,
                "exception": exception,
                "since": since,
                "before": before,
                "limit": limit,
            },
            token_key=token_key,
        )
    )


def sample(app_id: str, sample_id: str, *, token_key: str = "appsignal") -> dict[str, Any]:
    """Fetch one sanitized AppSignal sample id."""
    app = (app_id or "").strip()
    sample_ref = (sample_id or "").strip()
    if not app:
        raise RuntimeError("AppSignal app_id is required")
    if not sample_ref:
        raise RuntimeError("AppSignal sample_id is required")
    return http.get_json(_url(f"{app}/samples/{sample_ref}.json", token_key=token_key))


def sample_to_markdown(entry: dict[str, Any]) -> str:
    """Render a compact sample summary for support grounding."""
    exc = entry.get("exception") or {}
    title = exc.get("name") or entry.get("action") or entry.get("id") or "AppSignal sample"
    lines = [f"# {title}"]
    for label, key in [
        ("ID", "id"),
        ("Action", "action"),
        ("Path", "path"),
        ("Status", "status"),
        ("Time", "time"),
        ("Hostname", "hostname"),
    ]:
        value = entry.get(key)
        if value not in (None, ""):
            lines.append(f"- {label}: {value}")
    if exc.get("message"):
        lines.append(f"- Message: {exc['message']}")
    backtrace = exc.get("backtrace") or []
    if backtrace:
        lines.append("")
        lines.append("## Backtrace")
        lines.extend(f"- {line}" for line in backtrace[:12])
    return "\n".join(lines)


def samples_to_markdown(payload: dict[str, Any]) -> str:
    """Render an AppSignal samples index response."""
    rows = payload.get("log_entries") or payload.get("samples") or []
    total = payload.get("count", len(rows))
    lines = [f"# AppSignal error samples ({total})"]
    for row in rows[:20]:
        exc = row.get("exception") or {}
        ident = row.get("id") or "(no id)"
        summary = exc.get("name") or row.get("action") or row.get("path") or "sample"
        lines.append(f"- {ident}: {summary}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.appsignal")
    sub = parser.add_subparsers(dest="cmd", required=True)

    errors = sub.add_parser("errors", help="list and render AppSignal error samples")
    errors.add_argument("app_id")
    errors.add_argument("--action-id")
    errors.add_argument("--exception")
    errors.add_argument("--since")
    errors.add_argument("--before")
    errors.add_argument("--limit", type=int, default=10)

    one = sub.add_parser("sample", help="fetch and render one AppSignal sample")
    one.add_argument("app_id")
    one.add_argument("sample_id")

    args = parser.parse_args(argv)
    if args.cmd == "errors":
        print(
            samples_to_markdown(
                error_samples(
                    args.app_id,
                    action_id=args.action_id,
                    exception=args.exception,
                    since=args.since,
                    before=args.before,
                    limit=args.limit,
                )
            )
        )
        return 0
    if args.cmd == "sample":
        print(sample_to_markdown(sample(args.app_id, args.sample_id)))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
