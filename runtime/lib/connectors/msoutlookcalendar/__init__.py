"""Microsoft Outlook Calendar connector — reads calendars and events via Microsoft Graph v1.0.

Force-code trigger (d): Graph paginates with ``@odata.nextLink`` in the JSON body — an absolute
URL. lib.api's ``link`` style follows RFC 8288 ``Link:`` response headers; ``cursor`` style sends
the cursor value as a query param. Neither maps to Graph's "next absolute URL in JSON body"
pattern, so this thin script handles the pagination loop and delegates retry/backoff/auth to
``lib.api``.

Read-only: only GET requests (plus a POST to /getSchedule for free/busy, which is a Graph-idiomatic
query verb, not a write).

CLI:
    python -m lib.connectors.msoutlookcalendar calendars
    python -m lib.connectors.msoutlookcalendar events [--user UPN] [--top N] [--start ISO] [--end ISO]
    python -m lib.connectors.msoutlookcalendar calview --start ISO --end ISO [--user UPN]
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from lib import api

BASE = "https://graph.microsoft.com/v1.0"

# Manifest declared in manifest.yaml; register here so `python -m lib.api get msoutlookcalendar`
# and the connector CLI both resolve the same manifest instance.
MANIFEST = api.register(
    api.Manifest(
        key="msoutlookcalendar",
        base_url=BASE,
        auth=api.Auth(strategy="bearer"),
        # Pagination=none in the manifest so lib.api never attempts its own loop; the connector
        # runs the @odata.nextLink loop manually using _collect() below.
        pagination=api.Pagination(style="none"),
        rate_limit_remaining_header="",  # Graph uses 429 + Retry-After
    )
)


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="msoutlookcalendar")


def _collect(path: str, *, query: dict[str, Any] | None = None, max_items: int = 500) -> list[dict]:
    """GET a Graph list endpoint and follow ``@odata.nextLink`` until exhausted or max_items.

    Graph pagination: each response carries ``value`` (the item array) and optionally
    ``@odata.nextLink`` (an absolute URL for the next page). This is the force-code trigger:
    lib.api has no pagination style that follows an absolute URL from the JSON body.
    """
    c = _client()
    items: list[dict] = []
    # First page via the normal manifest path.
    body = c.get(path, query=query)
    items.extend(body.get("value") or [])

    next_url: str | None = body.get("@odata.nextLink")
    while next_url and len(items) < max_items:
        # _send_url follows the absolute URL verbatim, applying auth (bearer) and retries.
        resp = c._send_url("GET", next_url)  # noqa: SLF001 — internal lib.api seam for absolute URLs
        body = json.loads(resp.text)
        items.extend(body.get("value") or [])
        next_url = body.get("@odata.nextLink")

    return items[:max_items]


# ---------------------------------------------------------------------------
# High-level reads (support-relevant fields pre-selected)
# ---------------------------------------------------------------------------

_CALENDAR_FIELDS = "$select=id,name,isDefaultCalendar,canEdit,owner,color"
_EVENT_FIELDS = "id,subject,bodyPreview,start,end,isAllDay,isCancelled,organizer,attendees,location,onlineMeeting,webLink"


def list_calendars(user: str = "me") -> list[dict]:
    """Return all calendars for ``user`` (UPN or 'me') with support-relevant fields."""
    base_path = f"users/{user}" if user != "me" else "me"
    items = _collect(f"{base_path}/calendars", query={"$select": "id,name,isDefaultCalendar,canEdit,owner,color"})
    return [api.pick(c, "id,name,isDefaultCalendar,canEdit,owner.address,color") for c in items]


def list_events(
    user: str = "me",
    *,
    top: int = 50,
    start_datetime: str | None = None,
    end_datetime: str | None = None,
) -> list[dict]:
    """Return events from the primary calendar, newest first, with support-relevant fields pre-selected.

    Pass ``start_datetime`` + ``end_datetime`` (ISO 8601 UTC) to filter via ``$filter``; otherwise
    returns the most recent ``top`` events ordered by start descending.
    """
    base_path = f"users/{user}" if user != "me" else "me"
    q: dict[str, Any] = {
        "$select": _EVENT_FIELDS,
        "$top": min(top, 100),
        "$orderby": "start/dateTime desc",
    }
    if start_datetime and end_datetime:
        q["$filter"] = f"start/dateTime ge '{start_datetime}' and end/dateTime le '{end_datetime}'"
    items = _collect(f"{base_path}/events", query=q, max_items=top)
    return [_shape_event(e) for e in items]


def calendar_view(
    start_datetime: str,
    end_datetime: str,
    user: str = "me",
    *,
    max_items: int = 200,
) -> list[dict]:
    """Return the calendar view (expanded recurring instances) for a time window.

    Uses ``/calendarView`` with ``startDateTime`` + ``endDateTime`` query params (Graph-required).
    """
    base_path = f"users/{user}" if user != "me" else "me"
    q: dict[str, Any] = {
        "startDateTime": start_datetime,
        "endDateTime": end_datetime,
        "$select": _EVENT_FIELDS,
        "$top": 100,
        "$orderby": "start/dateTime asc",
    }
    items = _collect(f"{base_path}/calendarView", query=q, max_items=max_items)
    return [_shape_event(e) for e in items]


def _shape_event(e: dict) -> dict:
    """Pre-select the support-relevant scalar fields from a Graph event object.

    Graph events are large (full HTML body, recurrence patterns, sensitivity, …). This drops
    everything except the handful of fields a support agent needs to understand scheduling issues.
    """
    return api.pick(
        e,
        "id,subject,bodyPreview,start.dateTime,start.timeZone,"
        "end.dateTime,end.timeZone,isAllDay,isCancelled,"
        "organizer.emailAddress.name,organizer.emailAddress.address,"
        "attendees.*.emailAddress.address,attendees.*.status.response,"
        "location.displayName,onlineMeeting.joinUrl,webLink",
    )


# ---------------------------------------------------------------------------
# Markdown rendering (concise support output)
# ---------------------------------------------------------------------------


def calendars_to_markdown(calendars: list[dict]) -> str:
    if not calendars:
        return "# Outlook Calendars\n(none found)"
    lines = ["# Outlook Calendars", ""]
    for c in calendars:
        default_flag = " *(default)*" if c.get("isDefaultCalendar") else ""
        owner = c.get("owner.address", "")
        lines.append(f"- **{c.get('name', '?')}**{default_flag}  id=`{c.get('id', '?')}`" + (f"  owner={owner}" if owner else ""))
    return "\n".join(lines)


def events_to_markdown(events: list[dict], title: str = "Outlook Calendar Events") -> str:
    if not events:
        return f"# {title}\n(no events)"
    lines = [f"# {title}", ""]
    for e in events:
        subject = e.get("subject") or "(no subject)"
        start = (e.get("start.dateTime") or "")[:16].replace("T", " ")
        end = (e.get("end.dateTime") or "")[:16].replace("T", " ")
        all_day = " (all day)" if e.get("isAllDay") else ""
        cancelled = " **[CANCELLED]**" if e.get("isCancelled") else ""
        org = e.get("organizer.emailAddress.address") or ""
        loc = e.get("location.displayName") or ""
        join = e.get("onlineMeeting.joinUrl") or ""
        preview = (e.get("bodyPreview") or "")[:120]
        lines.append(f"## {subject}{cancelled}")
        lines.append(f"- Time: {start} → {end}{all_day}")
        if org:
            lines.append(f"- Organizer: {org}")
        if loc:
            lines.append(f"- Location: {loc}")
        if join:
            lines.append(f"- Join: {join}")
        attendees = e.get("attendees.*.emailAddress.address") or []
        if attendees:
            lines.append(f"- Attendees: {', '.join(attendees[:10])}" + (" …" if len(attendees) > 10 else ""))
        if preview:
            lines.append(f"- Preview: {preview}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.msoutlookcalendar",
        description="Read Microsoft Outlook Calendar via Graph v1.0 (read-only).",
    )
    parser.add_argument("--user", default="me", metavar="UPN", help="user principal name or 'me' (default)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("calendars", help="list all calendars for the user")

    ev = sub.add_parser("events", help="list events from the primary calendar")
    ev.add_argument("--top", type=int, default=50, metavar="N", help="max events to return (default 50)")
    ev.add_argument("--start", default=None, metavar="ISO", help="filter start datetime (ISO 8601 UTC)")
    ev.add_argument("--end", default=None, metavar="ISO", help="filter end datetime (ISO 8601 UTC)")

    cv = sub.add_parser("calview", help="calendar view (expanded recurring events) for a date range")
    cv.add_argument("--start", required=True, metavar="ISO", help="window start (ISO 8601 UTC)")
    cv.add_argument("--end", required=True, metavar="ISO", help="window end (ISO 8601 UTC)")
    cv.add_argument("--max", type=int, default=200, metavar="N", dest="max_items")

    args = parser.parse_args(argv)
    user = args.user

    if args.cmd == "calendars":
        print(calendars_to_markdown(list_calendars(user)))
        return 0
    if args.cmd == "events":
        evs = list_events(user, top=args.top, start_datetime=args.start, end_datetime=args.end)
        print(events_to_markdown(evs))
        return 0
    if args.cmd == "calview":
        evs = calendar_view(args.start, args.end, user, max_items=args.max_items)
        print(events_to_markdown(evs, title="Outlook Calendar View"))
        return 0

    parser.error("unknown command")
    return 2
