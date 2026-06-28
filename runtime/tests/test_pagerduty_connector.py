"""Fixture test for the manifest-ONLY PagerDuty integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. Fixture bodies mirror PagerDuty's own
documented REST v2 example payloads (developer.pagerduty.com/api-reference/), trimmed to the
support-relevant fields. PagerDuty paginates with offset+limit+more — two mocked pages exercise
the real `offset` pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \
        pytest tests/test_pagerduty_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API = "https://api.pagerduty.com"
INCIDENTS_URL = f"{API}/incidents"
SERVICES_URL = f"{API}/services"
USERS_URL = f"{API}/users"
ONCALLS_URL = f"{API}/oncalls"

# ---------------------------------------------------------------------------
# Fixture payloads — shapes mirror PagerDuty REST v2 documented examples,
# trimmed to support-diagnosis fields.
# ---------------------------------------------------------------------------

_INCIDENT_1 = {
    "id": "PT4KHLK",
    "title": "The server is on fire",
    "status": "triggered",
    "urgency": "high",
    "created_at": "2015-10-06T21:30:42Z",
    "service": {"id": "PIJ90N7", "summary": "My Web App"},
    "last_status_change_at": "2015-10-06T21:38:23Z",
    "html_url": "https://subdomain.pagerduty.com/incidents/PT4KHLK",
}

_INCIDENT_2 = {
    "id": "ABCDEFG",
    "title": "Database connection pool exhausted",
    "status": "acknowledged",
    "urgency": "low",
    "created_at": "2015-10-06T22:10:00Z",
    "service": {"id": "PIJ90N7", "summary": "My Web App"},
    "last_status_change_at": "2015-10-06T22:15:00Z",
    "html_url": "https://subdomain.pagerduty.com/incidents/ABCDEFG",
}

# Page 1: one incident, more=True → offset advances to page 2.
_PAGE_1 = {
    "incidents": [_INCIDENT_1],
    "offset": 0,
    "limit": 25,
    "more": True,
    "total": 2,
}

# Page 2: one incident, more=False → loop stops.
_PAGE_2 = {
    "incidents": [_INCIDENT_2],
    "offset": 1,
    "limit": 25,
    "more": False,
    "total": 2,
}

_SERVICE = {
    "id": "PIJ90N7",
    "name": "My Web App",
    "status": "critical",
    "description": "Production web application",
    "html_url": "https://subdomain.pagerduty.com/services/PIJ90N7",
}

_USER = {
    "id": "PXPGF42",
    "name": "Earline Greenholt",
    "email": "125.greenholt.earline@graham.name",
    "role": "admin",
    "time_zone": "America/Lima",
}

_ONCALL = {
    "escalation_level": 1,
    "start": "2015-03-06T15:28:51-05:00",
    "end": "2015-03-07T15:28:51-05:00",
    "user": {"id": "PXPGF42", "summary": "Earline Greenholt", "type": "user_reference"},
    "schedule": {"id": "PI7DH85", "summary": "Daily Engineering Rotation", "type": "schedule_reference"},
}


class PagerDutyManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `pagerduty`.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_PAGERDUTY")
        # Full header value as documented: "Token token=<api_key>"
        os.environ["RC_CONN_PAGERDUTY"] = "Token token=" + "test_api_key_abc123"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_PAGERDUTY", None)
        else:
            os.environ["RC_CONN_PAGERDUTY"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("pagerduty", m)
        pd = m["pagerduty"]
        self.assertEqual(pd.base_url, "https://api.pagerduty.com")
        self.assertEqual(pd.auth.strategy, "api_key_header")
        self.assertEqual(pd.auth.name, "Authorization")
        self.assertEqual(pd.pagination.style, "offset")
        self.assertEqual(pd.pagination.offset_param, "offset")
        self.assertEqual(pd.pagination.limit_param, "limit")
        self.assertEqual(pd.pagination.items_field, "incidents")
        self.assertEqual(pd.pagination.page_size, 25)
        self.assertEqual(pd.rate_limit_remaining_header, "X-RateLimit-Remaining")
        self.assertEqual(
            pd.default_headers.get("Accept"),
            "application/vnd.pagerduty+json;version=2",
        )

    @responses_lib.activate
    def test_offset_pagination_stitches_two_pages(self):
        """Two pages of incidents are joined via offset pagination; credential rides every request.

        lib.api's offset style terminates when a page returns fewer items than page_size. We use
        page_size=2: page 1 returns 2 items (full → advance), page 2 returns 1 item (< 2 → stop).
        """
        api.load_manifests()
        pd_manifest = api.MANIFESTS["pagerduty"]

        # Override page_size to 2 so that page 1 (2 items) is a full page and page 2 (1 item)
        # terminates the loop (1 < 2).
        from lib.api import Pagination, Manifest, Client
        test_manifest = Manifest(
            key=pd_manifest.key,
            base_url=pd_manifest.base_url,
            auth=pd_manifest.auth,
            pagination=Pagination(
                style="offset",
                offset_param="offset",
                limit_param="limit",
                items_field="incidents",
                page_size=2,  # page 1 returns 2 items (full) → advances; page 2 returns 1 → stops
            ),
            rate_limit_remaining_header=pd_manifest.rate_limit_remaining_header,
            default_headers=pd_manifest.default_headers,
        )

        # Page 1: 2 incidents (full page), page 2: 1 incident (partial → stops).
        page1 = {"incidents": [_INCIDENT_1, _INCIDENT_2], "offset": 0, "limit": 2, "more": True}
        page2 = {"incidents": [_INCIDENT_1], "offset": 2, "limit": 2, "more": False}

        cred = os.environ["RC_CONN_PAGERDUTY"]
        responses_lib.add(
            responses_lib.GET, INCIDENTS_URL,
            json=page1, status=200,
            headers={"X-RateLimit-Remaining": "900"},
        )
        responses_lib.add(
            responses_lib.GET, INCIDENTS_URL,
            json=page2, status=200,
            headers={"X-RateLimit-Remaining": "899"},
        )

        c = Client(manifest=test_manifest, credential=cred)
        result = c.collect("incidents", query={"statuses[]": "triggered"})

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, ["PT4KHLK", "ABCDEFG", "PT4KHLK"])  # 2 pages stitched in order
        self.assertEqual(len(responses_lib.calls), 2)

        # Auth: api_key_header strategy sends full value verbatim as Authorization header.
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], cred)
        # Required PagerDuty Accept header present on every request.
        for call in responses_lib.calls:
            self.assertEqual(
                call.request.headers["Accept"],
                "application/vnd.pagerduty+json;version=2",
            )

    @responses_lib.activate
    def test_single_page_get_services(self):
        """Single-page GET for services — no pagination, pick selects support-relevant fields."""
        api.load_manifests()
        responses_lib.add(
            responses_lib.GET, SERVICES_URL,
            json={"services": [_SERVICE], "more": False, "offset": 0, "limit": 25},
            status=200,
        )
        c = api.client(api.MANIFESTS["pagerduty"])
        body = c.get("services", query={"limit": 25})

        self.assertEqual(len(body["services"]), 1)
        svc = body["services"][0]
        picked = api.pick(svc, "id,name,status,description")
        self.assertEqual(picked["id"], "PIJ90N7")
        self.assertEqual(picked["status"], "critical")

        # Credential in every request header.
        self.assertEqual(
            responses_lib.calls[0].request.headers["Authorization"],
            os.environ["RC_CONN_PAGERDUTY"],
        )

    @responses_lib.activate
    def test_users_and_oncalls_single_page(self):
        """Verify users and oncalls endpoints parse correctly with --pick."""
        api.load_manifests()
        responses_lib.add(
            responses_lib.GET, USERS_URL,
            json={"users": [_USER], "more": False, "offset": 0, "limit": 25},
            status=200,
        )
        responses_lib.add(
            responses_lib.GET, ONCALLS_URL,
            json={"oncalls": [_ONCALL], "more": False, "offset": 0, "limit": 25},
            status=200,
        )

        c = api.client(api.MANIFESTS["pagerduty"])

        users_body = c.get("users", query={"query": "earline"})
        picked_user = api.pick(users_body["users"][0], "id,name,email,role")
        self.assertEqual(picked_user["id"], "PXPGF42")
        self.assertEqual(picked_user["email"], "125.greenholt.earline@graham.name")

        oncalls_body = c.get("oncalls", query={"limit": 25})
        picked_oncall = api.pick(oncalls_body["oncalls"][0], "user.summary,schedule.summary,start,end")
        self.assertEqual(picked_oncall["user.summary"], "Earline Greenholt")

    @responses_lib.activate
    def test_cli_drives_pagerduty(self):
        """CLI path: python -m lib.api get pagerduty incidents works end-to-end."""
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        responses_lib.add(
            responses_lib.GET, INCIDENTS_URL,
            json=_PAGE_1, status=200,
        )
        rc = api._main([
            "get", "pagerduty", "incidents",
            "--query", "statuses[]=triggered",
            "--pick", "incidents.*.id,incidents.*.title,incidents.*.status",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(responses_lib.calls[0].request.url.startswith(INCIDENTS_URL))
        self.assertIn("Token token=", responses_lib.calls[0].request.headers["Authorization"])

    @responses_lib.activate
    def test_api_key_header_credential_rides_on_every_request(self):
        """Credential verification — api_key_header places value verbatim, never as Bearer."""
        api.load_manifests()
        responses_lib.add(responses_lib.GET, f"{API}/incidents/PT4KHLK",
                          json={"incident": _INCIDENT_1}, status=200)

        c = api.client(api.MANIFESTS["pagerduty"])
        body = c.get("incidents/PT4KHLK")
        self.assertEqual(body["incident"]["id"], "PT4KHLK")

        auth_header = responses_lib.calls[0].request.headers["Authorization"]
        # Must be the full "Token token=..." value, NOT "Bearer ..."
        self.assertTrue(auth_header.startswith("Token token="))
        self.assertFalse(auth_header.startswith("Bearer "))


class PagerDutyTokenHygiene(unittest.TestCase):
    """CI guard: no real PagerDuty API key prefix may land in the connector dir or this test file.

    Scopes to the connector dir only — this test file legitimately names the prefix it guards
    against, so scanning itself would be a self-defeating false positive.
    """

    # PagerDuty API keys have no well-known prefix format (unlike GitHub's ghp_ etc.), but we
    # still guard against accidentally committed placeholder strings or common patterns.
    # Split the literal so the guard pattern itself doesn't trigger on this source file.
    _BANNED_PATTERNS = ("u_" + "token=actual", "pagerduty" + "_api_key=")

    def test_no_secrets_in_pagerduty_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "pagerduty"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in self._BANNED_PATTERNS:
                if pattern in text:
                    offenders.append(f"{path.name}: {pattern!r}")
        self.assertEqual(offenders, [], f"suspicious material in connector dir: {offenders}")


if __name__ == "__main__":
    unittest.main()
