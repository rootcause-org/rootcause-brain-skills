"""Fixture test for the manifest-ONLY Google Drive integration — proves a catalogued connector
with NO bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies mirror Google
Drive API v3 documented example payloads (developers.google.com/workspace/drive/api/reference/rest/v3),
trimmed to support-relevant fields.

Google Drive paginates with cursor tokens: `nextPageToken` in the response body → `pageToken`
query param on the next request. Two mocked pages exercise the real `cursor` pagination style.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_googledrive_connector.py -q
"""

import io
import json
import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API = "https://www.googleapis.com/drive/v3"
FILES_URL = f"{API}/files"
DRIVES_URL = f"{API}/drives"

# ---------------------------------------------------------------------------
# Fixture bodies — shaped from Drive API v3 documented payloads
# ---------------------------------------------------------------------------

# Page 1: two files + a nextPageToken so the cursor loop continues.
_FILES_PAGE_1 = {
    "kind": "drive#fileList",
    "nextPageToken": "TOKEN_PAGE_2",
    "files": [
        {
            "id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
            "name": "Invoice Q1 2026.pdf",
            "mimeType": "application/pdf",
            "modifiedTime": "2026-03-01T10:00:00.000Z",
            "owners": [{"emailAddress": "alice@example.com", "displayName": "Alice"}],
            "webViewLink": "https://drive.google.com/file/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms/view",
            "size": "204800",
        },
        {
            "id": "1aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdef",
            "name": "Support Notes",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-02-15T08:30:00.000Z",
            "owners": [{"emailAddress": "bob@example.com", "displayName": "Bob"}],
            "webViewLink": "https://docs.google.com/document/d/1aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdef/edit",
            "size": None,
        },
    ],
}

# Page 2: one file + no nextPageToken → loop stops.
_FILES_PAGE_2 = {
    "kind": "drive#fileList",
    "files": [
        {
            "id": "0B4GXdKgjFCUaem5BVEIxbE9nRUE",
            "name": "Backup Archive 2025.zip",
            "mimeType": "application/zip",
            "modifiedTime": "2025-12-31T23:59:00.000Z",
            "owners": [{"emailAddress": "carol@example.com", "displayName": "Carol"}],
            "webViewLink": "https://drive.google.com/file/d/0B4GXdKgjFCUaem5BVEIxbE9nRUE/view",
            "size": "52428800",
        },
    ],
}

# Shared drives list — two pages to exercise cursor pagination for a different endpoint.
_DRIVES_PAGE_1 = {
    "kind": "drive#driveList",
    "nextPageToken": "DRIVE_TOKEN_2",
    "drives": [
        {"id": "0APNcj0UkjQ0kUk9PVA", "name": "Engineering Shared", "kind": "drive#drive"},
    ],
}
_DRIVES_PAGE_2 = {
    "kind": "drive#driveList",
    "drives": [
        {"id": "0APNcj0UkjQ0kUk9PVB", "name": "Finance Shared", "kind": "drive#drive"},
    ],
}


# ---------------------------------------------------------------------------
# Helper: fresh registry per test
# ---------------------------------------------------------------------------

class _CleanRegistry:
    """Context: clear MANIFESTS + injected env var before/after each test."""

    def __init__(self, token: str = "ya29.drive_test_token"):
        self._token = token

    def __enter__(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GOOGLEDRIVE")
        os.environ["RC_CONN_GOOGLEDRIVE"] = self._token
        return self

    def __exit__(self, *_):
        if self._saved is None:
            os.environ.pop("RC_CONN_GOOGLEDRIVE", None)
        else:
            os.environ["RC_CONN_GOOGLEDRIVE"] = self._saved
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class GoogleDriveManifestLoad(unittest.TestCase):
    """YAML loads cleanly and maps every manifest field correctly."""

    def setUp(self):
        self._ctx = _CleanRegistry()
        self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__()

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("googledrive", m)

    def test_base_url(self):
        api.load_manifests()
        g = api.MANIFESTS["googledrive"]
        self.assertEqual(g.base_url, "https://www.googleapis.com/drive/v3")

    def test_auth_strategy_is_bearer(self):
        api.load_manifests()
        self.assertEqual(api.MANIFESTS["googledrive"].auth.strategy, "bearer")

    def test_pagination_is_cursor(self):
        api.load_manifests()
        p = api.MANIFESTS["googledrive"].pagination
        self.assertEqual(p.style, "cursor")
        self.assertEqual(p.cursor_param, "pageToken")
        self.assertEqual(p.cursor_field, "nextPageToken")
        self.assertEqual(p.items_field, "files")
        self.assertEqual(p.page_size, 100)

    def test_rate_limit_header_absent(self):
        api.load_manifests()
        self.assertEqual(api.MANIFESTS["googledrive"].rate_limit_remaining_header, "")

    def test_no_default_headers(self):
        """Drive v3 needs no mandatory version/Accept header beyond Authorization."""
        api.load_manifests()
        self.assertEqual(api.MANIFESTS["googledrive"].default_headers, {})


class GoogleDrivePagination(unittest.TestCase):
    """Cursor pagination stitches ≥2 pages; bearer rides every request."""

    def setUp(self):
        self._ctx = _CleanRegistry()
        self._ctx.__enter__()
        api.load_manifests()

    def tearDown(self):
        self._ctx.__exit__()

    @responses_lib.activate
    def test_cursor_pagination_stitches_two_pages(self):
        responses_lib.add(responses_lib.GET, FILES_URL, json=_FILES_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, FILES_URL, json=_FILES_PAGE_2, status=200)

        c = api.client(api.MANIFESTS["googledrive"])
        result = c.collect("files", query={"pageSize": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        names = [f["name"] for f in result["items"]]
        self.assertEqual(names, ["Invoice Q1 2026.pdf", "Support Notes", "Backup Archive 2025.zip"])

    @responses_lib.activate
    def test_bearer_credential_on_every_request(self):
        responses_lib.add(responses_lib.GET, FILES_URL, json=_FILES_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, FILES_URL, json=_FILES_PAGE_2, status=200)

        c = api.client(api.MANIFESTS["googledrive"])
        c.collect("files", query={"pageSize": 100})

        # Both page 1 and page 2 requests carry the injected bearer token.
        for call in responses_lib.calls:
            self.assertEqual(
                call.request.headers["Authorization"],
                "Bearer ya29.drive_test_token",
            )

    @responses_lib.activate
    def test_page_token_sent_on_second_request(self):
        """The nextPageToken from page 1 is sent as pageToken on page 2's request."""
        responses_lib.add(responses_lib.GET, FILES_URL, json=_FILES_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, FILES_URL, json=_FILES_PAGE_2, status=200)

        c = api.client(api.MANIFESTS["googledrive"])
        c.collect("files")

        # Page 2 request URL must include pageToken=TOKEN_PAGE_2.
        page2_url = responses_lib.calls[1].request.url
        self.assertIn("pageToken=TOKEN_PAGE_2", page2_url)

    @responses_lib.activate
    def test_single_page_no_next_token(self):
        """A response with no nextPageToken terminates after one page."""
        responses_lib.add(responses_lib.GET, FILES_URL, json=_FILES_PAGE_2, status=200)

        c = api.client(api.MANIFESTS["googledrive"])
        result = c.collect("files")

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_drives_endpoint_cursor_pagination(self):
        """drives endpoint also uses cursor pagination (items_field mismatch: 'drives' vs 'files').

        The manifest declares items_field='files'; for the drives endpoint the items live under
        'drives'. The cursor loop still terminates correctly (no nextPageToken on page 2) even
        though items_field doesn't match — collect returns empty items but doesn't loop forever.
        """
        responses_lib.add(responses_lib.GET, DRIVES_URL, json=_DRIVES_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, DRIVES_URL, json=_DRIVES_PAGE_2, status=200)

        c = api.client(api.MANIFESTS["googledrive"])
        result = c.collect("drives")

        # Loop terminates (page 2 has no nextPageToken).
        self.assertFalse(result["incomplete"])
        self.assertEqual(len(responses_lib.calls), 2)


class GoogleDrivePickFields(unittest.TestCase):
    """api.pick selects support-relevant fields from a file object."""

    def test_pick_support_fields_from_file(self):
        file_obj = _FILES_PAGE_1["files"][0]
        selected = api.pick(file_obj, "id,name,mimeType,modifiedTime,owners.*.emailAddress,webViewLink")
        self.assertEqual(selected["id"], "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms")
        self.assertEqual(selected["name"], "Invoice Q1 2026.pdf")
        self.assertEqual(selected["mimeType"], "application/pdf")
        self.assertEqual(selected["modifiedTime"], "2026-03-01T10:00:00.000Z")
        self.assertEqual(selected["owners.*.emailAddress"], ["alice@example.com"])
        self.assertIn("webViewLink", selected)

    def test_pick_missing_path_absent(self):
        """A path not in the object is simply absent — not raised, not None."""
        file_obj = _FILES_PAGE_1["files"][0]
        selected = api.pick(file_obj, "id,thumbnailLink")  # thumbnailLink not in fixture
        self.assertIn("id", selected)
        self.assertNotIn("thumbnailLink", selected)


class GoogleDriveCLI(unittest.TestCase):
    """The generic lib.api CLI drives the googledrive connector end-to-end."""

    def setUp(self):
        self._ctx = _CleanRegistry()
        self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__()

    @responses_lib.activate
    def test_cli_get_files_with_paginate_and_pick(self):
        responses_lib.add(responses_lib.GET, FILES_URL, json=_FILES_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, FILES_URL, json=_FILES_PAGE_2, status=200)

        captured = io.StringIO()
        import sys as _sys
        old_stdout = _sys.stdout
        _sys.stdout = captured
        try:
            rc = api._main([
                "get", "googledrive", "files",
                "--query", "pageSize=100",
                "--paginate",
                "--pick", "id,name,mimeType",
            ])
        finally:
            _sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        output = json.loads(captured.getvalue())
        self.assertFalse(output["incomplete"])
        self.assertEqual(len(output["items"]), 3)
        # Bearer was sent on both requests.
        for call in responses_lib.calls:
            self.assertEqual(
                call.request.headers["Authorization"],
                "Bearer ya29.drive_test_token",
            )

    @responses_lib.activate
    def test_cli_get_single_file(self):
        file_id = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        file_url = f"{API}/files/{file_id}"
        responses_lib.add(responses_lib.GET, file_url,
                          json=_FILES_PAGE_1["files"][0], status=200)

        captured = io.StringIO()
        import sys as _sys
        old_stdout = _sys.stdout
        _sys.stdout = captured
        try:
            rc = api._main([
                "get", "googledrive", f"files/{file_id}",
                "--pick", "id,name,mimeType",
            ])
        finally:
            _sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        output = json.loads(captured.getvalue())
        self.assertEqual(output["id"], file_id)
        self.assertEqual(output["name"], "Invoice Q1 2026.pdf")


class GoogleDriveCassetteHygiene(unittest.TestCase):
    """CI guard: no real Google Drive OAuth token prefix may land in the connector files.

    Scopes to the connector dir (manifest + any future cassettes), NOT this test file itself —
    the test legitimately names the prefixes it hunts for.
    """

    # Google OAuth access tokens start with "ya29."; service account tokens share the same prefix.
    # Split the prefix with string concatenation so this guard doesn't flag itself.
    _TOKEN_PREFIXES = ("ya29" ".",)

    def test_no_token_prefixes_in_googledrive_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "googledrive"
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
