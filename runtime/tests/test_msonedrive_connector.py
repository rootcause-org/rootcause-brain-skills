"""Fixture test for the Microsoft OneDrive connector (script connector, force-code trigger d).

Graph paginates via ``@odata.nextLink`` — a full absolute URL in the JSON body. The connector's
``collect_odata()`` follows those URLs directly via ``lib.api.Client._send_url``.

No live creds, no network: HTTP is mocked with ``responses``. Bodies are Microsoft Graph's own
DOCUMENTED example payloads (graph.microsoft.com), trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \
        pytest tests/test_msonedrive_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import msonedrive  # noqa: E402

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
CHILDREN_URL = f"{GRAPH_BASE}/me/drive/root/children"
SEARCH_URL = f"{GRAPH_BASE}/me/drive/root/search(q='quarterly report')"
RECENT_URL = f"{GRAPH_BASE}/me/drive/recent"
DRIVE_URL = f"{GRAPH_BASE}/me/drive"

# ---------------------------------------------------------------------------
# Documented example DriveItem payloads (trimmed to support-relevant fields).
# Shape mirrors the Graph "List DriveItem children" docs example.
# ---------------------------------------------------------------------------

_FILE_1 = {
    "id": "01BYE5RZY6DSDSZK37BFZLHGP2D4RQPMMX",
    "name": "myfile.jpg",
    "size": 2097152,
    "lastModifiedDateTime": "2023-08-14T10:30:00Z",
    "createdDateTime": "2023-07-01T09:00:00Z",
    "webUrl": "https://onedrive.live.com/redir?resid=01BYE5RZY6DSDSZK37BFZLHGP2D4RQPMMX",
    "file": {"mimeType": "image/jpeg"},
}
_FOLDER_1 = {
    "id": "01BYE5RZY6DSDSZK37BFZLHGP2D4RQPMMY",
    "name": "Documents",
    "size": 0,
    "lastModifiedDateTime": "2023-09-01T12:00:00Z",
    "createdDateTime": "2023-01-01T00:00:00Z",
    "webUrl": "https://onedrive.live.com/redir?resid=01BYE5RZY6DSDSZK37BFZLHGP2D4RQPMMY",
    "folder": {"childCount": 4},
}
_FILE_2 = {
    "id": "01BYE5RZY6DSDSZK37BFZLHGP2D4RQPMMZ",
    "name": "quarterly report.xlsx",
    "size": 1048576,
    "lastModifiedDateTime": "2023-10-10T08:00:00Z",
    "createdDateTime": "2023-10-01T00:00:00Z",
    "webUrl": "https://onedrive.live.com/redir?resid=01BYE5RZY6DSDSZK37BFZLHGP2D4RQPMMZ",
    "file": {"mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
}

# Page 1 response: two items + @odata.nextLink → page 2.
_PAGE_1_BODY = {
    "value": [_FILE_1, _FOLDER_1],
    "@odata.nextLink": f"{GRAPH_BASE}/me/drive/root/children?$skiptoken=asdlnjnkj1nalkm",
}
_PAGE_1_NEXT = f"{GRAPH_BASE}/me/drive/root/children?$skiptoken=asdlnjnkj1nalkm"

# Page 2 response: one item + no @odata.nextLink → pagination stops.
_PAGE_2_BODY = {
    "value": [_FILE_2],
}

_SEARCH_BODY = {
    "value": [
        {
            "id": "01BYE5RZY6DSDSZK37BFZLHGP2D4RQPMMW",
            "name": "quarterly report.xlsx",
            "size": 1048576,
            "lastModifiedDateTime": "2023-10-10T08:00:00Z",
            "createdDateTime": "2023-10-01T00:00:00Z",
            "webUrl": "https://onedrive.live.com/redir?resid=01BYE5RZY6DSDSZK37BFZLHGP2D4RQPMMW",
            "file": {"mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            "searchResult": {"onClickTelemetryUrl": "https://bing.com/abc"},
        },
    ],
    "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#Collection(microsoft.graph.driveItem)",
    # Single-page search result — no nextLink.
}

_DRIVE_BODY = {
    "id": "b!abc123",
    "name": "OneDrive",
    "driveType": "personal",
    "owner": {"user": {"displayName": "Daron Spektor", "email": "daron@example.com"}},
    "quota": {"used": 536870912, "total": 5368709120},
    "webUrl": "https://onedrive.live.com/?id=root",
}

_RECENT_BODY = {
    "value": [_FILE_2, _FILE_1],
}


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class MsOneDriveManifest(unittest.TestCase):
    """YAML manifest loads correctly via lib.api's loader and maps every field."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_MSONEDRIVE")
        os.environ["RC_CONN_MSONEDRIVE"] = "ms_test_" + "token_abc"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSONEDRIVE", None)
        else:
            os.environ["RC_CONN_MSONEDRIVE"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        # Force reload so the YAML path is exercised (not the register() in __init__.py).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        m = api.load_manifests()
        self.assertIn("msonedrive", m)
        mani = m["msonedrive"]
        self.assertEqual(mani.base_url, "https://graph.microsoft.com/v1.0")
        self.assertEqual(mani.auth.strategy, "bearer")
        self.assertEqual(mani.pagination.style, "none")
        self.assertEqual(mani.pagination.items_field, "value")
        self.assertEqual(mani.rate_limit_remaining_header, "")

    def test_register_wins_over_yaml(self):
        # The connector's register() is called at import time. Verify the registered manifest
        # takes precedence: load_manifests() with a pre-registered key must NOT overwrite it with
        # the YAML version (the explicit register() is the source of truth).
        # Restore the module-level registration (cleared in setUp).
        msonedrive.MANIFEST  # ensure module is loaded; re-register explicitly.
        api.register(msonedrive.MANIFEST)
        self.assertIn("msonedrive", api.MANIFESTS)
        # The registered entry must NOT be in _YAML_LOADED_KEYS (i.e. it's an explicit reg).
        self.assertNotIn("msonedrive", api._YAML_LOADED_KEYS)
        # Now load_manifests() must leave the explicit registration alone.
        api.load_manifests()
        self.assertNotIn("msonedrive", api._YAML_LOADED_KEYS)
        self.assertEqual(api.MANIFESTS["msonedrive"].key, "msonedrive")


class MsOneDriveOdataPagination(unittest.TestCase):
    """collect_odata() follows @odata.nextLink pages and stitches items in order."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_MSONEDRIVE")
        os.environ["RC_CONN_MSONEDRIVE"] = "ms_test_" + "token_abc"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSONEDRIVE", None)
        else:
            os.environ["RC_CONN_MSONEDRIVE"] = self._saved

    @responses_lib.activate
    def test_two_pages_stitched(self):
        """Page 1 has nextLink → page 2; page 2 has no nextLink → stop. Both pages stitched."""
        responses_lib.add(responses_lib.GET, CHILDREN_URL, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, _PAGE_1_NEXT, json=_PAGE_2_BODY, status=200)

        result = msonedrive.collect_odata("me/drive/root/children", query={"$top": 200})

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 3)
        names = [it["name"] for it in result["items"]]
        self.assertEqual(names, ["myfile.jpg", "Documents", "quarterly report.xlsx"])

    @responses_lib.activate
    def test_bearer_on_both_pages(self):
        """Bearer credential must appear on page 1 AND on the follow nextLink (page 2)."""
        responses_lib.add(responses_lib.GET, CHILDREN_URL, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, _PAGE_1_NEXT, json=_PAGE_2_BODY, status=200)

        msonedrive.collect_odata("me/drive/root/children", query={"$top": 200})

        self.assertEqual(len(responses_lib.calls), 2)
        # Auth header must ride every request — both page fetch and link follow.
        for call in responses_lib.calls:
            self.assertIn("Authorization", call.request.headers)
            self.assertTrue(
                call.request.headers["Authorization"].startswith("Bearer "),
                f"Expected Bearer auth, got: {call.request.headers.get('Authorization')}",
            )

    @responses_lib.activate
    def test_single_page_no_nextlink(self):
        """Single-page response (no @odata.nextLink) returns all items, incomplete=False."""
        single = {"value": [_FILE_1]}
        responses_lib.add(responses_lib.GET, CHILDREN_URL, json=single, status=200)

        result = msonedrive.collect_odata("me/drive/root/children")

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["name"], "myfile.jpg")

    @responses_lib.activate
    def test_max_items_caps_and_marks_incomplete(self):
        """max_items cap stops pagination early and sets incomplete=True."""
        responses_lib.add(responses_lib.GET, CHILDREN_URL, json=_PAGE_1_BODY, status=200)
        # Page 2 should NOT be fetched when max_items=1.
        result = msonedrive.collect_odata("me/drive/root/children", max_items=1)

        self.assertTrue(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses_lib.calls), 1)  # only page 1 requested


class MsOneDriveListChildren(unittest.TestCase):
    """list_children() paginates and picks support-relevant fields."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_MSONEDRIVE")
        os.environ["RC_CONN_MSONEDRIVE"] = "ms_test_" + "token_abc"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSONEDRIVE", None)
        else:
            os.environ["RC_CONN_MSONEDRIVE"] = self._saved

    @responses_lib.activate
    def test_children_picked_fields(self):
        responses_lib.add(responses_lib.GET, CHILDREN_URL, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, _PAGE_1_NEXT, json=_PAGE_2_BODY, status=200)

        result = msonedrive.list_children("me/drive/root")

        items = result["items"]
        self.assertEqual(len(items), 3)
        # pick() selects support fields; "name" must be present on all items.
        for it in items:
            self.assertIn("name", it)
        # File item: "file" facet path present, "folder" absent.
        self.assertIn("file", items[0])
        self.assertNotIn("folder", items[0])
        # Folder item: "folder" facet present.
        self.assertIn("folder", items[1])


class MsOneDriveSearch(unittest.TestCase):
    """search_files() uses the Graph search(q='…') path and picks fields."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_MSONEDRIVE")
        os.environ["RC_CONN_MSONEDRIVE"] = "ms_test_" + "token_abc"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSONEDRIVE", None)
        else:
            os.environ["RC_CONN_MSONEDRIVE"] = self._saved

    @responses_lib.activate
    def test_search_returns_picked_items(self):
        responses_lib.add(responses_lib.GET, SEARCH_URL, json=_SEARCH_BODY, status=200)

        result = msonedrive.search_files("quarterly report")

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        it = result["items"][0]
        self.assertIn("name", it)
        self.assertEqual(it["name"], "quarterly report.xlsx")
        # Picked items must NOT contain searchResult (not in _ITEM_FIELDS).
        self.assertNotIn("searchResult", it)


class MsOneDriveRecent(unittest.TestCase):
    """list_recent() returns picked items from me/drive/recent."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_MSONEDRIVE")
        os.environ["RC_CONN_MSONEDRIVE"] = "ms_test_" + "token_abc"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSONEDRIVE", None)
        else:
            os.environ["RC_CONN_MSONEDRIVE"] = self._saved

    @responses_lib.activate
    def test_recent_returns_files(self):
        responses_lib.add(responses_lib.GET, RECENT_URL, json=_RECENT_BODY, status=200)

        result = msonedrive.list_recent()

        self.assertEqual(len(result["items"]), 2)
        names = [it["name"] for it in result["items"]]
        self.assertIn("quarterly report.xlsx", names)


class MsOneDriveDrive(unittest.TestCase):
    """get_drive() returns raw drive metadata and renders to markdown."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_MSONEDRIVE")
        os.environ["RC_CONN_MSONEDRIVE"] = "ms_test_" + "token_abc"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSONEDRIVE", None)
        else:
            os.environ["RC_CONN_MSONEDRIVE"] = self._saved

    @responses_lib.activate
    def test_drive_metadata_and_markdown(self):
        responses_lib.add(responses_lib.GET, DRIVE_URL, json=_DRIVE_BODY, status=200)

        drive = msonedrive.get_drive()
        self.assertEqual(drive["name"], "OneDrive")
        self.assertEqual(drive["driveType"], "personal")

        md = msonedrive.drive_to_markdown(drive)
        self.assertIn("OneDrive", md)
        self.assertIn("Daron Spektor", md)


class MsOneDriveCLI(unittest.TestCase):
    """CLI commands run via main() and exit cleanly."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_MSONEDRIVE")
        os.environ["RC_CONN_MSONEDRIVE"] = "ms_test_" + "token_abc"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_MSONEDRIVE", None)
        else:
            os.environ["RC_CONN_MSONEDRIVE"] = self._saved

    @responses_lib.activate
    def test_cli_drive(self):
        responses_lib.add(responses_lib.GET, DRIVE_URL, json=_DRIVE_BODY, status=200)
        rc = msonedrive.main(["drive"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_children(self):
        responses_lib.add(responses_lib.GET, CHILDREN_URL, json=_PAGE_1_BODY, status=200)
        responses_lib.add(responses_lib.GET, _PAGE_1_NEXT, json=_PAGE_2_BODY, status=200)
        rc = msonedrive.main(["children", "me/drive/root"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_search(self):
        responses_lib.add(responses_lib.GET, SEARCH_URL, json=_SEARCH_BODY, status=200)
        rc = msonedrive.main(["search", "quarterly report"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_recent(self):
        responses_lib.add(responses_lib.GET, RECENT_URL, json=_RECENT_BODY, status=200)
        rc = msonedrive.main(["recent"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_generic_get_via_lib_api(self):
        """python -m lib.api get msonedrive ... also works (manifest is registered)."""
        responses_lib.add(responses_lib.GET, DRIVE_URL, json=_DRIVE_BODY, status=200)
        rc = api._main(["get", "msonedrive", "me/drive"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)
        self.assertIn("Authorization", responses_lib.calls[0].request.headers)


class MsOneDriveCassetteHygiene(unittest.TestCase):
    """CI guard: no real MS Graph access token prefix may appear in connector files.

    Scoped to the connector dir only — this test file names the guard strings itself and must
    not be scanned (split the literal so the guard doesn't false-positive on itself).
    """

    # MS Graph / MSAL access tokens are JWTs starting with "eyJ" (base64 "{"}).
    # Also guard against any literal "Bearer " prefix accidentally embedded.
    _TOKEN_PREFIXES = ("eyJ" "0", "Bearer" " ey", "RC_CONN" "_MSONEDRIVE=ey")

    def test_no_token_prefixes_in_msonedrive_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "msonedrive"
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
