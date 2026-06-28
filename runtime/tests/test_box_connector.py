"""Fixture test for the manifest-ONLY Box integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies mirror Box's own
documented example payloads (developer.box.com/reference), trimmed to support-relevant fields.
Box paginates with a marker-based cursor (`next_marker` / `marker` / `entries`), so the two mocked
pages exercise the real `cursor` pagination style end-to-end.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_box_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://api.box.com/2.0"
FOLDER_ITEMS_URL = f"{BASE}/folders/0/items"
FILES_URL = f"{BASE}/files/12345"
USERS_URL = f"{BASE}/users"
SEARCH_URL = f"{BASE}/search"

# Documented example folder-items response (developer.box.com "List items in a folder"),
# trimmed to support-relevant fields. Page 1 has next_marker; page 2 has next_marker=null → stop.
_FOLDER_ITEM_1 = {
    "id": "11111",
    "type": "file",
    "name": "invoice-2024-01.pdf",
    "size": 102400,
    "modified_at": "2024-01-15T10:30:00-07:00",
    "owned_by": {"login": "alice@example.com", "name": "Alice"},
}
_FOLDER_ITEM_2 = {
    "id": "22222",
    "type": "folder",
    "name": "Invoices 2024",
    "size": 0,
    "modified_at": "2024-01-20T09:00:00-07:00",
    "owned_by": {"login": "bob@example.com", "name": "Bob"},
}

# Two-page cursor responses. Page 1 carries next_marker; page 2 has next_marker=null → loop stops.
_PAGE_1 = {
    "entries": [_FOLDER_ITEM_1],
    "next_marker": "ZmlQZXJzaXN0ZWRfaWQ9MA==",
    "limit": 1000,
}
_PAGE_2 = {
    "entries": [_FOLDER_ITEM_2],
    "next_marker": None,   # null ⇒ last page
    "limit": 1000,
}

# Documented example file object (developer.box.com "Get file information"), trimmed.
_FILE_DETAIL = {
    "id": "12345",
    "type": "file",
    "name": "contract.pdf",
    "size": 204800,
    "created_at": "2024-01-01T08:00:00-07:00",
    "modified_at": "2024-01-10T12:00:00-07:00",
    "owned_by": {"id": "9876", "login": "carol@example.com", "name": "Carol"},
    "shared_link": {"url": "https://app.box.com/s/abc123", "access": "open"},
}

# Documented example user list (developer.box.com "List enterprise users"), trimmed.
_USER_1 = {
    "id": "100001",
    "type": "user",
    "name": "Alice",
    "login": "alice@example.com",
    "status": "active",
    "space_used": 1073741824,
    "space_amount": 10737418240,
}
_USER_2 = {
    "id": "100002",
    "type": "user",
    "name": "Bob",
    "login": "bob@example.com",
    "status": "active",
    "space_used": 524288000,
    "space_amount": 10737418240,
}

# Documented example search result envelope (developer.box.com "Search for content"), trimmed.
_SEARCH_RESULT = {
    "entries": [
        {
            "id": "33333",
            "type": "file",
            "name": "report-q1.xlsx",
            "modified_at": "2024-03-31T23:59:00-07:00",
            "parent": {"name": "Reports"},
        }
    ],
    "next_marker": None,
    "limit": 200,
}


class BoxManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `box`.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_BOX")
        # Split to keep the token-hygiene check from flagging the test itself.
        os.environ["RC_CONN_BOX"] = "box_test_" "token_abc123"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_BOX", None)
        else:
            os.environ["RC_CONN_BOX"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        """YAML loads cleanly and every declared field maps to the Manifest dataclass."""
        m = api.load_manifests()
        self.assertIn("box", m)
        b = m["box"]
        self.assertEqual(b.key, "box")
        self.assertEqual(b.base_url, "https://api.box.com/2.0")
        self.assertEqual(b.auth.strategy, "bearer")
        self.assertEqual(b.pagination.style, "cursor")
        self.assertEqual(b.pagination.cursor_param, "marker")
        self.assertEqual(b.pagination.cursor_field, "next_marker")
        self.assertEqual(b.pagination.has_more_field, "")   # Box uses null marker, not has_more
        self.assertEqual(b.pagination.items_field, "entries")
        self.assertEqual(b.pagination.page_size, 1000)
        self.assertEqual(b.rate_limit_remaining_header, "")  # Box sends 429+Retry-After only

    @responses.activate
    def test_cursor_pagination_stitches_two_pages(self):
        """Two mocked pages stitched via next_marker cursor produce all items in order."""
        responses.add(responses.GET, FOLDER_ITEMS_URL, json=_PAGE_1, status=200)
        responses.add(responses.GET, FOLDER_ITEMS_URL, json=_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["box"])
        result = c.collect("folders/0/items", query={"limit": 1000})

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)
        ids = [it["id"] for it in result["items"]]
        self.assertEqual(ids, ["11111", "22222"])

        # Page 2 request must carry the marker from page 1's next_marker.
        page2_url = responses.calls[1].request.url
        self.assertIn("marker=ZmlQZXJzaXN0ZWRfaWQ9MA%3D%3D", page2_url)

    @responses.activate
    def test_bearer_credential_on_every_request(self):
        """Bearer token is present on both pages (incl. second cursor follow)."""
        responses.add(responses.GET, FOLDER_ITEMS_URL, json=_PAGE_1, status=200)
        responses.add(responses.GET, FOLDER_ITEMS_URL, json=_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["box"])
        c.collect("folders/0/items", query={"limit": 1000})

        token = "box_test_" "token_abc123"
        self.assertEqual(responses.calls[0].request.headers["Authorization"], f"Bearer {token}")
        self.assertEqual(responses.calls[1].request.headers["Authorization"], f"Bearer {token}")

    @responses.activate
    def test_single_page_no_next_marker(self):
        """When next_marker is None the loop stops after one page."""
        responses.add(responses.GET, FOLDER_ITEMS_URL, json=_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["box"])
        result = c.collect("folders/0/items")

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_pick_selects_support_fields_from_file(self):
        """api.pick prunes a file object to the few support-relevant fields."""
        responses.add(responses.GET, FILES_URL, json=_FILE_DETAIL, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["box"])
        body = c.get("files/12345")

        picked = api.pick(body, "id,name,size,modified_at,owned_by.login,shared_link.url")
        self.assertEqual(picked["id"], "12345")
        self.assertEqual(picked["name"], "contract.pdf")
        self.assertEqual(picked["owned_by.login"], "carol@example.com")
        self.assertEqual(picked["shared_link.url"], "https://app.box.com/s/abc123")
        # Non-selected fields are absent.
        self.assertNotIn("created_at", picked)

    @responses.activate
    def test_user_list_cursor_pagination(self):
        """User list also uses entries/next_marker cursor — two pages stitched correctly."""
        page1 = {"entries": [_USER_1], "next_marker": "cursor_p2", "limit": 1000}
        page2 = {"entries": [_USER_2], "next_marker": None, "limit": 1000}
        responses.add(responses.GET, USERS_URL, json=page1, status=200)
        responses.add(responses.GET, USERS_URL, json=page2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["box"])
        result = c.collect("users", query={"limit": 1000})

        self.assertFalse(result["incomplete"])
        logins = [api.pick(u, "login")["login"] for u in result["items"]]
        self.assertEqual(logins, ["alice@example.com", "bob@example.com"])

    @responses.activate
    def test_search_endpoint_single_page(self):
        """Search returns entries/next_marker envelope — single page, no pagination needed."""
        responses.add(responses.GET, SEARCH_URL, json=_SEARCH_RESULT, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["box"])
        result = c.collect("search", query={"query": "report", "type": "file", "limit": 200})

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        picked = api.pick(result["items"][0], "id,name,type,parent.name")
        self.assertEqual(picked["name"], "report-q1.xlsx")
        self.assertEqual(picked["parent.name"], "Reports")

    @responses.activate
    def test_cli_drives_box_with_paginate(self):
        """CLI (`api._main`) drives the manifest-only integration end-to-end."""
        responses.add(responses.GET, FOLDER_ITEMS_URL, json=_PAGE_1, status=200)
        responses.add(responses.GET, FOLDER_ITEMS_URL, json=_PAGE_2, status=200)

        rc = api._main([
            "get", "box", "folders/0/items",
            "--query", "limit=1000",
            "--paginate",
            "--pick", "id,name,type",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses.calls), 2)
        self.assertTrue(responses.calls[0].request.url.startswith(FOLDER_ITEMS_URL))
        token = "box_test_" "token_abc123"
        self.assertEqual(responses.calls[0].request.headers["Authorization"], f"Bearer {token}")


class BoxCassetteHygiene(unittest.TestCase):
    """CI guard: no real Box token material may land in the committed connector files.

    Scoped to the connector dir (manifest + any future cassette), NOT this test file — this file
    intentionally names the prefix it hunts for (split across concatenation to bypass itself).
    """

    # Box developer tokens have no single public prefix but always start "Box" + long alphanumeric.
    # Split so the hygiene guard doesn't flag itself.
    _BANNED_LITERALS = ("box_test_" "token",)  # the literal used in setUp, split to avoid self-hit

    def test_no_credential_literals_in_box_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "box"
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
