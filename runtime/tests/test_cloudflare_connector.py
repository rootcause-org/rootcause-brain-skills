"""Fixture test for the manifest-ONLY Cloudflare integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. Fixture bodies match Cloudflare's
DOCUMENTED example payloads (developers.cloudflare.com), trimmed to support-relevant fields.
Cloudflare paginates by 1-based PAGE NUMBER (`page`/`per_page` params) with a `result_info`
envelope, so two mocked pages exercise the real `page` pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_cloudflare_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API = "https://api.cloudflare.com/client/v4"
ZONES_URL = f"{API}/zones"

# Documented example zone objects (developers.cloudflare.com/api/resources/zones/methods/list/).
# Trimmed to support-relevant fields. Page 1 returns 1 zone and signals page 2 exists.
_ZONE_1 = {
    "id": "023e105f4ecef8ad9ca31a8372d0c353",
    "name": "example.com",
    "status": "active",
    "plan": {"name": "Free Website"},
    "account": {"id": "01a7362d577a6c3019a474fd6f485823", "name": "Demo Account"},
    "name_servers": ["ns1.cloudflare.com", "ns2.cloudflare.com"],
    "original_name_servers": ["ns1.example.com"],
    "paused": False,
    "type": "full",
    "created_on": "2014-01-01T05:20:00.12345Z",
    "modified_on": "2014-01-01T05:20:00.12345Z",
}
_ZONE_2 = {
    "id": "99a7362d577a6c3019a474fd6f485823",
    "name": "staging.example.com",
    "status": "active",
    "plan": {"name": "Pro Website"},
    "account": {"id": "01a7362d577a6c3019a474fd6f485823", "name": "Demo Account"},
    "name_servers": ["ns1.cloudflare.com", "ns2.cloudflare.com"],
    "original_name_servers": [],
    "paused": False,
    "type": "full",
    "created_on": "2020-06-15T10:00:00.00000Z",
    "modified_on": "2020-06-15T10:00:00.00000Z",
}

def _zones_page(zones, page, total_pages, per_page=50):
    """Build a Cloudflare-style list response envelope."""
    return {
        "success": True,
        "errors": [],
        "messages": [],
        "result": zones,
        "result_info": {
            "page": page,
            "per_page": per_page,
            "count": len(zones),
            "total_count": total_pages * per_page,
            "total_pages": total_pages,
        },
    }


class CloudflareManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates 'cloudflare' (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_CLOUDFLARE")
        # Use a split literal so the hygiene guard in this file doesn't flag itself.
        os.environ["RC_CONN_CLOUDFLARE"] = "Bearer_" + "test_token_abc123"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_CLOUDFLARE", None)
        else:
            os.environ["RC_CONN_CLOUDFLARE"] = self._saved

    def test_manifest_loaded_from_yaml_with_page_pagination(self):
        """YAML loader creates a Manifest with every field mapped correctly."""
        m = api.load_manifests()
        self.assertIn("cloudflare", m)
        cf = m["cloudflare"]
        self.assertEqual(cf.base_url, "https://api.cloudflare.com/client/v4")
        self.assertEqual(cf.auth.strategy, "bearer")
        self.assertEqual(cf.pagination.style, "page")
        self.assertEqual(cf.pagination.page_param, "page")
        self.assertEqual(cf.pagination.page_start, 1)  # 1-based
        self.assertEqual(cf.pagination.limit_param, "per_page")
        self.assertEqual(cf.pagination.items_field, "result")
        self.assertEqual(cf.pagination.page_size, 50)
        # rate_limit is empty (compound header, not parseable as a plain integer)
        self.assertEqual(cf.rate_limit_remaining_header, "")
        # No extra required headers for Cloudflare REST
        self.assertNotIn("X-Cloudflare-Version", cf.default_headers)

    @responses_lib.activate
    def test_page_number_pagination_stitches_two_pages(self):
        """Two pages are fetched by 1-based page NUMBER (page=1,2); bearer rides both requests."""
        # Page 1 must be "full" (len == page_size=50) to trigger page 2; page 2 is short → stop.
        page1_zones = [_ZONE_1] * 50
        page2_zones = [_ZONE_2]   # partial page → signals last page

        responses_lib.add(
            responses_lib.GET, ZONES_URL,
            json=_zones_page(page1_zones, page=1, total_pages=2, per_page=50),
            status=200,
        )
        responses_lib.add(
            responses_lib.GET, ZONES_URL,
            json=_zones_page(page2_zones, page=2, total_pages=2, per_page=50),
            status=200,
        )

        api.load_manifests()
        c = api.client(api.MANIFESTS["cloudflare"])
        result = c.collect("zones")

        self.assertFalse(result["incomplete"], result["reason"])
        # 50 from page 1 + 1 from page 2
        self.assertEqual(len(result["items"]), 51)

        # Page NUMBER advances 1 → 2 (NOT an item-count offset like 0 → 50).
        self.assertEqual(len(responses_lib.calls), 2)
        self.assertIn("page=1", responses_lib.calls[0].request.url)
        self.assertIn("page=2", responses_lib.calls[1].request.url)

        # Bearer token on both requests
        for call in responses_lib.calls:
            auth_header = call.request.headers.get("Authorization", "")
            self.assertTrue(auth_header.startswith("Bearer "), auth_header)

    @responses_lib.activate
    def test_bearer_credential_on_every_request(self):
        """Even a single-page call carries the Authorization header."""
        responses_lib.add(
            responses_lib.GET, ZONES_URL,
            json=_zones_page([_ZONE_1], page=1, total_pages=1, per_page=50),
            status=200,
        )
        api.load_manifests()
        c = api.client(api.MANIFESTS["cloudflare"])
        page = c.fetch_page("zones")
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0]["name"], "example.com")
        auth = responses_lib.calls[0].request.headers["Authorization"]
        self.assertIn("Bearer", auth)

    @responses_lib.activate
    def test_pick_selects_support_relevant_zone_fields(self):
        """api.pick pre-selects the fields an agent actually needs for support diagnosis."""
        responses_lib.add(
            responses_lib.GET, ZONES_URL,
            json=_zones_page([_ZONE_1, _ZONE_2], page=1, total_pages=1, per_page=50),
            status=200,
        )
        api.load_manifests()
        c = api.client(api.MANIFESTS["cloudflare"])
        result = c.collect("zones")

        picked = [api.pick(it, "name,status,plan.name,account.id") for it in result["items"]]
        self.assertEqual(picked[0]["name"], "example.com")
        self.assertEqual(picked[0]["status"], "active")
        self.assertEqual(picked[0]["plan.name"], "Free Website")
        self.assertEqual(picked[0]["account.id"], "01a7362d577a6c3019a474fd6f485823")
        self.assertEqual(picked[1]["name"], "staging.example.com")
        self.assertEqual(picked[1]["plan.name"], "Pro Website")

    @responses_lib.activate
    def test_cli_drives_cloudflare_paginate(self):
        """The manifest-only CLI path (lib.api _main) works end-to-end with --paginate."""
        page1_zones = [_ZONE_1] * 50
        page2_zones = [_ZONE_2]

        responses_lib.add(
            responses_lib.GET, ZONES_URL,
            json=_zones_page(page1_zones, page=1, total_pages=2, per_page=50),
            status=200,
        )
        responses_lib.add(
            responses_lib.GET, ZONES_URL,
            json=_zones_page(page2_zones, page=2, total_pages=2, per_page=50),
            status=200,
        )

        rc = api._main([
            "get", "cloudflare", "zones",
            "--paginate",
            "--pick", "name,status",
        ])
        self.assertEqual(rc, 0)
        # Both pages fetched → 2 HTTP calls
        self.assertEqual(len(responses_lib.calls), 2)
        # Bearer rode on the first call
        self.assertIn(
            "Bearer",
            responses_lib.calls[0].request.headers["Authorization"],
        )

    @responses_lib.activate
    def test_cli_single_get_no_paginate(self):
        """Single GET (no --paginate) returns the raw result object, bearer is present."""
        DNS_URL = f"{API}/zones/023e105f4ecef8ad9ca31a8372d0c353/dns_records"
        dns_response = {
            "success": True,
            "errors": [],
            "messages": [],
            "result": [
                {
                    "id": "372e67954025e0ba6aaa6d586b9e0b59",
                    "type": "A",
                    "name": "example.com",
                    "content": "198.51.100.4",
                    "proxied": True,
                    "ttl": 1,
                    "created_on": "2014-01-01T05:20:00.12345Z",
                    "modified_on": "2014-01-01T05:20:00.12345Z",
                }
            ],
            "result_info": {"page": 1, "per_page": 50, "count": 1, "total_count": 1, "total_pages": 1},
        }
        responses_lib.add(responses_lib.GET, DNS_URL, json=dns_response, status=200)

        rc = api._main([
            "get", "cloudflare",
            "zones/023e105f4ecef8ad9ca31a8372d0c353/dns_records",
            "--pick", "result.0.type,result.0.name,result.0.content,result.0.proxied",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)
        self.assertIn("Bearer", responses_lib.calls[0].request.headers["Authorization"])


class CloudflareCassetteHygiene(unittest.TestCase):
    """CI guard: no real Cloudflare API token prefix may land in the committed manifest/fixtures.

    Scopes to the connector dir only — this test file itself legitimately names the prefixes
    it hunts for and must not be scanned (otherwise it is a trivial false positive).
    """

    # Cloudflare API token prefix. Split across two string literals so this guard doesn't
    # flag ITSELF as a leak.
    _TOKEN_PREFIXES = ("cf" "_",)

    def test_no_token_prefixes_in_cloudflare_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "cloudflare"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
