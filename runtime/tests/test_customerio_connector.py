"""Fixture test for the manifest-ONLY Customer.io integration — proves a catalogued connector with
NO bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. Bodies mirror Customer.io's documented
App API payloads for list-people and list-activities, trimmed to support-relevant fields.
Customer.io paginates with a top-level `"next"` cursor string, so the two mocked pages exercise
the real cursor pagination style end-to-end, verifying that `start=<cursor>` is sent on page 2
and that the bearer credential rides every request.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_customerio_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API = "https://api.customer.io/v1"
CUSTOMERS = f"{API}/customers"
# Use a cio_id (numeric string) rather than email so the URL is not percent-encoded.
PERSON_ID = "abc123"
ACTIVITIES = f"{API}/customers/{PERSON_ID}/activities"

# Two pages of people (customers). Shapes mirror the documented Customer.io App API response.
# Page 1 carries a "next" cursor → page 2. Page 2 has no "next" → loop stops.
_PAGE_1 = {
    "customers": [
        {
            "id": "abc123",
            "email": "alice@example.com",
            "attributes": {
                "first_name": "Alice",
                "last_name": "Smith",
                "plan": "pro",
            },
            "created_at": 1700000000,
        },
    ],
    "next": "cursor_page_2",
}
_PAGE_2 = {
    "customers": [
        {
            "id": "def456",
            "email": "bob@example.com",
            "attributes": {
                "first_name": "Bob",
                "last_name": "Jones",
                "plan": "starter",
            },
            "created_at": 1700001000,
        },
    ],
    # No "next" key — final page.
}

# Single page of activities for a person (no pagination needed for this shape).
_ACTIVITIES_PAGE_1 = {
    "activities": [
        {
            "type": "event",
            "name": "page_viewed",
            "timestamp": 1700005000,
        },
        {
            "type": "email",
            "name": "welcome_email",
            "timestamp": 1700004000,
        },
    ],
}


class CustomerioManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates 'customerio' (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_CUSTOMERIO")
        # Split prefix with concatenation so CI token-hygiene grep doesn't flag this test file.
        os.environ["RC_CONN_CUSTOMERIO"] = "app" + "_api_key_test_value"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_CUSTOMERIO", None)
        else:
            os.environ["RC_CONN_CUSTOMERIO"] = self._saved

    def test_manifest_loaded_from_yaml_with_cursor_pagination(self):
        m = api.load_manifests()
        self.assertIn("customerio", m)
        c = m["customerio"]
        self.assertEqual(c.base_url, "https://api.customer.io/v1")
        self.assertEqual(c.auth.strategy, "bearer")
        self.assertEqual(c.pagination.style, "cursor")
        self.assertEqual(c.pagination.cursor_field, "next")
        self.assertEqual(c.pagination.cursor_param, "start")
        self.assertEqual(c.pagination.has_more_field, "")
        self.assertEqual(c.pagination.items_field, "customers")
        self.assertEqual(c.pagination.page_size, 100)
        self.assertEqual(c.rate_limit_remaining_header, "X-RateLimit-Remaining")
        # No required version header.
        self.assertEqual(c.default_headers, {})

    @responses.activate
    def test_cursor_pagination_stitches_two_pages(self):
        # Page 1: customers list + "next" cursor → page 2. Page 2: no "next" → stop.
        responses.add(
            responses.GET, CUSTOMERS,
            json=_PAGE_1, status=200,
            headers={"X-RateLimit-Remaining": "999"},
        )
        responses.add(
            responses.GET, CUSTOMERS,
            json=_PAGE_2, status=200,
            headers={"X-RateLimit-Remaining": "998"},
        )

        api.load_manifests()
        cl = api.client(api.MANIFESTS["customerio"])
        result = cl.collect(CUSTOMERS, query={"limit": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, ["abc123", "def456"])  # both pages stitched, order preserved

        # Bearer credential rode on BOTH requests (incl. cursor follow).
        for call in responses.calls:
            self.assertEqual(
                call.request.headers["Authorization"],
                "Bearer " + "app" + "_api_key_test_value",
            )

        # Second request carried the cursor token as `start=cursor_page_2`.
        self.assertIn("start=cursor_page_2", responses.calls[1].request.url)

    @responses.activate
    def test_bearer_present_on_single_page_get(self):
        # Single-page GET (e.g. activities) — bearer must be on the wire.
        # Use a cio_id identifier (not email) to avoid percent-encoding in the URL.
        responses.add(
            responses.GET, ACTIVITIES,
            json=_ACTIVITIES_PAGE_1, status=200,
        )

        api.load_manifests()
        cl = api.client(api.MANIFESTS["customerio"])
        body = cl.get(f"customers/{PERSON_ID}/activities")

        self.assertIn("activities", body)
        self.assertEqual(len(body["activities"]), 2)
        self.assertEqual(
            responses.calls[0].request.headers["Authorization"],
            "Bearer " + "app" + "_api_key_test_value",
        )

    @responses.activate
    def test_pick_selects_support_relevant_fields(self):
        responses.add(responses.GET, CUSTOMERS, json=_PAGE_1, status=200)
        responses.add(responses.GET, CUSTOMERS, json=_PAGE_2, status=200)

        api.load_manifests()
        cl = api.client(api.MANIFESTS["customerio"])
        result = cl.collect(CUSTOMERS, query={"limit": 100})

        picked = [
            api.pick(it, "id,email,attributes.first_name,attributes.last_name,attributes.plan")
            for it in result["items"]
        ]
        self.assertEqual(picked[0]["id"], "abc123")
        self.assertEqual(picked[0]["email"], "alice@example.com")
        self.assertEqual(picked[0]["attributes.first_name"], "Alice")
        self.assertEqual(picked[0]["attributes.plan"], "pro")
        self.assertEqual(picked[1]["attributes.plan"], "starter")

    @responses.activate
    def test_cli_drives_customerio_with_bearer_and_paginate(self):
        responses.add(responses.GET, CUSTOMERS, json=_PAGE_1, status=200)
        responses.add(responses.GET, CUSTOMERS, json=_PAGE_2, status=200)

        rc = api._main([
            "get", "customerio", "/customers",
            "--query", "limit=100",
            "--paginate",
            "--pick", "id,email",
        ])
        self.assertEqual(rc, 0)
        # Both pages fetched.
        self.assertEqual(len(responses.calls), 2)
        self.assertTrue(responses.calls[0].request.url.startswith(CUSTOMERS))
        self.assertEqual(
            responses.calls[0].request.headers["Authorization"],
            "Bearer " + "app" + "_api_key_test_value",
        )


class CustomerioTokenHygiene(unittest.TestCase):
    """CI guard: no real Customer.io App API key prefix may land in the connector dir.

    Scopes to the connector dir (manifest + any future cassette), NOT this test file — the test
    legitimately names the prefixes it hunts for in split form, so scanning itself is a false
    positive.

    Customer.io App API keys are JWTs (base64url-encoded JSON Web Tokens), so they begin with the
    standard JWT header prefix `eyJ` (base64url of `{"`) — split to avoid self-flagging.
    """

    # JWT credential prefix (Customer.io App API keys are JWTs); split to avoid self-triggering.
    _TOKEN_PREFIXES = ("ey" "J",)

    def test_no_token_prefixes_in_customerio_files(self):
        connector_dir = (
            Path(__file__).resolve().parents[1] / "lib" / "connectors" / "customerio"
        )
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
