"""Fixture test for the Microsoft Outlook Calendar connector.

Force-code trigger: Graph paginates via ``@odata.nextLink`` in the JSON body (an absolute URL),
which maps to neither lib.api's ``link`` (RFC 8288 header) nor ``cursor`` (query-param token)
styles. The connector's ``_collect()`` function handles this loop; these tests verify it stitches
pages correctly and that the bearer credential rides every request (including next-page follows).

No live creds, no network. HTTP is mocked with ``responses``. Bodies mirror Graph's documented
example event/calendar payloads, trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_msoutlookcalendar_connector.py -q
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import msoutlookcalendar as conn  # noqa: E402

BASE = "https://graph.microsoft.com/v1.0"
CALENDARS_URL = f"{BASE}/me/calendars"
EVENTS_URL = f"{BASE}/me/events"
CALVIEW_URL = f"{BASE}/me/calendarView"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# ---------------------------------------------------------------------------

_CALENDAR_1 = {
    "id": "AAMkAGI=",
    "name": "Calendar",
    "isDefaultCalendar": True,
    "canEdit": True,
    "color": "auto",
    "owner": {"name": "Samantha Booth", "address": "samanthab@contoso.com"},
}
_CALENDAR_2 = {
    "id": "AAMkAGI2=",
    "name": "Work",
    "isDefaultCalendar": False,
    "canEdit": False,
    "color": "lightBlue",
    "owner": {"name": "Samantha Booth", "address": "samanthab@contoso.com"},
}

_EVENT_1 = {
    "id": "AAMkAGIAAAoZDOFAAA=",
    "subject": "Orientation",
    "bodyPreview": "Dana, this is the time you selected for our orientation.",
    "start": {"dateTime": "2017-04-21T10:00:00.0000000", "timeZone": "Pacific Standard Time"},
    "end": {"dateTime": "2017-04-21T12:00:00.0000000", "timeZone": "Pacific Standard Time"},
    "isAllDay": False,
    "isCancelled": False,
    "organizer": {"emailAddress": {"name": "Samantha Booth", "address": "samanthab@contoso.com"}},
    "attendees": [
        {"emailAddress": {"address": "danas@contoso.com"}, "status": {"response": "accepted"}},
    ],
    "location": {"displayName": "Assembly Hall"},
    "onlineMeeting": None,
    "webLink": "https://outlook.office365.com/calendar/item/AAMkAGI=",
}
_EVENT_2 = {
    "id": "AAMkAGIAAAoZDOFBBB=",
    "subject": "Team sync",
    "bodyPreview": "Weekly team sync.",
    "start": {"dateTime": "2017-04-24T09:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2017-04-24T09:30:00.0000000", "timeZone": "UTC"},
    "isAllDay": False,
    "isCancelled": False,
    "organizer": {"emailAddress": {"name": "Manager", "address": "manager@contoso.com"}},
    "attendees": [],
    "location": {"displayName": ""},
    "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/…"},
    "webLink": "https://outlook.office365.com/calendar/item/BBB=",
}

# OData page-1 response: value + @odata.nextLink pointing at page 2.
_EVENTS_PAGE_1 = {
    "@odata.context": f"{BASE}/$metadata#users('cd209b0b')/events",
    "@odata.nextLink": f"{EVENTS_URL}?$skiptoken=abc123",
    "value": [_EVENT_1],
}
_EVENTS_PAGE_2 = {
    "@odata.context": f"{BASE}/$metadata#users('cd209b0b')/events",
    "value": [_EVENT_2],
    # No @odata.nextLink → pagination loop stops.
}
_CALENDARS_SINGLE_PAGE = {
    "@odata.context": f"{BASE}/$metadata#users('cd209b0b')/calendars",
    "value": [_CALENDAR_1, _CALENDAR_2],
}


class MSOutlookCalendarManifest(unittest.TestCase):
    """The YAML manifest must load via lib.api and map every field correctly."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_MSOUTLOOKCALENDAR")
        os.environ["RC_CONN_MSOUTLOOKCALENDAR"] = "eyJ0_fake_bearer"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSOUTLOOKCALENDAR", None)
        else:
            os.environ["RC_CONN_MSOUTLOOKCALENDAR"] = self._saved
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("msoutlookcalendar", m)
        mani = m["msoutlookcalendar"]
        self.assertEqual(mani.base_url, "https://graph.microsoft.com/v1.0")
        self.assertEqual(mani.auth.strategy, "bearer")
        # Manifest style is `none`; pagination loop is handled by the connector script.
        self.assertEqual(mani.pagination.style, "none")
        self.assertEqual(mani.rate_limit_remaining_header, "")

    @responses.activate
    def test_manifest_single_page_via_lib_api_cli(self):
        """lib.api CLI drives a single-page GET without pagination (style=none)."""
        responses.add(
            responses.GET,
            CALENDARS_URL,
            json=_CALENDARS_SINGLE_PAGE,
            status=200,
        )
        api.load_manifests()
        rc = api._main([
            "get", "msoutlookcalendar", "me/calendars",
            "--pick", "value.*.name",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(responses.calls[0].request.headers["Authorization"], "Bearer eyJ0_fake_bearer")


class MSOutlookCalendarPagination(unittest.TestCase):
    """_collect() must follow @odata.nextLink across pages and attach the bearer on every request."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_MSOUTLOOKCALENDAR")
        os.environ["RC_CONN_MSOUTLOOKCALENDAR"] = "eyJ0_fake_bearer"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSOUTLOOKCALENDAR", None)
        else:
            os.environ["RC_CONN_MSOUTLOOKCALENDAR"] = self._saved
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    @responses.activate
    def test_collect_stitches_two_pages(self):
        """_collect() follows @odata.nextLink across 2 pages and returns all items."""
        responses.add(responses.GET, EVENTS_URL, json=_EVENTS_PAGE_1, status=200)
        # The nextLink absolute URL gets called verbatim.
        responses.add(
            responses.GET,
            f"{EVENTS_URL}?$skiptoken=abc123",
            json=_EVENTS_PAGE_2,
            status=200,
        )
        api.load_manifests()
        items = conn._collect("me/events", query={"$top": 100})
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["id"], "AAMkAGIAAAoZDOFAAA=")
        self.assertEqual(items[1]["id"], "AAMkAGIAAAoZDOFBBB=")

    @responses.activate
    def test_bearer_on_every_request_incl_nextlink_follow(self):
        """Bearer credential must ride the page-1 call AND the @odata.nextLink follow."""
        responses.add(responses.GET, EVENTS_URL, json=_EVENTS_PAGE_1, status=200)
        responses.add(
            responses.GET,
            f"{EVENTS_URL}?$skiptoken=abc123",
            json=_EVENTS_PAGE_2,
            status=200,
        )
        api.load_manifests()
        conn._collect("me/events")
        for call in responses.calls:
            self.assertEqual(
                call.request.headers.get("Authorization"),
                "Bearer eyJ0_fake_bearer",
                msg=f"Missing bearer on {call.request.url}",
            )

    @responses.activate
    def test_single_page_stops_without_nextlink(self):
        """When the first page has no @odata.nextLink, _collect() stops after one request."""
        responses.add(
            responses.GET,
            CALENDARS_URL,
            json=_CALENDARS_SINGLE_PAGE,
            status=200,
        )
        api.load_manifests()
        items = conn._collect("me/calendars")
        self.assertEqual(len(items), 2)
        self.assertEqual(len(responses.calls), 1)


class MSOutlookCalendarHighLevel(unittest.TestCase):
    """High-level functions pre-select support-relevant fields and render markdown."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_MSOUTLOOKCALENDAR")
        os.environ["RC_CONN_MSOUTLOOKCALENDAR"] = "eyJ0_fake_bearer"
        api.load_manifests()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSOUTLOOKCALENDAR", None)
        else:
            os.environ["RC_CONN_MSOUTLOOKCALENDAR"] = self._saved
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    @responses.activate
    def test_list_calendars_pick(self):
        responses.add(responses.GET, CALENDARS_URL, json=_CALENDARS_SINGLE_PAGE, status=200)
        cals = conn.list_calendars()
        self.assertEqual(len(cals), 2)
        # Support-relevant fields are present.
        self.assertEqual(cals[0]["name"], "Calendar")
        self.assertTrue(cals[0]["isDefaultCalendar"])
        self.assertEqual(cals[0]["owner.address"], "samanthab@contoso.com")
        # Unwanted raw fields like 'owner' dict are absent (pick selected owner.address).
        self.assertNotIn("owner", cals[0])

    @responses.activate
    def test_list_events_shape_and_pick(self):
        responses.add(responses.GET, EVENTS_URL, json=_EVENTS_PAGE_2, status=200)
        evs = conn.list_events()
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        # Core fields selected.
        self.assertEqual(ev["subject"], "Team sync")
        self.assertIn("start.dateTime", ev)
        self.assertIn("end.dateTime", ev)
        # _EVENT_2 has an empty attendees list so pick finds nothing for that path — the key is absent.
        # That's the correct pick() behaviour: missing/empty paths are omitted rather than null.
        self.assertNotIn("attendees.*.emailAddress.address", ev)
        # Raw Graph blob fields not leaked (e.g. no 'body' HTML).
        self.assertNotIn("body", ev)

    @responses.activate
    def test_events_to_markdown_renders_title_and_events(self):
        responses.add(responses.GET, EVENTS_URL, json=_EVENTS_PAGE_1, status=200)
        responses.add(
            responses.GET,
            f"{EVENTS_URL}?$skiptoken=abc123",
            json=_EVENTS_PAGE_2,
            status=200,
        )
        evs = conn.list_events(top=10)
        md = conn.events_to_markdown(evs)
        self.assertIn("# Outlook Calendar Events", md)
        self.assertIn("Orientation", md)
        self.assertIn("Team sync", md)
        self.assertIn("samanthab@contoso.com", md)

    @responses.activate
    def test_calendars_to_markdown_renders(self):
        responses.add(responses.GET, CALENDARS_URL, json=_CALENDARS_SINGLE_PAGE, status=200)
        cals = conn.list_calendars()
        md = conn.calendars_to_markdown(cals)
        self.assertIn("# Outlook Calendars", md)
        self.assertIn("Calendar", md)
        self.assertIn("default", md)

    @responses.activate
    def test_calendar_view_uses_correct_params(self):
        responses.add(responses.GET, CALVIEW_URL, json=_EVENTS_PAGE_2, status=200)
        evs = conn.calendar_view("2026-06-01T00:00:00Z", "2026-06-30T23:59:59Z")
        self.assertEqual(len(evs), 1)
        # Verify startDateTime and endDateTime were sent as query params.
        qs = responses.calls[0].request.url
        self.assertIn("startDateTime=2026-06-01", qs)
        self.assertIn("endDateTime=2026-06-30", qs)


class MSOutlookCalendarCLI(unittest.TestCase):
    """CLI entry points (calendars / events / calview) drive the connector correctly."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_MSOUTLOOKCALENDAR")
        os.environ["RC_CONN_MSOUTLOOKCALENDAR"] = "eyJ0_fake_bearer"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSOUTLOOKCALENDAR", None)
        else:
            os.environ["RC_CONN_MSOUTLOOKCALENDAR"] = self._saved
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    @responses.activate
    def test_cli_calendars(self):
        responses.add(responses.GET, CALENDARS_URL, json=_CALENDARS_SINGLE_PAGE, status=200)
        rc = conn.main(["calendars"])
        self.assertEqual(rc, 0)
        self.assertEqual(responses.calls[0].request.headers["Authorization"], "Bearer eyJ0_fake_bearer")

    @responses.activate
    def test_cli_events(self):
        responses.add(responses.GET, EVENTS_URL, json=_EVENTS_PAGE_2, status=200)
        rc = conn.main(["events", "--top", "5"])
        self.assertEqual(rc, 0)

    @responses.activate
    def test_cli_calview(self):
        responses.add(responses.GET, CALVIEW_URL, json=_EVENTS_PAGE_2, status=200)
        rc = conn.main(["calview", "--start", "2026-06-01T00:00:00Z", "--end", "2026-06-30T23:59:59Z"])
        self.assertEqual(rc, 0)

    @responses.activate
    def test_cli_events_with_user(self):
        user_events_url = f"{BASE}/users/someone@contoso.com/events"
        responses.add(responses.GET, user_events_url, json=_EVENTS_PAGE_2, status=200)
        rc = conn.main(["--user", "someone@contoso.com", "events"])
        self.assertEqual(rc, 0)
        self.assertTrue(responses.calls[0].request.url.startswith(user_events_url))


class MSOutlookCalendarCredentialHygiene(unittest.TestCase):
    """CI guard: no real token-like material in the connector directory.

    Token-prefix hygiene guard: split prefixes with string concatenation so this guard
    doesn't flag itself. Microsoft Graph tokens are JWTs (start with 'eyJ'); we also guard
    against leaked OAuth2 client secrets.
    """

    _TOKEN_PREFIXES = (
        "eyJ" "0eyJ",   # JWT body encoded — split so this file doesn't trigger itself
        "client_secret" "=",
    )

    def test_no_token_material_in_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "msoutlookcalendar"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
