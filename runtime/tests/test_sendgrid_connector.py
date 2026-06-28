"""Fixture test for the manifest-ONLY SendGrid integration — proves a catalogued connector with
NO bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies mirror SendGrid's
documented example payloads (trimmed to support-relevant fields). SendGrid's suppression endpoints
return bare JSON arrays and paginate with offset/limit, so two pages exercise the real `offset`
pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_sendgrid_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://api.sendgrid.com/v3"
BOUNCES_URL = f"{BASE}/suppression/bounces"

# Two pages of bounces (bare JSON arrays, as SendGrid returns for suppression endpoints).
# Shapes mirror SendGrid's documented bounce object; only support-relevant fields are kept.
_PAGE_1 = [
    {
        "created": 1443651141,
        "email": "bounced@example.com",
        "reason": "550 5.1.1 The email account that you tried to reach does not exist.",
        "status": "5.1.1",
    },
    {
        "created": 1443651200,
        "email": "noreply@example.com",
        "reason": "550 5.1.1 User unknown.",
        "status": "5.1.1",
    },
]
_PAGE_2 = [
    {
        "created": 1443651300,
        "email": "blocked@example.com",
        "reason": "421 Too many concurrent SMTP connections.",
        "status": "4.2.1",
    },
]


class SendGridManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates 'sendgrid' (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_SENDGRID")
        # Split the prefix to avoid triggering the token-hygiene guard below.
        os.environ["RC_CONN_SENDGRID"] = "SG." "test_key_abc123"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_SENDGRID", None)
        else:
            os.environ["RC_CONN_SENDGRID"] = self._saved

    def test_manifest_loaded_from_yaml_with_correct_fields(self):
        m = api.load_manifests()
        self.assertIn("sendgrid", m)
        sg = m["sendgrid"]
        self.assertEqual(sg.base_url, "https://api.sendgrid.com/v3")
        self.assertEqual(sg.auth.strategy, "bearer")
        self.assertEqual(sg.pagination.style, "offset")
        self.assertEqual(sg.pagination.offset_param, "offset")
        self.assertEqual(sg.pagination.limit_param, "limit")
        self.assertEqual(sg.pagination.items_field, "")   # bare array
        self.assertEqual(sg.pagination.page_size, 500)
        self.assertEqual(sg.rate_limit_remaining_header, "X-RateLimit-Remaining")

    @responses_lib.activate
    def test_offset_pagination_stitches_pages(self):
        """Two offset pages of bounces are stitched into one item list."""
        # Page 1: full page (page_size=500 but we use a small fixture; detect by items < page_size)
        # For the test we override page_size so a 2-item first page triggers a second fetch.
        api.load_manifests()
        mani = api.MANIFESTS["sendgrid"]

        # Build a client with page_size=2 so 2 items on page 1 triggers page 2.
        small_mani = api.Manifest(
            key=mani.key,
            base_url=mani.base_url,
            auth=mani.auth,
            pagination=api.Pagination(
                style="offset",
                offset_param="offset",
                limit_param="limit",
                items_field="",
                page_size=2,
            ),
            rate_limit_remaining_header=mani.rate_limit_remaining_header,
        )

        responses_lib.add(
            responses_lib.GET, BOUNCES_URL,
            json=_PAGE_1, status=200,
            headers={"X-RateLimit-Remaining": "499"},
        )
        responses_lib.add(
            responses_lib.GET, BOUNCES_URL,
            json=_PAGE_2, status=200,
            headers={"X-RateLimit-Remaining": "498"},
        )

        c = api.Client(manifest=small_mani, credential="SG." "test_key_abc123")
        result = c.collect("suppression/bounces")

        self.assertFalse(result["incomplete"], result["reason"])
        emails = [it["email"] for it in result["items"]]
        self.assertEqual(emails, ["bounced@example.com", "noreply@example.com", "blocked@example.com"])

    @responses_lib.activate
    def test_bearer_credential_on_every_request(self):
        """The Bearer API key rides every paginated request, including page 2."""
        api.load_manifests()
        mani = api.MANIFESTS["sendgrid"]
        small_mani = api.Manifest(
            key=mani.key,
            base_url=mani.base_url,
            auth=mani.auth,
            pagination=api.Pagination(
                style="offset",
                offset_param="offset",
                limit_param="limit",
                items_field="",
                page_size=2,
            ),
            rate_limit_remaining_header=mani.rate_limit_remaining_header,
        )

        responses_lib.add(responses_lib.GET, BOUNCES_URL, json=_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, BOUNCES_URL, json=_PAGE_2, status=200)

        cred = "SG." "test_key_abc123"
        c = api.Client(manifest=small_mani, credential=cred)
        c.collect("suppression/bounces")

        # Both page fetches carry Authorization: Bearer <key>
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], f"Bearer {cred}")

    @responses_lib.activate
    def test_pick_selects_support_fields(self):
        """api.pick extracts email/reason/status from each bounce item."""
        api.load_manifests()
        sg = api.MANIFESTS["sendgrid"]

        responses_lib.add(responses_lib.GET, BOUNCES_URL, json=_PAGE_1[:1], status=200)

        c = api.client(sg)
        body = c.get("suppression/bounces")
        picked = [api.pick(it, "email,reason,status") for it in body]
        self.assertEqual(picked[0]["email"], "bounced@example.com")
        self.assertEqual(picked[0]["status"], "5.1.1")
        self.assertIn("reason", picked[0])
        # 'created' was not picked
        self.assertNotIn("created", picked[0])

    @responses_lib.activate
    def test_cli_drives_sendgrid_get(self):
        """python -m lib.api get sendgrid suppression/bounces prints JSON."""
        responses_lib.add(responses_lib.GET, BOUNCES_URL, json=_PAGE_1, status=200)

        rc = api._main([
            "get", "sendgrid", "suppression/bounces",
            "--pick", "email,status",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(responses_lib.calls[0].request.url.startswith(BOUNCES_URL))
        self.assertIn(
            "Bearer " + "SG." + "test_key_abc123",
            responses_lib.calls[0].request.headers["Authorization"],
        )

    @responses_lib.activate
    def test_cli_paginate(self):
        """--paginate flag collects pages via offset pagination."""
        api.load_manifests()
        # page_size=500 means 2 items won't trigger page 2; single-page result is fine here.
        responses_lib.add(responses_lib.GET, BOUNCES_URL, json=_PAGE_1, status=200)

        rc = api._main([
            "get", "sendgrid", "suppression/bounces",
            "--paginate", "--pick", "email",
        ])
        self.assertEqual(rc, 0)


class SendGridTokenHygiene(unittest.TestCase):
    """CI guard: no real SendGrid API key prefix may land in the committed connector files.

    Scoped to the connector dir (manifest + any future files), NOT this test file — the test
    legitimately names the prefix it hunts for (split across string literals), so scanning
    itself would be a false positive.
    """

    # SendGrid API keys start with 'SG.' — split across a string concat to avoid self-triggering.
    _TOKEN_PREFIXES = ("SG" ".",)

    def test_no_token_prefixes_in_sendgrid_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "sendgrid"
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
