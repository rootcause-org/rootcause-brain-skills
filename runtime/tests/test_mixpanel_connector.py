"""Fixture test for the manifest-ONLY Mixpanel integration.

Proves a catalogued connector with NO bespoke Python is drivable end-to-end through
lib.api's YAML loader + CLI. No live creds, no network: HTTP is mocked with `responses`.

Mixpanel Query API uses HTTP Basic Auth (service account "username:secret"), cursor
pagination via `session_id` on the /engage endpoint, and single-page responses on
/segmentation, /funnels, and /retention.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \
        pytest tests/test_mixpanel_connector.py -q
"""

import base64
import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://mixpanel.com/api/2.0"
ENGAGE_URL = f"{BASE}/engage"
SEGMENTATION_URL = f"{BASE}/segmentation"

# Service account credential stored as "username:secret" (colon-separated), matching how the
# operator seeds it and how lib.api's `basic` strategy splits it for Base64 encoding.
_FAKE_CRED = "svc_user_abc123:secret_xyz789"
_ENCODED = base64.b64encode(_FAKE_CRED.encode()).decode()
_EXPECTED_AUTH = f"Basic {_ENCODED}"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields).
# Shape mirrors https://docs.mixpanel.com/reference/engage-query
# ---------------------------------------------------------------------------

# /engage page 1: two profiles, session_id present → page 2 exists.
_ENGAGE_PAGE_1 = {
    "page": 0,
    "page_size": 1000,
    "session_id": "1234567890-EXAMPL",
    "status": "ok",
    "total": 2,
    "results": [
        {
            "$distinct_id": "user_001",
            "$properties": {
                "$email": "alice@example.com",
                "$first_name": "Alice",
                "$last_name": "Smith",
                "plan": "pro",
            },
        },
    ],
}

# /engage page 2: last page → session_id absent (no more pages).
_ENGAGE_PAGE_2 = {
    "page": 1,
    "page_size": 1000,
    "status": "ok",
    "total": 1,
    "results": [
        {
            "$distinct_id": "user_002",
            "$properties": {
                "$email": "bob@example.com",
                "$first_name": "Bob",
                "$last_name": "Jones",
                "plan": "free",
            },
        },
    ],
}

# /segmentation — single page, no pagination cursor (aggregate event data).
_SEGMENTATION_BODY = {
    "status": "ok",
    "data": {
        "series": ["2024-01-01", "2024-01-02"],
        "values": {
            "Signed Up": {
                "2024-01-01": 42,
                "2024-01-02": 55,
            }
        },
    },
}


class MixpanelManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `mixpanel` (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_MIXPANEL")
        os.environ["RC_CONN_MIXPANEL"] = _FAKE_CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MIXPANEL", None)
        else:
            os.environ["RC_CONN_MIXPANEL"] = self._saved

    # ------------------------------------------------------------------
    # 1. YAML loading + field mapping
    # ------------------------------------------------------------------

    def test_manifest_loaded_from_yaml_and_fields_map(self):
        """YAML loader maps every declared field into the Manifest dataclass."""
        m = api.load_manifests()
        self.assertIn("mixpanel", m)
        mx = m["mixpanel"]
        self.assertEqual(mx.key, "mixpanel")
        self.assertEqual(mx.base_url, "https://mixpanel.com/api/2.0")
        # Auth — service account basic
        self.assertEqual(mx.auth.strategy, "basic")
        # Pagination — session_id cursor for /engage
        self.assertEqual(mx.pagination.style, "cursor")
        self.assertEqual(mx.pagination.cursor_field, "session_id")
        self.assertEqual(mx.pagination.cursor_param, "session_id")
        self.assertEqual(mx.pagination.has_more_field, "")  # loop until cursor empty
        self.assertEqual(mx.pagination.items_field, "results")
        self.assertEqual(mx.pagination.page_size, 1000)
        # Rate-limit header absent (429 + Retry-After is the Mixpanel mechanism)
        self.assertEqual(mx.rate_limit_remaining_header, "")
        # No required default headers for the Query API
        self.assertEqual(mx.default_headers, {})

    # ------------------------------------------------------------------
    # 2. Cursor pagination stitches ≥2 pages + credential on every request
    # ------------------------------------------------------------------

    @responses.activate
    def test_cursor_pagination_stitches_two_pages_session_id(self):
        """/engage session_id cursor: two pages collected, session_id absent on page 2 → stop."""
        # Page 1: session_id present → loop continues
        responses.add(responses.GET, ENGAGE_URL, json=_ENGAGE_PAGE_1, status=200)
        # Page 2: no session_id in response → loop stops
        responses.add(responses.GET, ENGAGE_URL, json=_ENGAGE_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["mixpanel"])
        result = c.collect(
            "engage",
            query={"project_id": "123456"},
        )

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)
        ids = [it["$distinct_id"] for it in result["items"]]
        self.assertEqual(ids, ["user_001", "user_002"])

        # Both pages fetched
        self.assertEqual(len(responses.calls), 2)

        # Page 1 request: no session_id yet (first page)
        p1_url = responses.calls[0].request.url
        self.assertNotIn("session_id=", p1_url)
        self.assertIn("project_id=123456", p1_url)

        # Page 2 request: session_id from page 1 echoed back
        p2_url = responses.calls[1].request.url
        self.assertIn("session_id=1234567890-EXAMPL", p2_url)

    @responses.activate
    def test_basic_auth_credential_on_every_request(self):
        """Basic auth header is present on BOTH pages (including the cursor-follow request)."""
        responses.add(responses.GET, ENGAGE_URL, json=_ENGAGE_PAGE_1, status=200)
        responses.add(responses.GET, ENGAGE_URL, json=_ENGAGE_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["mixpanel"])
        c.collect("engage", query={"project_id": "123456"})

        for call in responses.calls:
            self.assertEqual(call.request.headers["Authorization"], _EXPECTED_AUTH)

    # ------------------------------------------------------------------
    # 3. Single-page endpoint (/segmentation) — cursor absent → no second call
    # ------------------------------------------------------------------

    @responses.activate
    def test_single_page_endpoint_stops_after_one_call(self):
        """/segmentation has no session_id → cursor is None → paginate stops after page 1."""
        responses.add(responses.GET, SEGMENTATION_URL, json=_SEGMENTATION_BODY, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["mixpanel"])
        result = c.collect(
            "segmentation",
            query={"project_id": "123456", "event": "Signed Up",
                   "from_date": "2024-01-01", "to_date": "2024-01-31"},
        )

        # items_field="results" but segmentation has no "results" key → items = [] (the page is
        # treated as a single-item extraction attempt). Only one HTTP call is made (no cursor looping).
        self.assertEqual(len(responses.calls), 1)
        self.assertFalse(result["incomplete"], result["reason"])

    # ------------------------------------------------------------------
    # 4. api.pick selects support-relevant fields
    # ------------------------------------------------------------------

    def test_pick_selects_support_fields_from_profile(self):
        """pick() extracts email/name/plan from a nested $properties object."""
        profile = _ENGAGE_PAGE_1["results"][0]
        selected = api.pick(profile, "$properties.$email,$properties.$first_name,$properties.plan")
        self.assertEqual(selected["$properties.$email"], "alice@example.com")
        self.assertEqual(selected["$properties.$first_name"], "Alice")
        self.assertEqual(selected["$properties.plan"], "pro")

    # ------------------------------------------------------------------
    # 5. CLI drive (api._main) — manifest-only path
    # ------------------------------------------------------------------

    @responses.activate
    def test_cli_drives_engage_with_basic_auth_and_paginate(self):
        """The generic CLI can drive Mixpanel manifest-only, without any bespoke Python."""
        responses.add(responses.GET, ENGAGE_URL, json=_ENGAGE_PAGE_1, status=200)
        responses.add(responses.GET, ENGAGE_URL, json=_ENGAGE_PAGE_2, status=200)

        rc = api._main([
            "get", "mixpanel", "engage",
            "--query", "project_id=123456",
            "--paginate",
            "--pick", "$distinct_id,$properties.$email",
        ])
        self.assertEqual(rc, 0)
        # Both pages fetched via CLI
        self.assertEqual(len(responses.calls), 2)
        self.assertEqual(
            responses.calls[0].request.headers["Authorization"], _EXPECTED_AUTH
        )


# ---------------------------------------------------------------------------
# Token-prefix hygiene guard — scoped to the mixpanel connector dir only
# ---------------------------------------------------------------------------

class MixpanelCassetteHygiene(unittest.TestCase):
    """CI guard: no real Mixpanel service-account or project-secret prefix may land in the
    committed connector files.

    Scoped to the connector dir (manifest.yaml + any future cassette), NOT this test file —
    the test legitimately names the prefixes it hunts for, so scanning itself would be a
    false positive.
    """

    # Mixpanel service account usernames start with specific prefixes per their docs.
    # Split with concatenation so this guard doesn't flag itself.
    _TOKEN_PREFIXES = (
        "service-account" + ".",   # service account username prefix
        "project_secret" + "_",    # hypothetical project secret if ever committed
    )

    def test_no_token_prefixes_in_mixpanel_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "mixpanel"
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
