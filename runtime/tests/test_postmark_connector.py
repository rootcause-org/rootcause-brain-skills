"""Fixture test for the manifest-ONLY Postmark integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. Fixture bodies mirror Postmark's own
documented payloads (postmarkapp.com/developer/api/messages-api, /bounce-api), trimmed to the
support-relevant fields. Postmark's outbound message search wraps items under `Messages` and
paginates with offset/count, so two mocked pages exercise the real `offset` style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_postmark_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://api.postmarkapp.com"
OUTBOUND_URL = f"{BASE}/messages/outbound"
BOUNCES_URL = f"{BASE}/bounces"

_TOKEN = "test-server-token-0000-1111-2222"  # obviously fake; Postmark tokens are GUIDs (no prefix)

# Two pages of outbound messages, wrapped under "Messages" with a TotalCount envelope, as Postmark
# returns. Shapes mirror the documented outbound-search message object, trimmed to support fields.
_PAGE_1 = {
    "TotalCount": 3,
    "Messages": [
        {
            "MessageID": "0acb0e93-1111-4f7d-9f3a-aaaaaaaaaaaa",
            "Recipients": ["user@example.com"],
            "From": "support@momentumtools.io",
            "Subject": "Your invoice",
            "Status": "Sent",
            "ReceivedAt": "2024-01-10T12:00:00Z",
        },
        {
            "MessageID": "0acb0e93-2222-4f7d-9f3a-bbbbbbbbbbbb",
            "Recipients": ["user@example.com"],
            "From": "support@momentumtools.io",
            "Subject": "Password reset",
            "Status": "Sent",
            "ReceivedAt": "2024-01-11T09:30:00Z",
        },
    ],
}
_PAGE_2 = {
    "TotalCount": 3,
    "Messages": [
        {
            "MessageID": "0acb0e93-3333-4f7d-9f3a-cccccccccccc",
            "Recipients": ["user@example.com"],
            "From": "support@momentumtools.io",
            "Subject": "Welcome",
            "Status": "Sent",
            "ReceivedAt": "2024-01-12T08:00:00Z",
        },
    ],
}

# One bounce, wrapped under "Bounces" (the non-default items_field) — mirrors the documented shape.
_BOUNCES = {
    "TotalCount": 1,
    "Bounces": [
        {
            "ID": 692560173,
            "Type": "HardBounce",
            "Email": "bounced@example.com",
            "BouncedAt": "2024-01-09T07:21:00Z",
            "Description": "The server was unable to deliver your message (ie. it does not exist).",
            "Inactive": True,
            "CanActivate": True,
        }
    ],
}


class PostmarkManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `postmark` (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_POSTMARK")
        os.environ["RC_CONN_POSTMARK"] = _TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_POSTMARK", None)
        else:
            os.environ["RC_CONN_POSTMARK"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("postmark", m)
        pm = m["postmark"]
        self.assertEqual(pm.base_url, "https://api.postmarkapp.com")
        self.assertEqual(pm.auth.strategy, "api_key_header")
        self.assertEqual(pm.auth.name, "X-Postmark-Server-Token")
        self.assertEqual(pm.pagination.style, "offset")
        self.assertEqual(pm.pagination.offset_param, "offset")
        self.assertEqual(pm.pagination.limit_param, "count")
        self.assertEqual(pm.pagination.items_field, "Messages")
        self.assertEqual(pm.pagination.page_size, 500)
        self.assertEqual(pm.default_headers.get("Accept"), "application/json")

    @responses_lib.activate
    def test_offset_pagination_stitches_pages_under_messages(self):
        """Two offset pages of outbound messages (wrapped under `Messages`) stitch into one list.

        lib.api's offset style terminates when a page returns fewer items than page_size; page_size=2
        means page 1 (2 items) advances and page 2 (1 item) stops.
        """
        api.load_manifests()
        mani = api.MANIFESTS["postmark"]
        small_mani = api.Manifest(
            key=mani.key,
            base_url=mani.base_url,
            auth=mani.auth,
            pagination=api.Pagination(
                style="offset",
                offset_param="offset",
                limit_param="count",
                items_field="Messages",
                page_size=2,
            ),
            rate_limit_remaining_header=mani.rate_limit_remaining_header,
        )

        responses_lib.add(responses_lib.GET, OUTBOUND_URL, json=_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, OUTBOUND_URL, json=_PAGE_2, status=200)

        c = api.Client(manifest=small_mani, credential=_TOKEN)
        result = c.collect("messages/outbound")

        self.assertFalse(result["incomplete"], result["reason"])
        subjects = [it["Subject"] for it in result["items"]]
        self.assertEqual(subjects, ["Your invoice", "Password reset", "Welcome"])
        # The server token rode as the Postmark header on every paginated request (page 2 included).
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["X-Postmark-Server-Token"], _TOKEN)
            self.assertNotIn("Authorization", call.request.headers)

    @responses_lib.activate
    def test_single_page_bounces_with_pick(self):
        """Bounces wrap under the non-default `Bounces` key — fetch one page and --pick the envelope."""
        api.load_manifests()
        responses_lib.add(responses_lib.GET, BOUNCES_URL, json=_BOUNCES, status=200)

        c = api.client(api.MANIFESTS["postmark"])
        body = c.get("bounces", query={"emailFilter": "bounced@example.com"})
        picked = api.pick(body, "Bounces.*.Type,Bounces.*.Email,Bounces.*.Inactive")
        self.assertEqual(picked["Bounces.*.Type"], ["HardBounce"])
        self.assertEqual(picked["Bounces.*.Email"], ["bounced@example.com"])
        self.assertEqual(picked["Bounces.*.Inactive"], [True])

    @responses_lib.activate
    def test_cli_drives_postmark_get_with_header_auth(self):
        responses_lib.add(responses_lib.GET, OUTBOUND_URL, json=_PAGE_1, status=200)
        rc = api._main([
            "get", "postmark", "messages/outbound",
            "--query", "recipient=user@example.com",
            "--pick", "Messages.*.MessageID,Messages.*.Status",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(responses_lib.calls[0].request.url.startswith(OUTBOUND_URL))
        self.assertEqual(responses_lib.calls[0].request.headers["X-Postmark-Server-Token"], _TOKEN)


if __name__ == "__main__":
    unittest.main()
