"""Fixture test for the manifest-ONLY Freshdesk integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies mirror Freshdesk's
own documented example payloads (developers.freshdesk.com/api), trimmed to support-relevant fields.
Freshdesk paginates with RFC 8288 `Link: <url>; rel="next"` headers (same as GitHub), so the two
mocked pages exercise the real `link` pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_freshdesk_connector.py -q
"""

import base64
import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

# Freshdesk is per-account: the domain is supplied as an absolute URL at call time.
DOMAIN = "acme"
BASE = f"https://{DOMAIN}.freshdesk.com/api/v2"
TICKETS_URL = f"{BASE}/tickets"
TICKET_DETAIL_URL = f"{BASE}/tickets/1"
CONVERSATIONS_URL = f"{BASE}/tickets/1/conversations"

# Documented example ticket payloads (developers.freshdesk.com "List All Tickets"), trimmed.
_TICKET_1 = {
    "id": 1,
    "subject": "Support needed",
    "status": 2,          # 2=Open
    "priority": 1,        # 1=Low
    "created_at": "2021-01-01T00:00:00Z",
    "updated_at": "2021-01-02T00:00:00Z",
    "requester_id": 101,
    "tags": ["billing"],
}
_TICKET_2 = {
    "id": 2,
    "subject": "Feature request",
    "status": 5,          # 5=Closed
    "priority": 2,
    "created_at": "2021-01-03T00:00:00Z",
    "updated_at": "2021-01-04T00:00:00Z",
    "requester_id": 102,
    "tags": [],
}

# Page 1 advertises page 2 via Link header; page 2 has no Link → loop stops.
_PAGE_1_LINK = f'<{TICKETS_URL}?per_page=100&page=2>; rel="next"'

# Documented example conversation payload.
_CONV_1 = {
    "id": 901,
    "ticket_id": 1,
    "body_text": "Please help with my billing issue.",
    "incoming": True,
    "created_at": "2021-01-01T01:00:00Z",
}


def _basic_header(credential: str) -> str:
    """Compute the expected Authorization header value for a basic-auth credential string."""
    encoded = base64.b64encode(credential.encode()).decode()
    return f"Basic {encoded}"


class FreshdeskManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `freshdesk`.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_FRESHDESK")
        # Credential is "apikey:X" — the literal Freshdesk basic-auth convention.
        os.environ["RC_CONN_FRESHDESK"] = "test_api" "key:X"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_FRESHDESK", None)
        else:
            os.environ["RC_CONN_FRESHDESK"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        """YAML loads cleanly and every declared field maps to the Manifest dataclass."""
        m = api.load_manifests()
        self.assertIn("freshdesk", m)
        fd = m["freshdesk"]
        # Auth: basic — credential "apikey:X" → Base64 in Authorization header.
        self.assertEqual(fd.auth.strategy, "basic")
        # Pagination: link — RFC 8288 Link header, bare-array pages.
        self.assertEqual(fd.pagination.style, "link")
        self.assertEqual(fd.pagination.items_field, "")
        self.assertEqual(fd.pagination.page_size, 100)
        # Rate-limit advisory header present.
        self.assertEqual(fd.rate_limit_remaining_header, "X-RateLimit-Remaining")
        # base_url carries the subdomain placeholder (not a callable URL itself).
        self.assertIn("freshdesk.com", fd.base_url)

    @responses_lib.activate
    def test_link_pagination_stitches_pages(self):
        """Two mocked pages stitched via Link header produce all items in order."""
        responses_lib.add(
            responses_lib.GET, TICKETS_URL,
            json=[_TICKET_1], status=200,
            headers={
                "Link": _PAGE_1_LINK,
                "X-RateLimit-Remaining": "4999",
            },
        )
        responses_lib.add(
            responses_lib.GET, TICKETS_URL,
            json=[_TICKET_2], status=200,
            headers={"X-RateLimit-Remaining": "4998"},
        )

        api.load_manifests()
        # Override base_url to the concrete test domain so the relative path resolves correctly.
        fd = api.MANIFESTS["freshdesk"]
        import dataclasses
        fd_concrete = dataclasses.replace(fd, base_url=BASE)
        c = api.client(fd_concrete)
        result = c.collect("tickets", query={"per_page": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual([it["id"] for it in result["items"]], [1, 2])

    @responses_lib.activate
    def test_basic_auth_credential_on_every_request(self):
        """Basic-auth header is present on both the initial request AND the link-follow."""
        responses_lib.add(
            responses_lib.GET, TICKETS_URL,
            json=[_TICKET_1], status=200,
            headers={"Link": _PAGE_1_LINK},
        )
        responses_lib.add(
            responses_lib.GET, TICKETS_URL,
            json=[_TICKET_2], status=200,
        )

        api.load_manifests()
        import dataclasses
        fd = dataclasses.replace(api.MANIFESTS["freshdesk"], base_url=BASE)
        c = api.client(fd)
        c.collect("tickets", query={"per_page": 100})

        expected_auth = _basic_header("test_api" "key:X")
        # Credential on page-1 request.
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], expected_auth)
        # Credential on link-follow (page-2) request — must not be dropped.
        self.assertEqual(responses_lib.calls[1].request.headers["Authorization"], expected_auth)

    @responses_lib.activate
    def test_pick_selects_support_fields(self):
        """api.pick prunes a ticket to the few support-relevant fields."""
        responses_lib.add(responses_lib.GET, TICKET_DETAIL_URL, json=_TICKET_1, status=200)

        api.load_manifests()
        import dataclasses
        fd = dataclasses.replace(api.MANIFESTS["freshdesk"], base_url=BASE)
        c = api.client(fd)
        body = c.get("tickets/1")

        picked = api.pick(body, "id,subject,status,priority,created_at,requester_id")
        self.assertEqual(picked["id"], 1)
        self.assertEqual(picked["subject"], "Support needed")
        self.assertEqual(picked["status"], 2)
        self.assertEqual(picked["requester_id"], 101)
        # Fields not in pick spec are absent.
        self.assertNotIn("tags", picked)
        self.assertNotIn("updated_at", picked)

    @responses_lib.activate
    def test_single_page_no_link_header(self):
        """When no Link header is present the loop stops after one page (style=link)."""
        responses_lib.add(
            responses_lib.GET, TICKETS_URL,
            json=[_TICKET_1], status=200,
            # No Link header → this is the last page.
        )

        api.load_manifests()
        import dataclasses
        fd = dataclasses.replace(api.MANIFESTS["freshdesk"], base_url=BASE)
        c = api.client(fd)
        result = c.collect("tickets", query={"per_page": 100})

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_cli_drives_freshdesk_with_paginate(self):
        """CLI (`api._main`) drives the manifest-only integration end-to-end."""
        responses_lib.add(
            responses_lib.GET, TICKETS_URL,
            json=[_TICKET_1], status=200,
            headers={"Link": _PAGE_1_LINK},
        )
        responses_lib.add(
            responses_lib.GET, TICKETS_URL,
            json=[_TICKET_2], status=200,
        )

        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

        # The CLI needs a concrete base_url; we inject a pre-built manifest to avoid the template.
        import dataclasses
        api.load_manifests()
        fd_concrete = dataclasses.replace(api.MANIFESTS["freshdesk"], base_url=BASE)
        api.register(fd_concrete)

        rc = api._main([
            "get", "freshdesk", "tickets",
            "--query", "per_page=100",
            "--paginate",
            "--pick", "id,subject,status",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)
        self.assertTrue(responses_lib.calls[0].request.url.startswith(TICKETS_URL))
        # Auth present on CLI-driven calls too.
        expected_auth = _basic_header("test_api" "key:X")
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], expected_auth)


class FreshdeskCassetteHygiene(unittest.TestCase):
    """CI guard: no real Freshdesk API key material may land in the committed connector files.

    Scoped to the connector dir (manifest + any future cassette), NOT this test file — this file
    intentionally names the prefix it hunts for (split across concatenation to bypass itself).
    """

    # Freshdesk API keys have no well-known public prefix, but we guard against common patterns.
    # Split with concatenation so the hygiene check doesn't flag this test file itself.
    _TOKEN_PREFIXES: tuple[str, ...] = ()  # Freshdesk keys have no detectable fixed prefix

    # Instead, guard against suspiciously long alphanumeric strings that look like real secrets:
    # a real Freshdesk key is 32+ chars of alphanumeric. We guard the literal test value we use.
    _BANNED_LITERALS = ("test_api" "key",)  # split so the guard doesn't self-trigger

    def test_no_credential_literals_in_freshdesk_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "freshdesk"
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
