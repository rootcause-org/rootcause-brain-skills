"""Sentry read-only connector.

Example:
    from lib.connectors import sentry
    issue = sentry.fetch_issue("4500000000000000")

CLI:
    python -m lib.connectors.sentry issue 4500000000000000
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from lib import http, oauth

API_BASE = "https://sentry.io/api/0"


def fetch_issue(issue_id: str, *, api_base: str | None = None, token_key: str = "sentry") -> dict[str, Any]:
    """Fetch one Sentry issue by id using the injected read token."""
    issue = (issue_id or "").strip()
    if not issue:
        raise RuntimeError("issue id is required")
    base = (api_base or os.environ.get("SENTRY_API_BASE") or API_BASE).rstrip("/")
    tok = oauth.token(token_key)
    return http.get_json(
        f"{base}/issues/{issue}/",
        headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
    )


def issue_to_markdown(issue: dict[str, Any]) -> str:
    """Render a compact issue summary for support grounding."""
    title = issue.get("title") or issue.get("culprit") or issue.get("shortId") or "Sentry issue"
    lines = [f"# {title}"]
    for label, key in [
        ("Short ID", "shortId"),
        ("Status", "status"),
        ("Level", "level"),
        ("Users", "userCount"),
        ("Events", "count"),
        ("First seen", "firstSeen"),
        ("Last seen", "lastSeen"),
        ("Permalink", "permalink"),
    ]:
        value = issue.get(key)
        if value not in (None, ""):
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.sentry")
    sub = parser.add_subparsers(dest="cmd", required=True)
    issue = sub.add_parser("issue", help="fetch and render a Sentry issue")
    issue.add_argument("issue_id")
    args = parser.parse_args(argv)

    if args.cmd == "issue":
        print(issue_to_markdown(fetch_issue(args.issue_id)))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
