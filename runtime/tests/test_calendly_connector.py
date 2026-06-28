"""Fixture test for the manifest-ONLY Calendly integration — proves a catalogued connector with
NO bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. Bodies mirror Calendly's documented
API v2 example payloads (https://developer.calendly.com/api-docs), trimmed to support-relevant
fields. Calendly paginates with a cursor token at `pagination.next_page_token`, items under
`collection`, so two mocked pages exercise the real `cursor` pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_calendly_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API = "https://api.calendly.com"
SCHEDULED_EVENTS = f"{API}/scheduled_events"
ORG_URI = f"{API}/organizations/ABCD1234"

# Documented example scheduled_events response shapes (support-relevant fields).
# Page 1 has one event; pagination.next_page_token signals more. Page 2 has one event; token absent ⇒ stop.
_PAGE_1 = {
    "collection": [
        {
            "uri": f"{API}/scheduled_events/AAAA0001",
            "name": "30 Minute Meeting",
            "status": "active",
            "start_time": "2026-07-01T10:00:00.000000Z",
            "end_time": "2026-07-01T10:30:00.000000Z",
            "location": {"type": "zoom", "join_url": "https://zoom.us/j/1234567890"},
            "event_memberships": [{"user": f"{API}/users/USER0001"}],
        }
    ],
    "pagination": {
        "count": 1,
        "next_page": f"{SCHEDULED_EVENTS}?page_token=tok_page2",
        "next_page_token": "tok_page2",
        "previous_page": None,
        "previous_page_token": None,
    },
}
_PAGE_2 = {
    "collection": [
        {
            "uri": f"{API}/scheduled_events/AAAA0002",
            "name": "60 Minute Call",
            "status": "canceled",
            "start_time": "2026-06-28T14:00:00.000000Z",
            "end_time": "2026-06-28T15:00:00.000000Z",
            "location": {"type": "phone", "location": "+1-555-0100"},
            "event_memberships": [{"user": f"{API}/users/USER0001"}],
        }
    ],
    "pagination": {
        "count": 1,
        "next_page": None,
        "next_page_token": None,
        "previous_page": f"{SCHEDULED_EVENTS}?page_token=tok_page1",
        "previous_page_token": "tok_page1",
    },
}

# Documented /users/me response for the single-object endpoint test.
_USER_ME = {
    "resource": {
        "uri": f"{API}/users/USER0001",
        "name": "Alice Example",
        "email": "alice@example.com",
        "scheduling_url": "https://calendly.com/alice-example",
        "timezone": "America/New_York",
        "avatar_url": "https://i.calendly.com/alice-example.jpg",
        "current_organization": ORG_URI,
        "created_at": "2024-01-15T10:00:00.000000Z",
        "updated_at": "2026-06-01T08:00:00.000000Z",
    }
}

# Documented /event_types list response for the pick-field test.
_EVENT_TYPES_PAGE = {
    "collection": [
        {
            "uri": f"{API}/event_types/ETABC001",
            "name": "30 Minute Meeting",
            "type": "StandardEventType",
            "active": True,
            "duration": 30,
            "scheduling_url": "https://calendly.com/alice-example/30min",
        },
        {
            "uri": f"{API}/event_types/ETABC002",
            "name": "60 Minute Consultation",
            "type": "StandardEventType",
            "active": True,
            "duration": 60,
            "scheduling_url": "https://calendly.com/alice-example/60min",
        },
    ],
    "pagination": {
        "count": 2,
        "next_page": None,
        "next_page_token": None,
        "previous_page": None,
        "previous_page_token": None,
    },
}


class CalendlyManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates 'calendly' (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_CALENDLY")
        # Split the prefix literal so the hygiene guard below doesn't flag this test file itself.
        os.environ["RC_CONN_CALENDLY"] = "eyJ" + "calendly_test_pat"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_CALENDLY", None)
        else:
            os.environ["RC_CONN_CALENDLY"] = self._saved

    def test_manifest_loaded_from_yaml_with_cursor_pagination(self):
        """YAML loader discovers the manifest and maps every field correctly."""
        m = api.load_manifests()
        self.assertIn("calendly", m)
        c = m["calendly"]
        self.assertEqual(c.base_url, "https://api.calendly.com")
        self.assertEqual(c.auth.strategy, "bearer")
        self.assertEqual(c.pagination.style, "cursor")
        self.assertEqual(c.pagination.cursor_param, "page_token")
        self.assertEqual(c.pagination.cursor_field, "pagination.next_page_token")
        self.assertEqual(c.pagination.has_more_field, "")
        self.assertEqual(c.pagination.items_field, "collection")
        self.assertEqual(c.pagination.page_size, 100)
        self.assertEqual(c.rate_limit_remaining_header, "X-RateLimit-Remaining")

    @responses_lib.activate
    def test_cursor_pagination_stitches_two_pages(self):
        """Cursor pagination follows next_page_token across two pages and stitches items."""
        # Page 1: returns next_page_token → framework sends it back as page_token on page 2.
        responses_lib.add(
            responses_lib.GET,
            SCHEDULED_EVENTS,
            json=_PAGE_1,
            status=200,
            headers={"X-RateLimit-Remaining": "1199"},
        )
        # Page 2: next_page_token is None → pagination stops.
        responses_lib.add(
            responses_lib.GET,
            SCHEDULED_EVENTS,
            json=_PAGE_2,
            status=200,
            headers={"X-RateLimit-Remaining": "1198"},
        )

        api.load_manifests()
        cl = api.client(api.MANIFESTS["calendly"])
        result = cl.collect(
            "/scheduled_events",
            query={"organization": ORG_URI, "count": 100},
        )

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)
        uris = [it["uri"] for it in result["items"]]
        self.assertIn(f"{API}/scheduled_events/AAAA0001", uris)
        self.assertIn(f"{API}/scheduled_events/AAAA0002", uris)

    @responses_lib.activate
    def test_bearer_credential_on_every_request_including_paged(self):
        """The bearer token rides every request — page 1 AND the cursor-advanced page 2."""
        responses_lib.add(responses_lib.GET, SCHEDULED_EVENTS, json=_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, SCHEDULED_EVENTS, json=_PAGE_2, status=200)

        api.load_manifests()
        cl = api.client(api.MANIFESTS["calendly"])
        cl.collect("/scheduled_events", query={"organization": ORG_URI, "count": 100})

        # Both calls must carry the bearer.
        expected = "Bearer " + "eyJ" + "calendly_test_pat"
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], expected)

    @responses_lib.activate
    def test_page_two_sends_cursor_token_as_page_token(self):
        """After page 1, the framework re-sends page_token=<next_page_token> on the second call."""
        responses_lib.add(responses_lib.GET, SCHEDULED_EVENTS, json=_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, SCHEDULED_EVENTS, json=_PAGE_2, status=200)

        api.load_manifests()
        cl = api.client(api.MANIFESTS["calendly"])
        cl.collect("/scheduled_events", query={"organization": ORG_URI, "count": 100})

        self.assertEqual(len(responses_lib.calls), 2)
        # Page 1: no page_token in params.
        page1_url = responses_lib.calls[0].request.url
        self.assertNotIn("page_token=", page1_url)
        # Page 2: page_token=tok_page2 forwarded from pagination.next_page_token.
        page2_url = responses_lib.calls[1].request.url
        self.assertIn("page_token=tok_page2", page2_url)

    @responses_lib.activate
    def test_pick_selects_support_relevant_fields(self):
        """api.pick() trims event objects down to the few fields an agent needs."""
        responses_lib.add(responses_lib.GET, SCHEDULED_EVENTS, json=_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, SCHEDULED_EVENTS, json=_PAGE_2, status=200)

        api.load_manifests()
        cl = api.client(api.MANIFESTS["calendly"])
        result = cl.collect("/scheduled_events", query={"organization": ORG_URI, "count": 100})

        picked = [
            api.pick(it, "uri,name,status,start_time,end_time,location")
            for it in result["items"]
        ]
        self.assertEqual(picked[0]["name"], "30 Minute Meeting")
        self.assertEqual(picked[0]["status"], "active")
        self.assertIn("start_time", picked[0])
        self.assertEqual(picked[1]["status"], "canceled")

    @responses_lib.activate
    def test_single_page_response_with_no_next_token(self):
        """When pagination.next_page_token is absent (None), collect() stops after one page."""
        responses_lib.add(
            responses_lib.GET,
            f"{API}/event_types",
            json=_EVENT_TYPES_PAGE,
            status=200,
        )

        api.load_manifests()
        cl = api.client(api.MANIFESTS["calendly"])
        result = cl.collect(
            "/event_types",
            query={"user": f"{API}/users/USER0001", "count": 100},
        )

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"].startswith("Bearer "), True)
        # Exactly one HTTP call — no second page requested.
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_cli_drive_paginate(self):
        """CLI `python -m lib.api get calendly ...` stitches pages and returns collected JSON."""
        responses_lib.add(responses_lib.GET, SCHEDULED_EVENTS, json=_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, SCHEDULED_EVENTS, json=_PAGE_2, status=200)

        rc = api._main([
            "get", "calendly", "/scheduled_events",
            "--query", f"organization={ORG_URI}",
            "--query", "count=100",
            "--paginate",
            "--pick", "uri,name,status,start_time",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)
        # Bearer present on the wire.
        self.assertIn("Bearer", responses_lib.calls[0].request.headers["Authorization"])

    @responses_lib.activate
    def test_cli_drive_single_get(self):
        """CLI single GET (no --paginate) fetches /users/me and returns the resource object."""
        responses_lib.add(
            responses_lib.GET,
            f"{API}/users/me",
            json=_USER_ME,
            status=200,
        )

        rc = api._main([
            "get", "calendly", "/users/me",
            "--pick", "resource.uri,resource.name,resource.email,resource.current_organization",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)
        self.assertIn("Bearer", responses_lib.calls[0].request.headers["Authorization"])


class CalendlyCassetteHygiene(unittest.TestCase):
    """CI guard: no real Calendly PAT prefix may land in the connector dir or fixtures.

    Scopes to the connector directory only — this test file legitimately names the prefix it hunts
    for (split to avoid self-triggering) so scanning this file would always produce a false positive.
    """

    # Calendly Personal Access Tokens start with "eyJ" (base64url JWT header) but that is too
    # common; the stable unique marker is the full prefix the Calendly docs show.
    # We guard against the un-split prefix as a concatenated string here:
    _TOKEN_PREFIXES = ("eyJhbGci" + "OiJIUzI",)  # split to avoid the guard flagging itself

    def test_no_token_prefixes_in_calendly_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "calendly"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains token-like prefix")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
