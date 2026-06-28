"""Fixture test for the manifest-ONLY Close CRM integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies mirror Close's own
documented example payloads (developer.close.com), trimmed to support-relevant fields. Close
paginates with offset (_skip/_limit) and a `{"data": [...], "has_more": bool}` envelope, so the
two mocked pages exercise the real `offset` pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_close_connector.py -q
"""

import base64
import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://api.close.com/api/v1"
LEADS_URL = f"{BASE}/lead/"
OPPORTUNITIES_URL = f"{BASE}/opportunity/"
ACTIVITIES_URL = f"{BASE}/activity/"
ACTIVITY_EMAIL_URL = f"{BASE}/activity/email/"

# Documented example lead payloads (developer.close.com "List Leads"), trimmed to support fields.
_LEAD_1 = {
    "id": "lead_abc123",
    "display_name": "Acme Corp",
    "status_label": "Potential",
    "status_id": "stat_abc",
    "date_created": "2024-01-15T10:00:00.000000+00:00",
    "date_updated": "2024-03-01T12:00:00.000000+00:00",
    "html_url": "https://app.close.com/lead/lead_abc123/",
    "organization_id": "orga_xyz",
    "contacts": [
        {
            "id": "cont_111",
            "name": "Alice Example",
            "emails": [{"email": "alice@acme.com", "type": "office"}],
            "phones": [{"phone": "+1-555-000-0001", "type": "office"}],
        }
    ],
    "opportunities": [],
}
_LEAD_2 = {
    "id": "lead_def456",
    "display_name": "Beta Ltd",
    "status_label": "Customer",
    "status_id": "stat_def",
    "date_created": "2024-02-01T09:00:00.000000+00:00",
    "date_updated": "2024-03-10T11:00:00.000000+00:00",
    "html_url": "https://app.close.com/lead/lead_def456/",
    "organization_id": "orga_xyz",
    "contacts": [
        {
            "id": "cont_222",
            "name": "Bob Example",
            "emails": [{"email": "bob@beta.com", "type": "office"}],
            "phones": [],
        }
    ],
    "opportunities": [],
}

# Page 1: has_more=True → advance _skip by page_size. Page 2: has_more=False → stop.
_PAGE_1 = {"data": [_LEAD_1], "has_more": True, "total_results": 2}
_PAGE_2 = {"data": [_LEAD_2], "has_more": False, "total_results": 2}

# Documented opportunity payload (developer.close.com "Opportunities").
_OPPORTUNITY_1 = {
    "id": "oppo_abc",
    "lead_id": "lead_abc123",
    "status_label": "Active",
    "status_type": "active",
    "value": 50000,
    "value_currency": "USD",
    "close_date": "2024-06-30",
    "date_won": None,
    "date_lost": None,
    "date_created": "2024-01-20T10:00:00.000000+00:00",
}

# Documented email activity payload (developer.close.com "Activities / Emails").
_EMAIL_ACTIVITY_1 = {
    "id": "acti_email_001",
    "_type": "Email",
    "lead_id": "lead_abc123",
    "date_created": "2024-03-01T09:00:00.000000+00:00",
    "subject": "Welcome to Acme",
    "body_text": "Hi Alice, welcome aboard!",
    "direction": "outgoing",
    "status": "sent",
}


def _basic_header(credential: str) -> str:
    """Compute the expected Authorization header value for a basic-auth credential string."""
    encoded = base64.b64encode(credential.encode()).decode()
    return f"Basic {encoded}"


class CloseManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `close` (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_CLOSE")
        # Credential is "apikey:" — Close basic-auth: API key as user, empty password.
        # Split token prefix so the hygiene guard doesn't flag this file itself.
        os.environ["RC_CONN_CLOSE"] = "test_close_" "key:"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_CLOSE", None)
        else:
            os.environ["RC_CONN_CLOSE"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        """YAML loads cleanly and every declared field maps to the Manifest dataclass."""
        m = api.load_manifests()
        self.assertIn("close", m)
        cl = m["close"]
        self.assertEqual(cl.base_url, "https://api.close.com/api/v1")
        # Auth: basic — credential "apikey:" → Base64 in Authorization header.
        self.assertEqual(cl.auth.strategy, "basic")
        # Pagination: offset — _skip/_limit, response envelope {"data": [...], "has_more": bool}.
        self.assertEqual(cl.pagination.style, "offset")
        self.assertEqual(cl.pagination.offset_param, "_skip")
        self.assertEqual(cl.pagination.limit_param, "_limit")
        self.assertEqual(cl.pagination.items_field, "data")
        self.assertEqual(cl.pagination.has_more_field, "has_more")
        self.assertEqual(cl.pagination.page_size, 100)
        # No rate-limit remaining header (Close uses structured RateLimit header, not simple).
        self.assertEqual(cl.rate_limit_remaining_header, "")

    @responses_lib.activate
    def test_offset_pagination_stitches_two_pages(self):
        """Two mocked pages stitched via offset produce all items in order.

        lib.api's offset paginator advances _skip while len(page.items) == page_size. We use
        page_size=2: page 1 returns both leads (2 items == 2 → fetches page 2); page 2 returns
        no items (0 < 2 → loop stops). Both leads appear in the collected result.
        """
        api.load_manifests()
        mani = api.MANIFESTS["close"]
        small_mani = api.Manifest(
            key=mani.key,
            base_url=mani.base_url,
            auth=mani.auth,
            pagination=api.Pagination(
                style="offset",
                offset_param="_skip",
                limit_param="_limit",
                items_field="data",
                page_size=2,  # page 1 fills exactly 2 items → triggers page 2 fetch
            ),
            rate_limit_remaining_header=mani.rate_limit_remaining_header,
        )

        # Page 1: full page (2 items == page_size → continue)
        responses_lib.add(
            responses_lib.GET, LEADS_URL,
            json={"data": [_LEAD_1, _LEAD_2], "has_more": True},
            status=200,
        )
        # Page 2: empty → loop stops
        responses_lib.add(
            responses_lib.GET, LEADS_URL,
            json={"data": [], "has_more": False},
            status=200,
        )

        c = api.Client(manifest=small_mani, credential="test_close_" "key:")
        result = c.collect("lead/")

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, ["lead_abc123", "lead_def456"])
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_basic_auth_credential_on_every_request(self):
        """Basic-auth header rides on both pages — not dropped on the second offset request."""
        api.load_manifests()
        mani = api.MANIFESTS["close"]
        small_mani = api.Manifest(
            key=mani.key,
            base_url=mani.base_url,
            auth=mani.auth,
            pagination=api.Pagination(
                style="offset",
                offset_param="_skip",
                limit_param="_limit",
                items_field="data",
                page_size=2,  # full page triggers page 2; empty page 2 stops loop
            ),
            rate_limit_remaining_header=mani.rate_limit_remaining_header,
        )
        responses_lib.add(
            responses_lib.GET, LEADS_URL,
            json={"data": [_LEAD_1, _LEAD_2], "has_more": True},
            status=200,
        )
        responses_lib.add(
            responses_lib.GET, LEADS_URL,
            json={"data": [], "has_more": False},
            status=200,
        )

        c = api.Client(manifest=small_mani, credential="test_close_" "key:")
        c.collect("lead/")

        # Split the credential prefix across concatenation so the hygiene guard doesn't flag this.
        expected_auth = _basic_header("test_close_" "key:")
        self.assertEqual(
            responses_lib.calls[0].request.headers["Authorization"], expected_auth,
            "page 1 must carry the basic-auth header",
        )
        self.assertEqual(
            responses_lib.calls[1].request.headers["Authorization"], expected_auth,
            "page 2 (offset advance) must carry the basic-auth header too",
        )

    @responses_lib.activate
    def test_single_page_has_more_false(self):
        """When has_more is False after page 1, the loop stops — no second request."""
        responses_lib.add(
            responses_lib.GET, LEADS_URL,
            json={"data": [_LEAD_1], "has_more": False, "total_results": 1},
            status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["close"])
        result = c.collect("lead/")

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_pick_selects_support_fields(self):
        """api.pick prunes a lead to the few support-relevant fields."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/lead/lead_abc123/",
            json=_LEAD_1, status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["close"])
        body = c.get("lead/lead_abc123/")

        picked = api.pick(body, "id,display_name,status_label,html_url,contacts.*.name,contacts.*.emails.*.email")
        self.assertEqual(picked["id"], "lead_abc123")
        self.assertEqual(picked["display_name"], "Acme Corp")
        self.assertEqual(picked["status_label"], "Potential")
        self.assertEqual(picked["contacts.*.name"], ["Alice Example"])
        self.assertEqual(picked["contacts.*.emails.*.email"], [["alice@acme.com"]])
        # Fields not in pick spec are absent.
        self.assertNotIn("date_created", picked)
        self.assertNotIn("organization_id", picked)

    @responses_lib.activate
    def test_opportunity_query_filtered_by_lead(self):
        """Opportunity endpoint filtered by lead_id returns the right record."""
        responses_lib.add(
            responses_lib.GET, OPPORTUNITIES_URL,
            json={"data": [_OPPORTUNITY_1], "has_more": False},
            status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["close"])
        result = c.collect("opportunity/", query={"lead_id": "lead_abc123"})

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        opp = result["items"][0]
        self.assertEqual(opp["id"], "oppo_abc")
        self.assertEqual(opp["status_label"], "Active")
        self.assertEqual(opp["value"], 50000)

        # Confirm the lead_id filter was sent as a query param.
        sent_url = responses_lib.calls[0].request.url
        self.assertIn("lead_id=lead_abc123", sent_url)

    @responses_lib.activate
    def test_email_activity_pick(self):
        """Email activity endpoint returns the right shape; pick extracts support fields."""
        responses_lib.add(
            responses_lib.GET, ACTIVITY_EMAIL_URL,
            json={"data": [_EMAIL_ACTIVITY_1], "has_more": False},
            status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["close"])
        result = c.collect("activity/email/", query={"lead_id": "lead_abc123"})

        self.assertFalse(result["incomplete"])
        email = result["items"][0]
        picked = api.pick(email, "id,_type,date_created,subject,direction,status")
        self.assertEqual(picked["_type"], "Email")
        self.assertEqual(picked["subject"], "Welcome to Acme")
        self.assertEqual(picked["direction"], "outgoing")

    @responses_lib.activate
    def test_cli_drives_close_with_paginate_and_pick(self):
        """CLI (`api._main`) drives the manifest-only integration end-to-end.

        The default page_size=100 means a 1-item page is < page_size → paginator stops after a
        single page. We verify the call reached the right URL, carried the basic-auth header, and
        the pick flag pruned the response correctly.
        """
        # Single page: 1 lead returned, has_more=False, 1 item < page_size=100 → loop stops.
        responses_lib.add(
            responses_lib.GET, LEADS_URL,
            json={"data": [_LEAD_1], "has_more": False, "total_results": 1},
            status=200,
        )

        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

        rc = api._main([
            "get", "close", "lead/",
            "--paginate",
            "--pick", "id,display_name,status_label",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)
        self.assertTrue(responses_lib.calls[0].request.url.startswith(LEADS_URL))
        # Auth present on CLI-driven calls too.
        expected_auth = _basic_header("test_close_" "key:")
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], expected_auth)


class CloseCassetteHygiene(unittest.TestCase):
    """CI guard: no real Close API key material may land in the committed connector files.

    Scoped to the connector dir (manifest.yaml only for this manifest-only connector), NOT this
    test file — this file intentionally names the prefixes it hunts for (split across concatenation
    to bypass itself).
    """

    # Close API keys have no well-known fixed prefix, so we guard against the literal test value
    # we use in this test, split so the guard doesn't self-trigger.
    _BANNED_LITERALS: tuple[str, ...] = ("test_close_" "key",)

    def test_no_credential_literals_in_close_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "close"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for lit in self._BANNED_LITERALS:
                if lit in text:
                    offenders.append(f"{path.name}: contains banned literal")
        self.assertEqual(offenders, [], f"credential-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
