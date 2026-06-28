"""Fixture test for the manifest-ONLY HubSpot integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies are trimmed from
HubSpot's documented CRM v3 example payloads (developers.hubspot.com/docs/api/crm/objects).
HubSpot paginates with a cursor at `paging.next.after`; results live under `results`. Two mocked
pages exercise the real `cursor` pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_hubspot_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API = "https://api.hubapi.com"
CONTACTS = f"{API}/crm/v3/objects/contacts"
TICKETS = f"{API}/crm/v3/objects/tickets"

# Two pages of contacts. Shapes mirror the documented HubSpot CRM v3 list response:
# {"results": [...], "paging": {"next": {"after": "...", "link": "..."}}}
# Only support-relevant properties are kept.
_PAGE_1 = {
    "results": [
        {
            "id": "1",
            "properties": {
                "firstname": "Alice",
                "lastname": "Smith",
                "email": "alice@example.com",
                "hs_lead_status": "OPEN",
            },
            "createdAt": "2024-01-01T00:00:00.000Z",
            "updatedAt": "2024-06-01T00:00:00.000Z",
        },
    ],
    "paging": {
        "next": {
            "after": "cursor-abc-123",
            "link": f"{CONTACTS}?after=cursor-abc-123",
        }
    },
}
_PAGE_2 = {
    "results": [
        {
            "id": "2",
            "properties": {
                "firstname": "Bob",
                "lastname": "Jones",
                "email": "bob@example.com",
                "hs_lead_status": "NEW",
            },
            "createdAt": "2024-02-01T00:00:00.000Z",
            "updatedAt": "2024-06-15T00:00:00.000Z",
        },
    ],
    # no "paging" key → cursor absent → last page, loop stops
}

_TICKETS_PAGE = {
    "results": [
        {
            "id": "101",
            "properties": {
                "subject": "Login failure after password reset",
                "hs_pipeline_stage": "1",
                "hs_ticket_priority": "HIGH",
                "content": "Customer cannot log in after resetting password.",
            },
            "createdAt": "2024-05-10T08:00:00.000Z",
            "updatedAt": "2024-05-11T09:00:00.000Z",
        },
    ],
    # no paging → single page
}


class HubspotManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates "hubspot" (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_HUBSPOT")
        # Token prefix split so the hygiene guard in this file's own test can't flag itself.
        os.environ["RC_CONN_HUBSPOT"] = "pat" "-eu1-test-private-app-token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_HUBSPOT", None)
        else:
            os.environ["RC_CONN_HUBSPOT"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        """YAML loads and every catalogued field maps correctly to Manifest dataclass fields."""
        m = api.load_manifests()
        self.assertIn("hubspot", m)
        h = m["hubspot"]
        self.assertEqual(h.key, "hubspot")
        self.assertEqual(h.base_url, "https://api.hubapi.com")
        self.assertEqual(h.auth.strategy, "bearer")
        # Cursor pagination: results array under 'results', cursor at paging.next.after
        self.assertEqual(h.pagination.style, "cursor")
        self.assertEqual(h.pagination.items_field, "results")
        self.assertEqual(h.pagination.cursor_field, "paging.next.after")
        self.assertEqual(h.pagination.cursor_param, "after")
        self.assertEqual(h.pagination.has_more_field, "")
        self.assertEqual(h.pagination.page_size, 100)
        self.assertEqual(h.rate_limit_remaining_header, "X-HubSpot-RateLimit-Remaining")

    @responses.activate
    def test_cursor_pagination_stitches_two_pages(self):
        """Cursor pagination: page 1 has paging.next.after → page 2 fetched with ?after=; page 2
        has no paging block → loop stops. Both pages' results are merged into one list."""
        responses.add(
            responses.GET, CONTACTS, json=_PAGE_1, status=200,
            headers={"X-HubSpot-RateLimit-Remaining": "99"},
        )
        # Page 2 is fetched with ?after=cursor-abc-123 appended by the cursor pager.
        responses.add(
            responses.GET, CONTACTS, json=_PAGE_2, status=200,
            headers={"X-HubSpot-RateLimit-Remaining": "98"},
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["hubspot"])
        result = c.collect(
            "/crm/v3/objects/contacts",
            query={"limit": 100, "properties": "firstname,lastname,email,hs_lead_status"},
        )

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, ["1", "2"])  # both pages stitched in order

    @responses.activate
    def test_bearer_credential_rides_every_request_including_cursor_follow(self):
        """The bearer token must appear in the Authorization header on BOTH page fetches."""
        responses.add(responses.GET, CONTACTS, json=_PAGE_1, status=200)
        responses.add(responses.GET, CONTACTS, json=_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["hubspot"])
        c.collect("/crm/v3/objects/contacts")

        self.assertEqual(len(responses.calls), 2)
        for call in responses.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(
                auth.startswith("Bearer "),
                f"Expected 'Bearer …' Authorization header, got: {auth!r}",
            )
            # The full credential value is present
            self.assertIn("pat" "-eu1-test-private-app-token", auth)

    @responses.activate
    def test_cursor_param_sent_on_page_2(self):
        """The cursor value from page 1 (paging.next.after) is sent as the 'after' query param
        on the page-2 request."""
        responses.add(responses.GET, CONTACTS, json=_PAGE_1, status=200)
        responses.add(responses.GET, CONTACTS, json=_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["hubspot"])
        list(c.paginate("/crm/v3/objects/contacts"))

        self.assertEqual(len(responses.calls), 2)
        # Page 1: no cursor param
        self.assertNotIn("after=", responses.calls[0].request.url)
        # Page 2: cursor param = the value from paging.next.after
        self.assertIn("after=cursor-abc-123", responses.calls[1].request.url)

    @responses.activate
    def test_single_page_when_no_paging_block(self):
        """When the response has no paging block at all, collect returns after one page."""
        responses.add(responses.GET, TICKETS, json=_TICKETS_PAGE, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["hubspot"])
        result = c.collect("/crm/v3/objects/tickets", query={"limit": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], "101")
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_pick_selects_support_relevant_fields(self):
        """api.pick prunes the large CRM object down to the few support-relevant fields."""
        responses.add(responses.GET, CONTACTS, json=_PAGE_1, status=200)
        responses.add(responses.GET, CONTACTS, json=_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["hubspot"])
        result = c.collect("/crm/v3/objects/contacts")

        picked = [api.pick(it, "id,properties.email,properties.firstname,properties.hs_lead_status")
                  for it in result["items"]]
        self.assertEqual(picked[0]["id"], "1")
        self.assertEqual(picked[0]["properties.email"], "alice@example.com")
        self.assertEqual(picked[0]["properties.hs_lead_status"], "OPEN")
        self.assertEqual(picked[1]["id"], "2")
        self.assertEqual(picked[1]["properties.email"], "bob@example.com")

    @responses.activate
    def test_cli_drives_hubspot_with_bearer_and_paginate(self):
        """The lib.api generic CLI works end-to-end: loads YAML, bearer header, cursor pages."""
        responses.add(responses.GET, CONTACTS, json=_PAGE_1, status=200)
        responses.add(responses.GET, CONTACTS, json=_PAGE_2, status=200)

        rc = api._main([
            "get", "hubspot", "/crm/v3/objects/contacts",
            "--query", "limit=100",
            "--paginate",
            "--pick", "id,properties.email",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses.calls), 2)
        self.assertTrue(responses.calls[0].request.url.startswith(CONTACTS))
        auth = responses.calls[0].request.headers["Authorization"]
        self.assertTrue(auth.startswith("Bearer "))

    @responses.activate
    def test_cli_single_get_no_paginate(self):
        """Non-paginated CLI call: fetches exactly one page, prints JSON."""
        responses.add(responses.GET, TICKETS, json=_TICKETS_PAGE, status=200)

        rc = api._main([
            "get", "hubspot", "/crm/v3/objects/tickets",
            "--query", "limit=10",
            "--pick", "results.*.id",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses.calls), 1)


class HubspotCassetteHygiene(unittest.TestCase):
    """CI guard: no real HubSpot private-app token prefix may land in the connector dir.

    Scopes to the connector dir (manifest + any future cassette), NOT this test file — the test
    legitimately splits the prefix literals so scanning itself is not a false positive.
    """

    # HubSpot private-app token prefix: pat-<region>- (e.g. pat-na1-, pat-eu1-).
    # Split to avoid the hygiene guard flagging this source file.
    _TOKEN_PREFIXES = ("pat" "-na1-", "pat" "-eu1-", "pat" "-ap1-")

    def test_no_token_prefixes_in_hubspot_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "hubspot"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains {pref!r}")
        self.assertEqual(offenders, [], f"token-like material in connector dir: {offenders}")


if __name__ == "__main__":
    unittest.main()
