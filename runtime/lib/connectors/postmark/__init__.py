"""Postmark read-only connector.

Examples:
    from lib.connectors import postmark
    messages = postmark.outbound_messages(recipient="customer@example.com")

CLI:
    python -m lib.connectors.postmark outbound --recipient customer@example.com
    python -m lib.connectors.postmark outbound-detail MESSAGE_ID
    python -m lib.connectors.postmark suppressions outbound --email customer@example.com
"""

from __future__ import annotations

import argparse
from typing import Any
from urllib.parse import urlencode

from lib import http, oauth

API_BASE = "https://api.postmarkapp.com"


def _headers(token_key: str = "postmark") -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-Postmark-Server-Token": oauth.token(token_key),
    }


def _url(path: str, params: dict[str, Any] | None = None) -> str:
    clean = {key: value for key, value in (params or {}).items() if value not in (None, "")}
    suffix = f"?{urlencode(clean)}" if clean else ""
    return f"{API_BASE}/{path.lstrip('/')}{suffix}"


def outbound_messages(
    *,
    recipient: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    fromdate: str | None = None,
    todate: str | None = None,
    count: int = 50,
    offset: int = 0,
    token_key: str = "postmark",
) -> dict[str, Any]:
    """Search outbound messages in the server retention window."""
    return http.get_json(
        _url(
            "messages/outbound",
            {
                "recipient": recipient,
                "tag": tag,
                "status": status,
                "fromdate": fromdate,
                "todate": todate,
                "count": count,
                "offset": offset,
            },
        ),
        headers=_headers(token_key),
    )


def inbound_messages(
    *,
    recipient: str | None = None,
    status: str | None = None,
    fromdate: str | None = None,
    todate: str | None = None,
    count: int = 50,
    offset: int = 0,
    token_key: str = "postmark",
) -> dict[str, Any]:
    """Search inbound messages in the server retention window."""
    return http.get_json(
        _url(
            "messages/inbound",
            {
                "recipient": recipient,
                "status": status,
                "fromdate": fromdate,
                "todate": todate,
                "count": count,
                "offset": offset,
            },
        ),
        headers=_headers(token_key),
    )


def outbound_detail(message_id: str, *, token_key: str = "postmark") -> dict[str, Any]:
    """Fetch one outbound message details payload by Postmark MessageID."""
    mid = (message_id or "").strip()
    if not mid:
        raise RuntimeError("Postmark message_id is required")
    return http.get_json(_url(f"messages/outbound/{mid}/details"), headers=_headers(token_key))


def suppressions(
    stream_id: str,
    *,
    email: str | None = None,
    reason: str | None = None,
    origin: str | None = None,
    fromdate: str | None = None,
    todate: str | None = None,
    token_key: str = "postmark",
) -> dict[str, Any]:
    """Read suppression entries for one message stream."""
    stream = (stream_id or "").strip()
    if not stream:
        raise RuntimeError("Postmark stream_id is required")
    return http.get_json(
        _url(
            f"message-streams/{stream}/suppressions/dump",
            {
                "EmailAddress": email,
                "SuppressionReason": reason,
                "Origin": origin,
                "fromdate": fromdate,
                "todate": todate,
            },
        ),
        headers=_headers(token_key),
    )


def messages_to_markdown(payload: dict[str, Any], *, title: str = "Postmark messages") -> str:
    """Render a compact message-search response."""
    rows = payload.get("Messages") or []
    total = payload.get("TotalCount", len(rows))
    lines = [f"# {title} ({total})"]
    for row in rows[:25]:
        mid = row.get("MessageID") or "(no id)"
        subject = row.get("Subject") or "(no subject)"
        status = row.get("Status") or ""
        received = row.get("ReceivedAt") or ""
        recipients = ", ".join(row.get("Recipients") or [])
        lines.append(f"- {mid}: {status} {received} {recipients} — {subject}".strip())
    return "\n".join(lines)


def suppressions_to_markdown(payload: dict[str, Any]) -> str:
    """Render a suppression dump response."""
    rows = payload.get("Suppressions") or []
    lines = [f"# Postmark suppressions ({len(rows)})"]
    for row in rows[:50]:
        lines.append(
            "- {email}: {reason} ({origin}) {created}".format(
                email=row.get("EmailAddress") or "(no email)",
                reason=row.get("SuppressionReason") or "suppressed",
                origin=row.get("Origin") or "unknown",
                created=row.get("CreatedAt") or "",
            ).strip()
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.postmark")
    sub = parser.add_subparsers(dest="cmd", required=True)

    out = sub.add_parser("outbound", help="search outbound messages")
    out.add_argument("--recipient")
    out.add_argument("--tag")
    out.add_argument("--status")
    out.add_argument("--fromdate")
    out.add_argument("--todate")
    out.add_argument("--count", type=int, default=50)
    out.add_argument("--offset", type=int, default=0)

    inc = sub.add_parser("inbound", help="search inbound messages")
    inc.add_argument("--recipient")
    inc.add_argument("--status")
    inc.add_argument("--fromdate")
    inc.add_argument("--todate")
    inc.add_argument("--count", type=int, default=50)
    inc.add_argument("--offset", type=int, default=0)

    detail = sub.add_parser("outbound-detail", help="fetch outbound message details")
    detail.add_argument("message_id")

    supp = sub.add_parser("suppressions", help="read message-stream suppressions")
    supp.add_argument("stream_id")
    supp.add_argument("--email")
    supp.add_argument("--reason")
    supp.add_argument("--origin")
    supp.add_argument("--fromdate")
    supp.add_argument("--todate")

    args = parser.parse_args(argv)
    if args.cmd == "outbound":
        print(
            messages_to_markdown(
                outbound_messages(
                    recipient=args.recipient,
                    tag=args.tag,
                    status=args.status,
                    fromdate=args.fromdate,
                    todate=args.todate,
                    count=args.count,
                    offset=args.offset,
                ),
                title="Postmark outbound messages",
            )
        )
        return 0
    if args.cmd == "inbound":
        print(
            messages_to_markdown(
                inbound_messages(
                    recipient=args.recipient,
                    status=args.status,
                    fromdate=args.fromdate,
                    todate=args.todate,
                    count=args.count,
                    offset=args.offset,
                ),
                title="Postmark inbound messages",
            )
        )
        return 0
    if args.cmd == "outbound-detail":
        print(messages_to_markdown({"Messages": [outbound_detail(args.message_id)]}, title="Postmark message"))
        return 0
    if args.cmd == "suppressions":
        print(
            suppressions_to_markdown(
                suppressions(
                    args.stream_id,
                    email=args.email,
                    reason=args.reason,
                    origin=args.origin,
                    fromdate=args.fromdate,
                    todate=args.todate,
                )
            )
        )
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
