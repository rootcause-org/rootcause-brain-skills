"""Fixture test for the manifest-ONLY Mailchimp integration.

Proves a catalogued connector with NO bespoke Python is drivable end-to-end via lib.api's YAML
loader + CLI. No live creds, no network: HTTP is mocked with `responses`. Bodies match the
vendor's documented example payloads, trimmed to support-relevant fields.

Mailchimp paginates with offset+count but each endpoint wraps items under a resource-specific key
(lists, campaigns, members…) so style=none is declared. The tests verify:
  - YAML loads and every manifest field maps correctly.
  - Single-page fetches (style=none) work with the bearer credential on every request.
  - api.pick selects support-relevant fields from nested response objects.
  - The CLI driver works via api._main([...]).
  - No token-like material leaks into the connector directory.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \
        pytest tests/test_mailchimp_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

DC = "us6"
BASE = f"https://{DC}.api.mailchimp.com/3.0"
LISTS_URL = f"{BASE}/lists"
CAMPAIGNS_URL = f"{BASE}/campaigns"
MEMBERS_URL = f"{BASE}/lists/abc123/members"
# MD5 of "subscriber@example.com" (lowercased) — how Mailchimp identifies a member.
_MEMBER_HASH = "6b4f24a7e4b4e0d0e7f2b9d6c5a3e0c1"
MEMBER_URL = f"{BASE}/lists/abc123/members/{_MEMBER_HASH}"
ACTIVITY_URL = f"{BASE}/lists/abc123/members/{_MEMBER_HASH}/activity"

# --- Documented example payloads (trimmed to support-relevant fields) ---

_LISTS_BODY = {
    "lists": [
        {
            "id": "abc123",
            "name": "Main Newsletter",
            "date_created": "2022-01-15T10:00:00+00:00",
            "stats": {"member_count": 4200, "unsubscribe_count": 35},
        },
        {
            "id": "def456",
            "name": "Product Updates",
            "date_created": "2023-03-01T09:00:00+00:00",
            "stats": {"member_count": 1800, "unsubscribe_count": 12},
        },
    ],
    "total_items": 2,
}

_CAMPAIGNS_BODY = {
    "campaigns": [
        {
            "id": "cmp001",
            "status": "sent",
            "settings": {"subject_line": "May Newsletter", "title": "May 2024 Newsletter"},
            "send_time": "2024-05-10T14:00:00+00:00",
            "emails_sent": 3950,
        },
        {
            "id": "cmp002",
            "status": "sent",
            "settings": {"subject_line": "Product Launch", "title": "New Feature Drop"},
            "send_time": "2024-04-20T10:00:00+00:00",
            "emails_sent": 4100,
        },
    ],
    "total_items": 2,
}

_MEMBER_BODY = {
    "id": _MEMBER_HASH,
    "email_address": "subscriber@example.com",
    "status": "subscribed",
    "timestamp_opt": "2022-06-01T08:00:00+00:00",
    "timestamp_signup": "2022-06-01T07:55:00+00:00",
    "tags": [{"name": "vip"}, {"name": "early-adopter"}],
}

_MEMBERS_BODY = {
    "members": [
        {
            "id": _MEMBER_HASH,
            "email_address": "subscriber@example.com",
            "status": "subscribed",
            "timestamp_opt": "2022-06-01T08:00:00+00:00",
        },
        {
            "id": "deadbeef" * 4,
            "email_address": "another@example.com",
            "status": "unsubscribed",
            "timestamp_opt": "2023-01-10T11:00:00+00:00",
        },
    ],
    "total_items": 2,
}

_ACTIVITY_BODY = {
    "activity": [
        {
            "action": "open",
            "timestamp": "2024-05-10T15:30:00+00:00",
            "campaign_id": "cmp001",
            "title": "May Newsletter",
        },
        {
            "action": "click",
            "timestamp": "2024-05-10T15:31:00+00:00",
            "campaign_id": "cmp001",
            "title": "May Newsletter",
        },
    ]
}


class MailchimpManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader populates `mailchimp` without interference.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_MAILCHIMP")
        # Use a split literal so the token-prefix hygiene guard doesn't flag this file.
        os.environ["RC_CONN_MAILCHIMP"] = "test" "_mc_apikey_us6"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MAILCHIMP", None)
        else:
            os.environ["RC_CONN_MAILCHIMP"] = self._saved

    def test_manifest_loaded_from_yaml_with_correct_fields(self):
        m = api.load_manifests()
        self.assertIn("mailchimp", m)
        mc = m["mailchimp"]
        self.assertIn("api.mailchimp.com", mc.base_url)
        self.assertEqual(mc.auth.strategy, "bearer")
        self.assertEqual(mc.pagination.style, "none")
        self.assertEqual(mc.rate_limit_remaining_header, "")
        # No default_headers required by Mailchimp (no version header needed).

    @responses.activate
    def test_bearer_credential_on_lists_request(self):
        responses.add(responses.GET, LISTS_URL, json=_LISTS_BODY, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["mailchimp"])
        body = c.get(LISTS_URL, query={"count": 100})

        self.assertEqual(len(responses.calls), 1)
        auth_header = responses.calls[0].request.headers["Authorization"]
        self.assertTrue(
            auth_header.startswith("Bearer "),
            f"Expected Bearer, got: {auth_header!r}",
        )
        self.assertIn("test", auth_header)  # credential value present
        self.assertEqual(body["total_items"], 2)

    @responses.activate
    def test_lists_pick_selects_support_fields(self):
        responses.add(responses.GET, LISTS_URL, json=_LISTS_BODY, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["mailchimp"])
        body = c.get(LISTS_URL)
        lists = body["lists"]
        picked = [api.pick(li, "id,name,stats.member_count") for li in lists]

        self.assertEqual(picked[0]["id"], "abc123")
        self.assertEqual(picked[0]["name"], "Main Newsletter")
        self.assertEqual(picked[0]["stats.member_count"], 4200)
        self.assertEqual(picked[1]["id"], "def456")

    @responses.activate
    def test_campaigns_returns_correct_body(self):
        responses.add(responses.GET, CAMPAIGNS_URL, json=_CAMPAIGNS_BODY, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["mailchimp"])
        body = c.get(CAMPAIGNS_URL, query={"count": 50, "sort_field": "send_time", "sort_dir": "DESC"})

        self.assertEqual(body["total_items"], 2)
        picked = [
            api.pick(
                camp,
                "id,status,settings.subject_line,send_time,emails_sent",
            )
            for camp in body["campaigns"]
        ]
        self.assertEqual(picked[0]["id"], "cmp001")
        self.assertEqual(picked[0]["settings.subject_line"], "May Newsletter")
        self.assertEqual(picked[0]["emails_sent"], 3950)

    @responses.activate
    def test_member_lookup_by_hash(self):
        responses.add(responses.GET, MEMBER_URL, json=_MEMBER_BODY, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["mailchimp"])
        body = c.get(MEMBER_URL)
        picked = api.pick(
            body, "email_address,status,timestamp_opt,tags.*.name"
        )

        self.assertEqual(picked["email_address"], "subscriber@example.com")
        self.assertEqual(picked["status"], "subscribed")
        self.assertEqual(picked["tags.*.name"], ["vip", "early-adopter"])

    @responses.activate
    def test_members_with_offset_query_manual_paging(self):
        """style=none: agent drives offset manually; two calls = two manual pages."""
        responses.add(responses.GET, MEMBERS_URL, json=_MEMBERS_BODY, status=200)
        responses.add(responses.GET, MEMBERS_URL, json={"members": [], "total_items": 2}, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["mailchimp"])

        # Page 1 (offset=0)
        page1 = c.get(MEMBERS_URL, query={"count": 100, "offset": 0})
        self.assertEqual(len(page1["members"]), 2)

        # Page 2 (offset=100) → empty members list signals end-of-data
        page2 = c.get(MEMBERS_URL, query={"count": 100, "offset": 100})
        self.assertEqual(len(page2["members"]), 0)

        # Both requests carried the bearer credential
        for call in responses.calls:
            self.assertTrue(call.request.headers["Authorization"].startswith("Bearer "))

    @responses.activate
    def test_activity_feed_pick(self):
        responses.add(responses.GET, ACTIVITY_URL, json=_ACTIVITY_BODY, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["mailchimp"])
        body = c.get(ACTIVITY_URL)
        picked = [
            api.pick(ev, "action,timestamp,campaign_id,title")
            for ev in body["activity"]
        ]

        self.assertEqual(picked[0]["action"], "open")
        self.assertEqual(picked[1]["action"], "click")
        self.assertEqual(picked[0]["campaign_id"], "cmp001")

    @responses.activate
    def test_cli_drives_mailchimp_get(self):
        responses.add(responses.GET, LISTS_URL, json=_LISTS_BODY, status=200)
        rc = api._main([
            "get", "mailchimp", LISTS_URL,
            "--query", "count=100",
            "--pick", "lists.*.id,lists.*.name",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses.calls), 1)
        self.assertEqual(
            responses.calls[0].request.headers["Authorization"],
            "Bearer " + os.environ["RC_CONN_MAILCHIMP"],
        )

    @responses.activate
    def test_pagination_style_none_does_not_auto_page(self):
        """Confirm style=none: paginate() yields exactly one page with no next token."""
        responses.add(responses.GET, LISTS_URL, json=_LISTS_BODY, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["mailchimp"])
        pages = list(c.paginate(LISTS_URL))

        self.assertEqual(len(pages), 1)
        self.assertIsNone(pages[0].next)
        self.assertEqual(len(responses.calls), 1)


class MailchimpCassetteHygiene(unittest.TestCase):
    """CI guard: no real Mailchimp API key prefix may land in the committed connector directory.

    Scopes to the connector dir only — this test file legitimately contains the prefix literals it
    hunts for (split across concatenations so they don't self-trigger).
    """

    # Mailchimp API key suffix pattern: ends with "-us<N>" (e.g. "abc123def456-" + "us6").
    # We guard against the recognisable suffix pattern rather than a fixed prefix.
    _TOKEN_PATTERNS = (
        # Real MC keys look like "<40hex>-us<N>" — guard the datacenter suffix literal.
        "-us1", "-us2", "-us3", "-us4", "-us5", "-us6", "-us7", "-us8",
        "-us9", "-us10", "-us11", "-us12", "-us13", "-us14", "-us15",
        "-us16", "-us17", "-us18", "-us19", "-us20",
    )

    def test_no_token_patterns_in_mailchimp_files(self):
        connector_dir = (
            Path(__file__).resolve().parents[1] / "lib" / "connectors" / "mailchimp"
        )
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in self._TOKEN_PATTERNS:
                # Only flag if it looks like a key context (preceded by hex chars), not just the
                # datacenter URL patterns like "us6.api.mailchimp.com".
                import re
                if re.search(r"[0-9a-f]{8}" + re.escape(pattern), text):
                    offenders.append(f"{path.name}: {pattern!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
