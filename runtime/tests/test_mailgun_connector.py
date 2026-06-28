"""Fixture test for the manifest-ONLY Mailgun integration — proves the catalogued connector with
NO bespoke Python is drivable end-to-end through lib.api's YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies mirror Mailgun's
documented example payloads (trimmed to support-relevant fields). Mailgun uses HTTP Basic Auth
(user="api", password=<key>) and wraps list results in an "items" array with a "paging" object
carrying the next-page URL — style=none, so the agent follows paging.next manually.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_mailgun_connector.py -q
"""

import base64
import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://api.mailgun.net/v3"
DOMAIN = "mg.example.com"
BOUNCES = f"{BASE}/{DOMAIN}/bounces"
EVENTS = f"{BASE}/{DOMAIN}/events"
DOMAINS = f"{BASE}/domains"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# ---------------------------------------------------------------------------

_BOUNCES_PAGE = {
    "items": [
        {
            "address": "alice@example.com",
            "code": "550",
            "error": "No such user",
            "created_at": "Thu, 13 Oct 2011 18:02:00 UTC",
        },
        {
            "address": "bob@example.com",
            "code": "552",
            "error": "Message too large",
            "created_at": "Fri, 14 Oct 2011 09:00:00 UTC",
        },
    ],
    "paging": {
        "first": f"{BOUNCES}?limit=100",
        "next": f"{BOUNCES}?limit=100&p=bob%40example.com",
        "previous": f"{BOUNCES}?limit=100",
        "last": f"{BOUNCES}?limit=100&p=bob%40example.com",
    },
}

_EVENTS_PAGE = {
    "items": [
        {
            "event": "failed",
            "recipient": "charlie@example.com",
            "reason": "bounce",
            "delivery-status": {
                "message": "5.1.1 User unknown",
                "code": 550,
            },
            "timestamp": 1512950614.428238,
        },
        {
            "event": "delivered",
            "recipient": "diana@example.com",
            "reason": None,
            "delivery-status": {
                "message": "",
                "code": 250,
            },
            "timestamp": 1512950712.123456,
        },
    ],
    "paging": {
        "next": f"{EVENTS}?page=next&p=W3siYSI6ICJmYWlsZWQifV0%3D",
        "previous": f"{EVENTS}?page=previous&p=W3siYSI6ICJmYWlsZWQifV0%3D",
    },
}

_DOMAINS_PAGE = {
    "total_count": 2,
    "items": [
        {
            "name": "mg.example.com",
            "state": "active",
            "type": "custom",
            "created_at": "Thu, 13 Oct 2011 18:02:00 UTC",
        },
        {
            "name": "sandbox1234.mailgun.org",
            "state": "active",
            "type": "sandbox",
            "created_at": "Fri, 14 Oct 2011 09:00:00 UTC",
        },
    ],
}

# ---------------------------------------------------------------------------
# Expected Basic Auth header for credential "api:key-test1234"
# ---------------------------------------------------------------------------
# Split as string concatenation so the token-prefix hygiene guard in this class
# doesn't flag its own source text.
_CRED = "api:" + "key-test1234"
_EXPECTED_AUTH = "Basic " + base64.b64encode(_CRED.encode()).decode()


class MailgunManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `mailgun`.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_MAILGUN")
        # Credential format: "api:<key>" — lib.api's `basic` strategy splits on ":"
        os.environ["RC_CONN_MAILGUN"] = _CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MAILGUN", None)
        else:
            os.environ["RC_CONN_MAILGUN"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("mailgun", m)
        mg = m["mailgun"]
        self.assertEqual(mg.base_url, "https://api.mailgun.net/v3")
        self.assertEqual(mg.auth.strategy, "basic")
        # style=none: paging.next is a full URL in the JSON body, followed manually
        self.assertEqual(mg.pagination.style, "none")
        self.assertEqual(mg.pagination.items_field, "items")
        self.assertEqual(mg.rate_limit_remaining_header, "X-RateLimit-Remaining")

    @responses.activate
    def test_basic_auth_credential_on_bounces_request(self):
        """Basic Auth header must carry the base64-encoded 'api:<key>' on every request."""
        responses.add(
            responses.GET,
            BOUNCES,
            json=_BOUNCES_PAGE,
            status=200,
            headers={"X-RateLimit-Remaining": "498"},
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["mailgun"])
        body = c.get(f"{DOMAIN}/bounces", query={"limit": 100})

        # Credential rides the request as HTTP Basic Auth.
        self.assertEqual(responses.calls[0].request.headers["Authorization"], _EXPECTED_AUTH)
        # Items are extracted correctly.
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(body["items"][0]["address"], "alice@example.com")
        self.assertEqual(body["items"][1]["code"], "552")

    @responses.activate
    def test_events_get_and_pick(self):
        """Events endpoint returns items+paging; pick selects support-relevant fields."""
        responses.add(
            responses.GET,
            EVENTS,
            json=_EVENTS_PAGE,
            status=200,
            headers={"X-RateLimit-Remaining": "499"},
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["mailgun"])
        body = c.get(f"{DOMAIN}/events", query={"event": "failed", "limit": 100})

        # Auth present.
        self.assertEqual(responses.calls[0].request.headers["Authorization"], _EXPECTED_AUTH)

        # paging.next available for manual follow.
        self.assertIn("paging", body)
        self.assertIn("next", body["paging"])
        self.assertTrue(body["paging"]["next"].startswith("https://"))

        # pick pre-selects the few fields relevant to support.
        items = body["items"]
        picked = [api.pick(it, "event,recipient,reason,delivery-status.message") for it in items]
        self.assertEqual(picked[0]["event"], "failed")
        self.assertEqual(picked[0]["recipient"], "charlie@example.com")
        self.assertEqual(picked[0]["delivery-status.message"], "5.1.1 User unknown")
        self.assertEqual(picked[1]["event"], "delivered")

    @responses.activate
    def test_single_page_fetch_style_none(self):
        """style=none: fetch_page returns one page; items extracted from `items` field."""
        responses.add(
            responses.GET,
            BOUNCES,
            json=_BOUNCES_PAGE,
            status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["mailgun"])
        page = c.fetch_page(f"{DOMAIN}/bounces", query={"limit": 100})

        self.assertEqual(len(page.items), 2)
        # style=none: next is always None (no auto-pagination — agent follows paging.next manually)
        self.assertIsNone(page.next)
        # Auth still applied.
        self.assertEqual(responses.calls[0].request.headers["Authorization"], _EXPECTED_AUTH)

    @responses.activate
    def test_collect_style_none_single_page(self):
        """collect() with style=none yields exactly one page and marks complete."""
        responses.add(responses.GET, BOUNCES, json=_BOUNCES_PAGE, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["mailgun"])
        result = c.collect(f"{DOMAIN}/bounces", query={"limit": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["address"], "alice@example.com")

    @responses.activate
    def test_domains_endpoint_pick(self):
        """GET /domains returns total_count + items; pick extracts domain metadata."""
        responses.add(responses.GET, DOMAINS, json=_DOMAINS_PAGE, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["mailgun"])
        body = c.get("domains", query={"limit": 100})

        self.assertEqual(responses.calls[0].request.headers["Authorization"], _EXPECTED_AUTH)
        self.assertEqual(body["total_count"], 2)

        picked = [api.pick(it, "name,state,type") for it in body["items"]]
        self.assertEqual(picked[0]["name"], "mg.example.com")
        self.assertEqual(picked[0]["state"], "active")
        self.assertEqual(picked[1]["type"], "sandbox")

    @responses.activate
    def test_cli_drives_mailgun_get(self):
        """python -m lib.api get mailgun <path> works end-to-end."""
        responses.add(responses.GET, EVENTS, json=_EVENTS_PAGE, status=200)

        rc = api._main([
            "get", "mailgun", f"{DOMAIN}/events",
            "--query", "event=failed",
            "--query", "limit=100",
            "--pick", "items.*.event,items.*.recipient",
        ])
        self.assertEqual(rc, 0)
        # Exactly one HTTP call was made with Basic Auth.
        self.assertEqual(len(responses.calls), 1)
        self.assertEqual(responses.calls[0].request.headers["Authorization"], _EXPECTED_AUTH)


class MailgunCassetteHygiene(unittest.TestCase):
    """CI guard: no real Mailgun API key prefix may land in the committed connector files.

    Scoped to the connector dir (manifest only — no cassettes for manifest-only connectors),
    NOT this test file itself — the test legitimately names the prefixes it hunts for.
    """

    # Mailgun private API key prefix (split to avoid self-flagging).
    _TOKEN_PREFIXES = ("key" "-",)

    def test_no_token_prefixes_in_mailgun_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "mailgun"
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
