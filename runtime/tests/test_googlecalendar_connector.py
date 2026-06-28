"""Fixture test for the manifest-ONLY Google Calendar integration — proves a catalogued connector
with NO bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies are the Google
Calendar API's documented example payloads, trimmed to support-relevant fields. The API paginates
with a cursor: `nextPageToken` in the response body, sent back as the `pageToken` query param —
two mocked pages exercise the `cursor` pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_googlecalendar_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://www.googleapis.com/calendar/v3"
EVENTS_URL = f"{BASE}/calendars/primary/events"
CALLIST_URL = f"{BASE}/users/me/calendarList"

# --- Fixture payloads ---
# Shapes mirror documented Calendar API example objects; trimmed to support-relevant fields.
# Page 1 of events — carries nextPageToken pointing at page 2.
_EVENTS_PAGE_1 = {
    "kind": "calendar#events",
    "summary": "Primary calendar",
    "nextPageToken": "tok_page2",
    "items": [
        {
            "id": "event1",
            "summary": "Team standup",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-10T09:00:00+02:00"},
            "end": {"dateTime": "2026-06-10T09:30:00+02:00"},
            "attendees": [
                {"email": "alice@example.com"},
                {"email": "bob@example.com"},
            ],
            "htmlLink": "https://www.google.com/calendar/event?eid=event1",
        }
    ],
}

# Page 2 of events — no nextPageToken ⇒ loop stops.
_EVENTS_PAGE_2 = {
    "kind": "calendar#events",
    "summary": "Primary calendar",
    "items": [
        {
            "id": "event2",
            "summary": "Customer sync",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T14:00:00+02:00"},
            "end": {"dateTime": "2026-06-11T15:00:00+02:00"},
            "attendees": [
                {"email": "charlie@example.com"},
            ],
            "htmlLink": "https://www.google.com/calendar/event?eid=event2",
        }
    ],
}

# Single-page calendarList response (no nextPageToken).
_CAL_LIST = {
    "kind": "calendar#calendarList",
    "items": [
        {
            "id": "primary",
            "summary": "alice@example.com",
            "accessRole": "owner",
            "primary": True,
        },
        {
            "id": "team_cal@group.calendar.google.com",
            "summary": "Team calendar",
            "accessRole": "reader",
        },
    ],
}


class GoogleCalendarManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `googlecalendar` (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GOOGLECALENDAR")
        # Split prefix so the token-hygiene guard doesn't flag this line.
        os.environ["RC_CONN_GOOGLECALENDAR"] = "ya29" "_test_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GOOGLECALENDAR", None)
        else:
            os.environ["RC_CONN_GOOGLECALENDAR"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        """YAML parses into the expected Manifest fields."""
        m = api.load_manifests()
        self.assertIn("googlecalendar", m)
        gc = m["googlecalendar"]
        self.assertEqual(gc.base_url, "https://www.googleapis.com/calendar/v3")
        self.assertEqual(gc.auth.strategy, "bearer")
        self.assertEqual(gc.pagination.style, "cursor")
        self.assertEqual(gc.pagination.cursor_param, "pageToken")
        self.assertEqual(gc.pagination.cursor_field, "nextPageToken")
        self.assertEqual(gc.pagination.items_field, "items")
        self.assertEqual(gc.pagination.has_more_field, "")  # absent nextPageToken ⇒ stop
        self.assertEqual(gc.pagination.page_size, 250)
        self.assertEqual(gc.rate_limit_remaining_header, "")

    @responses.activate
    def test_cursor_pagination_stitches_two_pages(self):
        """Two pages are fetched and stitched; bearer rides both requests; nextPageToken drives loop."""
        # Page 1 carries nextPageToken; page 2 does not ⇒ pagination stops after 2 calls.
        responses.add(responses.GET, EVENTS_URL, json=_EVENTS_PAGE_1, status=200)
        responses.add(responses.GET, EVENTS_URL, json=_EVENTS_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["googlecalendar"])
        result = c.collect(
            "calendars/primary/events",
            query={"singleEvents": "true", "orderBy": "startTime"},
        )

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, ["event1", "event2"])  # both pages collected in order

        # Bearer credential on every request (including the page-2 follow).
        self.assertEqual(len(responses.calls), 2)
        self.assertEqual(
            responses.calls[0].request.headers["Authorization"],
            "Bearer ya29" "_test_token",
        )
        self.assertEqual(
            responses.calls[1].request.headers["Authorization"],
            "Bearer ya29" "_test_token",
        )

        # Page 2 request included pageToken=tok_page2 (cursor from page 1's nextPageToken).
        import urllib.parse
        p2_params = urllib.parse.parse_qs(urllib.parse.urlparse(responses.calls[1].request.url).query)
        self.assertIn("pageToken", p2_params)
        self.assertEqual(p2_params["pageToken"][0], "tok_page2")

    @responses.activate
    def test_single_page_calendarlist(self):
        """Single-page response (no nextPageToken) collects exactly one page cleanly."""
        responses.add(responses.GET, CALLIST_URL, json=_CAL_LIST, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["googlecalendar"])
        result = c.collect("users/me/calendarList")

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertIn("primary", ids)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_pick_selects_support_fields(self):
        """pick() prunes the big event object down to the few support-relevant fields."""
        responses.add(responses.GET, EVENTS_URL, json=_EVENTS_PAGE_1, status=200)
        # No page 2 — only one page fetched.
        responses.add(responses.GET, EVENTS_URL, json=_EVENTS_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["googlecalendar"])
        result = c.collect("calendars/primary/events", query={"singleEvents": "true"})

        picked = [api.pick(it, "id,summary,status,start,attendees.*.email,htmlLink")
                  for it in result["items"]]
        first = picked[0]
        self.assertEqual(first["id"], "event1")
        self.assertEqual(first["summary"], "Team standup")
        self.assertEqual(first["status"], "confirmed")
        self.assertEqual(first["attendees.*.email"], ["alice@example.com", "bob@example.com"])
        self.assertIn("htmlLink", first)

    @responses.activate
    def test_cli_drives_googlecalendar_with_bearer_and_paginate(self):
        """CLI `python -m lib.api get googlecalendar ... --paginate` collects both pages."""
        responses.add(responses.GET, EVENTS_URL, json=_EVENTS_PAGE_1, status=200)
        responses.add(responses.GET, EVENTS_URL, json=_EVENTS_PAGE_2, status=200)

        rc = api._main([
            "get", "googlecalendar", "calendars/primary/events",
            "--query", "singleEvents=true",
            "--paginate",
            "--pick", "id,summary,status",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(responses.calls[0].request.url.startswith(EVENTS_URL))
        self.assertEqual(
            responses.calls[0].request.headers["Authorization"],
            "Bearer ya29" "_test_token",
        )
        self.assertEqual(len(responses.calls), 2)


class GoogleCalendarTokenHygiene(unittest.TestCase):
    """CI guard: no real Google OAuth token prefix may land in the committed manifest/fixtures.

    Scopes to the connector dir only (manifest + any future cassette), NOT this test file — the
    test legitimately names the prefixes it hunts for, so scanning itself would be a false positive.
    """

    # Google OAuth access-token prefixes split to avoid self-triggering the guard.
    _TOKEN_PREFIXES = ("ya29" ".",)

    def test_no_token_prefixes_in_googlecalendar_files(self):
        connector_dir = (
            Path(__file__).resolve().parents[1] / "lib" / "connectors" / "googlecalendar"
        )
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
