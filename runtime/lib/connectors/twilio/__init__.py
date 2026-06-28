"""Twilio support connector — messages, calls, and phone numbers.

Force-code trigger (d): Twilio paginates via ``next_page_uri`` in the JSON body (a relative path
to follow verbatim — e.g. ``/2010-04-01/Accounts/AC.../Messages.json?Page=1&PageToken=...``).
None of the generic lib.api pagination styles can express this: ``cursor`` sends a token as a query
param; ``link`` reads an RFC 8288 ``Link:`` response header. So the script follows ``next_page_uri``
directly via ``_twilio_pages()``.

Auth is HTTP Basic with ``AccountSid:AuthToken`` as the credential value (injected as
``RC_CONN_TWILIO``). The AccountSid (``AC…`` prefix) is extracted from the credential to build
per-account API paths — every Twilio resource lives under
``/2010-04-01/Accounts/{AccountSid}/…``.

Read-only: only GETs. Never writes to the customer's Twilio account.

CLI:
    python -m lib.connectors.twilio messages [--to +1555...] [--from +1555...] [--limit 20]
    python -m lib.connectors.twilio calls    [--to +1555...] [--status completed] [--limit 20]
    python -m lib.connectors.twilio numbers  [--limit 20]
"""

from __future__ import annotations

import argparse
from typing import Any, Iterator

from lib import api, oauth

BASE = "https://api.twilio.com"

# Manifest: basic auth, single-page mode (pagination is script-driven). Registered so that
# `python -m lib.api get twilio ...` also works for direct path access.
MANIFEST = api.register(
    api.Manifest(
        key="twilio",
        base_url=BASE + "/2010-04-01",
        auth=api.Auth(strategy="basic"),
        pagination=api.Pagination(style="none"),
        rate_limit_remaining_header="",
    )
)


def _credential() -> str:
    """Return the raw ``AccountSid:AuthToken`` credential string from the injected env var."""
    return oauth.token("twilio")


def _account_sid() -> str:
    """Extract the AccountSid (``AC…`` prefix) from the credential.

    The injected ``RC_CONN_TWILIO`` value must be ``{AccountSid}:{AuthToken}``. We parse the SID
    here so the script can build per-account paths without the caller ever supplying it separately.
    Raises loudly with a clear message if the credential is missing or malformed.
    """
    cred = _credential()
    sid, _, _ = cred.partition(":")
    if not sid.startswith("AC"):
        raise RuntimeError(
            "RC_CONN_TWILIO must be 'AccountSid:AuthToken' — AccountSid starts with 'AC'"
        )
    return sid


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="twilio")


def _account_path(resource: str, sid: str) -> str:
    """Build a per-account resource path relative to base_url, e.g. ``Accounts/AC.../Messages.json``.

    base_url is ``https://api.twilio.com/2010-04-01``; _join appends this path to produce the full
    URL. No leading slash — _join strips it off the path, so both forms work, but keeping it
    prefix-free makes the join clean and avoids double-segment issues.
    """
    return f"Accounts/{sid}/{resource}.json"


def _twilio_pages(
    path: str,
    *,
    query: dict[str, Any] | None = None,
    items_key: str,
    max_pages: int = 20,
) -> Iterator[list[dict]]:
    """Yield pages of items, following Twilio's ``next_page_uri`` in the JSON body.

    Twilio envelopes list results like::

        {
          "messages": [...],
          "next_page_uri": "/2010-04-01/Accounts/AC.../Messages.json?Page=1&PageToken=...",
          ...
        }

    The ``next_page_uri`` is a relative path; we follow it by issuing a GET against the full host
    (``api.twilio.com``). This is the force-code trigger (d): none of the generic pagination styles
    can drive a JSON-body next-path.
    """
    c = _client()
    q = dict(query or {})
    seen = 0

    # First page: use the caller-supplied path + query.
    body = c.get(path, query=q)
    items = body.get(items_key) or []
    yield items
    seen += 1

    # Subsequent pages: follow next_page_uri from the response body.
    while seen < max_pages:
        next_uri = body.get("next_page_uri")
        if not next_uri:
            break
        # next_page_uri is a path like /2010-04-01/…; make it absolute and GET it directly.
        next_url = BASE + next_uri
        body = c.get(next_url)  # _join handles absolute URL pass-through
        items = body.get(items_key) or []
        yield items
        seen += 1


def list_messages(
    account_sid: str,
    *,
    to: str | None = None,
    from_: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List SMS/MMS messages, newest first, up to ``limit``.

    Filters: ``to`` / ``from_`` (E.164 phone number), ``status``
    (queued|sent|delivered|failed|undelivered|receiving|received|accepted|scheduled|read|partially_delivered|canceled).
    """
    path = _account_path("Messages", account_sid)
    q: dict[str, Any] = {"PageSize": min(limit, 100)}
    if to:
        q["To"] = to
    if from_:
        q["From"] = from_
    if status:
        q["Status"] = status
    out: list[dict] = []
    for page in _twilio_pages(path, query=q, items_key="messages"):
        out.extend(page)
        if len(out) >= limit:
            break
    return out[:limit]


def list_calls(
    account_sid: str,
    *,
    to: str | None = None,
    from_: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List call records, newest first, up to ``limit``.

    Filters: ``to`` / ``from_`` (E.164), ``status``
    (queued|ringing|in-progress|completed|busy|failed|no-answer|canceled).
    """
    path = _account_path("Calls", account_sid)
    q: dict[str, Any] = {"PageSize": min(limit, 100)}
    if to:
        q["To"] = to
    if from_:
        q["From"] = from_
    if status:
        q["Status"] = status
    out: list[dict] = []
    for page in _twilio_pages(path, query=q, items_key="calls"):
        out.extend(page)
        if len(out) >= limit:
            break
    return out[:limit]


def list_numbers(account_sid: str, *, limit: int = 50) -> list[dict]:
    """List provisioned incoming phone numbers."""
    path = _account_path("IncomingPhoneNumbers", account_sid)
    out: list[dict] = []
    for page in _twilio_pages(path, query={"PageSize": min(limit, 100)}, items_key="incoming_phone_numbers"):
        out.extend(page)
        if len(out) >= limit:
            break
    return out[:limit]


# -- Field pre-selection (support-relevant only) -------------------------------

_MESSAGE_FIELDS = "sid,to,from,status,direction,date_sent,body,num_segments,error_code,error_message"
_CALL_FIELDS = "sid,to,from,status,direction,start_time,duration,price,price_unit"
_NUMBER_FIELDS = "sid,phone_number,friendly_name,status,capabilities"


def _render_messages(messages: list[dict]) -> str:
    lines = [f"# Twilio Messages ({len(messages)} returned)\n"]
    for msg in messages:
        m = api.pick(msg, _MESSAGE_FIELDS)
        sid = m.get("sid", "—")
        to = m.get("to", "—")
        from_ = m.get("from", "—")
        status = m.get("status", "—")
        direction = m.get("direction", "—")
        date = m.get("date_sent", "—")
        body = (m.get("body") or "")[:120]
        err = m.get("error_code")
        err_msg = m.get("error_message")
        lines.append(f"## `{sid}`")
        lines.append(f"- {direction}: {from_} → {to}  |  **{status}**  |  {date}")
        if body:
            lines.append(f"- Body: {body!r}")
        if err:
            lines.append(f"- Error: {err} — {err_msg}")
    return "\n".join(lines)


def _render_calls(calls: list[dict]) -> str:
    lines = [f"# Twilio Calls ({len(calls)} returned)\n"]
    for call in calls:
        c = api.pick(call, _CALL_FIELDS)
        sid = c.get("sid", "—")
        to = c.get("to", "—")
        from_ = c.get("from", "—")
        status = c.get("status", "—")
        direction = c.get("direction", "—")
        start = c.get("start_time", "—")
        dur = c.get("duration", "—")
        price = c.get("price")
        unit = c.get("price_unit", "")
        cost = f"  |  {price} {unit}".strip() if price else ""
        lines.append(f"## `{sid}`")
        lines.append(f"- {direction}: {from_} → {to}  |  **{status}**  |  {start}  |  {dur}s{cost}")
    return "\n".join(lines)


def _render_numbers(numbers: list[dict]) -> str:
    lines = [f"# Twilio Phone Numbers ({len(numbers)} returned)\n"]
    for num in numbers:
        n = api.pick(num, _NUMBER_FIELDS)
        sid = n.get("sid", "—")
        phone = n.get("phone_number", "—")
        friendly = n.get("friendly_name") or ""
        status = n.get("status", "—")
        caps = n.get("capabilities") or {}
        cap_str = ", ".join(k for k, v in caps.items() if v) if isinstance(caps, dict) else str(caps)
        lines.append(f"- `{sid}`  {phone}  {friendly}  **{status}**  [{cap_str}]")
    return "\n".join(lines)


# -- CLI -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.twilio",
        description="Read Twilio messages/calls/numbers for support grounding.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    msg_p = sub.add_parser("messages", help="List SMS/MMS messages")
    msg_p.add_argument("--to", default=None, help="filter by To number (E.164)")
    msg_p.add_argument("--from", dest="from_", default=None, help="filter by From number (E.164)")
    msg_p.add_argument("--status", default=None, help="filter by status (delivered/failed/…)")
    msg_p.add_argument("--limit", type=int, default=20, help="max messages to return (default 20)")

    call_p = sub.add_parser("calls", help="List call records")
    call_p.add_argument("--to", default=None)
    call_p.add_argument("--from", dest="from_", default=None)
    call_p.add_argument("--status", default=None, help="filter by status (completed/failed/…)")
    call_p.add_argument("--limit", type=int, default=20)

    num_p = sub.add_parser("numbers", help="List provisioned incoming phone numbers")
    num_p.add_argument("--limit", type=int, default=50)

    args = parser.parse_args(argv)
    sid = _account_sid()

    if args.cmd == "messages":
        msgs = list_messages(sid, to=args.to, from_=args.from_, status=args.status, limit=args.limit)
        print(_render_messages(msgs))
    elif args.cmd == "calls":
        calls = list_calls(sid, to=args.to, from_=args.from_, status=args.status, limit=args.limit)
        print(_render_calls(calls))
    elif args.cmd == "numbers":
        nums = list_numbers(sid, limit=args.limit)
        print(_render_numbers(nums))
    else:
        parser.error("unknown command")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
