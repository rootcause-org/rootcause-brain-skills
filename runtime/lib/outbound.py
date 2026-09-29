"""Credential-free outbound email/SMS proposals for trusted chat runs.

The host owns mailbox and SMS credentials, recipient validation and the send itself. This module only
calls the per-run ``http://rc-broker.internal/outbound/*`` surface. ``propose`` sends NOTHING: it
freezes one email text and/or one SMS text for a recipient list and the host renders a card with a
Send button; only the user's click sends. When the capability is absent it fails with one sentence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lib import _http_audit

BASE_URL = "http://rc-broker.internal/outbound"
TIMEOUT = (10.0, 60.0)


class OutboundError(RuntimeError):
    pass


class OutboundUnavailable(OutboundError):
    pass


def channels() -> dict:
    """Which channels can send in this scope: ``{"email": {...}, "sms": {...}}``."""
    return _request("GET", "/channels")


def propose(recipients: list[dict], *, email_subject: str = "", email_body_markdown: str = "",
            sms_body: str = "", mailbox: str | None = None) -> dict:
    """Propose one batch. ``recipients``: ``[{"key", "name"?, "email"?, "phone"?}]`` (≤50)."""
    body: dict = {"recipients": recipients}
    if email_body_markdown:
        body["email_subject"] = email_subject
        body["email_body_markdown"] = email_body_markdown
    if sms_body:
        body["sms_body"] = sms_body
    if mailbox:
        body["mailbox"] = mailbox
    return _request("POST", "/propose", body)


def status(batch_id: str) -> dict:
    """The batch's current card projection, including per-recipient outcomes after Send."""
    return _request("GET", "/batches/" + batch_id, template="/outbound/batches/{id}")


def _request(method: str, path: str, body: dict | None = None, template: str | None = None) -> dict:
    import requests

    try:
        response = _http_audit.request(
            method, BASE_URL + path, json_body=body, timeout=TIMEOUT,
            endpoint_template=template or "/outbound" + path,
        )
    except requests.exceptions.RequestException as exc:
        raise OutboundUnavailable("Outbound messaging is not enabled for this run") from exc
    if not 200 <= response.status_code < 300:
        sentence = response.text.strip() or f"Outbound request failed (HTTP {response.status_code})"
        raise OutboundError(f"HTTP {response.status_code}: {sentence}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OutboundError("Outbound broker returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise OutboundError("Outbound broker returned an invalid response")
    return payload


def _read(path: str | None) -> str:
    return Path(path).read_text(encoding="utf-8") if path else ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lib.outbound")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("channels")
    prop = sub.add_parser("propose")
    prop.add_argument("--recipients-file", required=True, help='JSON list: [{"key","name","email","phone"}]')
    prop.add_argument("--email-subject", default="")
    prop.add_argument("--email-body-file")
    prop.add_argument("--sms-body-file")
    prop.add_argument("--mailbox")
    st = sub.add_parser("status")
    st.add_argument("batch_id")
    return parser


def _main(argv=None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "channels":
            value = channels()
        elif args.command == "propose":
            recipients = json.loads(Path(args.recipients_file).read_text(encoding="utf-8"))
            if not isinstance(recipients, list):
                raise OutboundError("--recipients-file must hold a JSON list")
            value = propose(recipients, email_subject=args.email_subject,
                            email_body_markdown=_read(args.email_body_file).strip(),
                            sms_body=_read(args.sms_body_file).strip(), mailbox=args.mailbox)
        else:
            value = status(args.batch_id)
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
        return 0
    except (OutboundError, OSError, ValueError) as exc:
        parser.exit(1, f"outbound: {exc}\n")


if __name__ == "__main__":
    _main()
