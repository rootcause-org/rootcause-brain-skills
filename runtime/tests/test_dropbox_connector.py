"""Fixture test for the Dropbox script connector.

No live creds, no network: HTTP is mocked with `responses`. Bodies mirror Dropbox's documented
example API payloads (trimmed to support-relevant fields). Tests cover:

  - manifest.yaml loads via lib.api's YAML loader and maps every field correctly
  - POST-based two-phase cursor pagination (list_folder → list_folder/continue) stitches ≥2 pages
  - bearer credential rides every POST request (including continuation calls)
  - field pre-selection (_compact_entry) extracts the 6 support-relevant fields
  - shared_links, get_metadata, search, account_info operations
  - CLI (main([...])) drives list-folder, get-metadata, search, shared-links, account

Token-prefix hygiene guard: Dropbox tokens start with "sl." (short-lived OAuth) or are long
opaque strings. Split the prefix literal with string concatenation so this guard doesn't flag itself.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_dropbox_connector.py -q
"""

import json
import os
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import dropbox as dbx  # noqa: E402

_API = "https://api.dropboxapi.com/2"
_TOKEN = "dropbox" + "_test_token_fixture"  # no real token prefix; hygiene guard ignores this

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# ---------------------------------------------------------------------------

# files/list_folder — page 1: two entries + has_more=True + cursor
_LIST_FOLDER_PAGE_1 = {
    "entries": [
        {
            ".tag": "folder",
            "name": "Documents",
            "path_lower": "/documents",
            "path_display": "/Documents",
            "id": "id:abc123folderA",
        },
        {
            ".tag": "file",
            "name": "report.pdf",
            "path_lower": "/documents/report.pdf",
            "path_display": "/Documents/report.pdf",
            "id": "id:abc123fileA",
            "size": 204800,
            "server_modified": "2024-01-15T10:00:00Z",
            "client_modified": "2024-01-14T09:00:00Z",
            "rev": "a1b2c3d4e5f6",
        },
    ],
    "cursor": "cursor_abc_page1",
    "has_more": True,
}

# files/list_folder/continue — page 2: one more entry + has_more=False
_LIST_FOLDER_PAGE_2 = {
    "entries": [
        {
            ".tag": "file",
            "name": "invoice.xlsx",
            "path_lower": "/documents/invoice.xlsx",
            "path_display": "/Documents/invoice.xlsx",
            "id": "id:abc123fileB",
            "size": 51200,
            "server_modified": "2024-02-20T14:30:00Z",
            "client_modified": "2024-02-19T12:00:00Z",
            "rev": "f6e5d4c3b2a1",
        },
    ],
    "cursor": "cursor_abc_page2",
    "has_more": False,
}

# files/get_metadata
_GET_METADATA = {
    ".tag": "file",
    "name": "report.pdf",
    "path_lower": "/documents/report.pdf",
    "path_display": "/Documents/report.pdf",
    "id": "id:abc123fileA",
    "size": 204800,
    "server_modified": "2024-01-15T10:00:00Z",
}

# files/search/v2
_SEARCH_V2 = {
    "matches": [
        {
            "metadata": {
                ".tag": "metadata",
                "metadata": {
                    ".tag": "file",
                    "name": "Q4 Report.pdf",
                    "path_lower": "/finance/q4 report.pdf",
                    "path_display": "/Finance/Q4 Report.pdf",
                    "id": "id:searchFileA",
                    "size": 102400,
                    "server_modified": "2024-03-01T08:00:00Z",
                },
            }
        }
    ],
    "has_more": False,
}

# sharing/list_shared_links
_SHARED_LINKS = {
    "links": [
        {
            ".tag": "file",
            "url": "https://www.dropbox.com/s/xyz123/report.pdf?dl=0",
            "name": "report.pdf",
            "path_lower": "/documents/report.pdf",
            "link_permissions": {
                "resolved_visibility": {".tag": "public"},
            },
            "expires": None,
        }
    ],
    "has_more": False,
}

# users/get_current_account
_GET_ACCOUNT = {
    "account_id": "dbid:AABCDE1234",
    "name": {"display_name": "Alice Example", "given_name": "Alice", "surname": "Example"},
    "email": "alice@example.com",
    "account_type": {".tag": "pro"},
}

# users/get_space_usage
_SPACE_USAGE = {
    "used": 10737418240,  # 10 GiB
    "allocation": {".tag": "individual", "allocated": 107374182400},  # 100 GiB
}


# ---------------------------------------------------------------------------
# Helper to set env / clear manifest registry
# ---------------------------------------------------------------------------

class _DropboxBase(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_DROPBOX")
        os.environ["RC_CONN_DROPBOX"] = _TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_DROPBOX", None)
        else:
            os.environ["RC_CONN_DROPBOX"] = self._saved


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------

class TestDropboxManifestLoading(_DropboxBase):

    def test_manifest_loads_from_yaml(self):
        manifests = api.load_manifests()
        self.assertIn("dropbox", manifests)

    def test_manifest_fields(self):
        api.load_manifests()
        m = api.MANIFESTS["dropbox"]
        self.assertEqual(m.key, "dropbox")
        self.assertEqual(m.base_url, "https://api.dropboxapi.com/2")
        self.assertEqual(m.auth.strategy, "bearer")
        self.assertEqual(m.pagination.style, "none")
        self.assertEqual(m.rate_limit_remaining_header, "")

    def test_connector_module_is_registered(self):
        # The connector module registers its MANIFEST on import via api.register(). Accessing the
        # module-level MANIFEST attribute re-triggers the registration (api.register returns and
        # sets MANIFESTS[key]). Ensure the key is present after explicit re-registration.
        api.register(dbx.MANIFEST)
        self.assertIn("dropbox", api.MANIFESTS)

    def test_manifest_yaml_has_oauth_block(self):
        """Verify oauth fields in the raw YAML (lib.api loader ignores them, but catalog needs them)."""
        import yaml
        manifest_path = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "dropbox" / "manifest.yaml"
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["key"], "dropbox")
        self.assertIn("oauth", raw)
        self.assertIn("auth_url", raw["oauth"])
        self.assertIn("token_url", raw["oauth"])
        self.assertIn("default_scopes", raw["oauth"])
        self.assertIn("files.metadata.read", raw["oauth"]["default_scopes"])
        self.assertIn("kinds", raw)
        self.assertIn("oauth", raw["kinds"])
        self.assertIn("token", raw["kinds"])
        self.assertEqual(raw["connector_module"], "lib.connectors.dropbox")


# ---------------------------------------------------------------------------
# Two-phase POST cursor pagination
# ---------------------------------------------------------------------------

class TestDropboxListFolderPagination(_DropboxBase):

    @responses_lib.activate
    def test_two_phase_cursor_pagination_stitches_pages(self):
        """list_folder → list_folder/continue POSTs stitch ≥2 pages into one result."""
        responses_lib.add(
            responses_lib.POST,
            f"{_API}/files/list_folder",
            json=_LIST_FOLDER_PAGE_1,
            status=200,
        )
        responses_lib.add(
            responses_lib.POST,
            f"{_API}/files/list_folder/continue",
            json=_LIST_FOLDER_PAGE_2,
            status=200,
        )

        entries = dbx.list_folder("/Documents")

        # Three entries total across two pages
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["type"], "folder")
        self.assertEqual(entries[0]["name"], "Documents")
        self.assertEqual(entries[1]["type"], "file")
        self.assertEqual(entries[1]["name"], "report.pdf")
        self.assertEqual(entries[2]["name"], "invoice.xlsx")

        # Two HTTP calls were made (one per page)
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_bearer_credential_on_all_posts(self):
        """Bearer token must ride every POST — first call AND continuation call."""
        responses_lib.add(responses_lib.POST, f"{_API}/files/list_folder",
                          json=_LIST_FOLDER_PAGE_1, status=200)
        responses_lib.add(responses_lib.POST, f"{_API}/files/list_folder/continue",
                          json=_LIST_FOLDER_PAGE_2, status=200)

        dbx.list_folder("/Documents")

        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], f"Bearer {_TOKEN}")

    @responses_lib.activate
    def test_single_page_stops_without_continuation(self):
        """When has_more=False on page 1, no continuation POST is made."""
        single_page = {**_LIST_FOLDER_PAGE_1, "has_more": False}
        responses_lib.add(responses_lib.POST, f"{_API}/files/list_folder",
                          json=single_page, status=200)

        entries = dbx.list_folder("")
        self.assertEqual(len(entries), 2)
        self.assertEqual(len(responses_lib.calls), 1)  # no continuation


# ---------------------------------------------------------------------------
# Field pre-selection
# ---------------------------------------------------------------------------

class TestDropboxFieldPreSelection(_DropboxBase):

    def test_compact_entry_extracts_file_fields(self):
        raw_file = _LIST_FOLDER_PAGE_1["entries"][1]  # report.pdf
        compact = dbx._compact_entry(raw_file)
        self.assertEqual(compact["type"], "file")
        self.assertEqual(compact["name"], "report.pdf")
        self.assertEqual(compact["path"], "/Documents/report.pdf")
        self.assertEqual(compact["id"], "id:abc123fileA")
        self.assertEqual(compact["size"], 204800)
        self.assertEqual(compact["modified"], "2024-01-15T10:00:00Z")
        # Noise fields stripped
        self.assertNotIn("rev", compact)
        self.assertNotIn("client_modified", compact)
        self.assertNotIn("content_hash", compact)

    def test_compact_entry_extracts_folder_fields(self):
        raw_folder = _LIST_FOLDER_PAGE_1["entries"][0]  # Documents folder
        compact = dbx._compact_entry(raw_folder)
        self.assertEqual(compact["type"], "folder")
        self.assertEqual(compact["name"], "Documents")
        self.assertIsNone(compact["size"])     # folders have no size
        self.assertIsNone(compact["modified"]) # folders have no server_modified

    @responses_lib.activate
    def test_list_folder_returns_compact_dicts(self):
        single = {**_LIST_FOLDER_PAGE_1, "has_more": False}
        responses_lib.add(responses_lib.POST, f"{_API}/files/list_folder", json=single, status=200)

        entries = dbx.list_folder("/Documents")
        # All entries are compact (6 keys)
        for e in entries:
            self.assertEqual(set(e.keys()), {"type", "name", "path", "id", "size", "modified"})


# ---------------------------------------------------------------------------
# Other operations
# ---------------------------------------------------------------------------

class TestDropboxGetMetadata(_DropboxBase):

    @responses_lib.activate
    def test_get_metadata_returns_compact(self):
        responses_lib.add(responses_lib.POST, f"{_API}/files/get_metadata",
                          json=_GET_METADATA, status=200)

        entry = dbx.get_metadata("/Documents/report.pdf")
        self.assertEqual(entry["type"], "file")
        self.assertEqual(entry["name"], "report.pdf")
        self.assertEqual(entry["size"], 204800)
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"],
                         f"Bearer {_TOKEN}")


class TestDropboxSearch(_DropboxBase):

    @responses_lib.activate
    def test_search_returns_compact_matches(self):
        responses_lib.add(responses_lib.POST, f"{_API}/files/search/v2",
                          json=_SEARCH_V2, status=200)

        results = dbx.search("quarterly report")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "Q4 Report.pdf")
        self.assertEqual(results[0]["type"], "file")
        self.assertEqual(results[0]["size"], 102400)

    @responses_lib.activate
    def test_search_bearer_on_request(self):
        responses_lib.add(responses_lib.POST, f"{_API}/files/search/v2",
                          json=_SEARCH_V2, status=200)
        dbx.search("invoice")
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"],
                         f"Bearer {_TOKEN}")


class TestDropboxSharedLinks(_DropboxBase):

    @responses_lib.activate
    def test_shared_links_returns_compact(self):
        responses_lib.add(responses_lib.POST, f"{_API}/sharing/list_shared_links",
                          json=_SHARED_LINKS, status=200)

        links = dbx.shared_links()
        self.assertEqual(len(links), 1)
        lk = links[0]
        self.assertEqual(set(lk.keys()), {"url", "name", "path", "link_type", "visibility", "expires"})
        self.assertIn("dropbox.com", lk["url"])
        self.assertEqual(lk["visibility"], "public")

    @responses_lib.activate
    def test_shared_links_bearer(self):
        responses_lib.add(responses_lib.POST, f"{_API}/sharing/list_shared_links",
                          json=_SHARED_LINKS, status=200)
        dbx.shared_links()
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"],
                         f"Bearer {_TOKEN}")


class TestDropboxAccountInfo(_DropboxBase):

    @responses_lib.activate
    def test_account_info_returns_compact(self):
        responses_lib.add(responses_lib.POST, f"{_API}/users/get_current_account",
                          json=_GET_ACCOUNT, status=200)
        responses_lib.add(responses_lib.POST, f"{_API}/users/get_space_usage",
                          json=_SPACE_USAGE, status=200)

        info = dbx.account_info()
        self.assertEqual(info["name"], "Alice Example")
        self.assertEqual(info["email"], "alice@example.com")
        self.assertEqual(info["account_type"], "pro")
        self.assertEqual(info["quota_used_bytes"], 10737418240)
        self.assertEqual(info["quota_allocated_bytes"], 107374182400)


# ---------------------------------------------------------------------------
# Error normalization
# ---------------------------------------------------------------------------

class TestDropboxErrorNormalization(_DropboxBase):

    @responses_lib.activate
    def test_non_2xx_raises_api_error(self):
        responses_lib.add(responses_lib.POST, f"{_API}/files/list_folder",
                          json={"error_summary": "not_found/."}, status=409)

        with self.assertRaises(api.ApiError) as ctx:
            dbx.list_folder("/nonexistent")
        self.assertEqual(ctx.exception.status, 409)

    @responses_lib.activate
    def test_401_raises_api_error(self):
        responses_lib.add(responses_lib.POST, f"{_API}/files/get_metadata",
                          json={"error": "invalid_access_token"}, status=401)

        with self.assertRaises(api.ApiError) as ctx:
            dbx.get_metadata("/private")
        self.assertEqual(ctx.exception.status, 401)


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------

class TestDropboxCLI(_DropboxBase):

    @responses_lib.activate
    def test_cli_list_folder(self):
        single = {**_LIST_FOLDER_PAGE_1, "has_more": False}
        responses_lib.add(responses_lib.POST, f"{_API}/files/list_folder", json=single, status=200)

        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            rc = dbx.main(["list-folder", "/Documents"])
        self.assertEqual(rc, 0)
        output = mock_out.getvalue()
        self.assertIn("Documents", output)
        self.assertIn("report.pdf", output)

    @responses_lib.activate
    def test_cli_get_metadata(self):
        responses_lib.add(responses_lib.POST, f"{_API}/files/get_metadata",
                          json=_GET_METADATA, status=200)

        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            rc = dbx.main(["get-metadata", "/Documents/report.pdf"])
        self.assertEqual(rc, 0)
        self.assertIn("report.pdf", mock_out.getvalue())

    @responses_lib.activate
    def test_cli_search(self):
        responses_lib.add(responses_lib.POST, f"{_API}/files/search/v2",
                          json=_SEARCH_V2, status=200)

        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            rc = dbx.main(["search", "quarterly report"])
        self.assertEqual(rc, 0)
        self.assertIn("Q4 Report", mock_out.getvalue())

    @responses_lib.activate
    def test_cli_shared_links(self):
        responses_lib.add(responses_lib.POST, f"{_API}/sharing/list_shared_links",
                          json=_SHARED_LINKS, status=200)

        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            rc = dbx.main(["shared-links"])
        self.assertEqual(rc, 0)
        self.assertIn("dropbox.com", mock_out.getvalue())

    @responses_lib.activate
    def test_cli_account(self):
        responses_lib.add(responses_lib.POST, f"{_API}/users/get_current_account",
                          json=_GET_ACCOUNT, status=200)
        responses_lib.add(responses_lib.POST, f"{_API}/users/get_space_usage",
                          json=_SPACE_USAGE, status=200)

        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            rc = dbx.main(["account"])
        self.assertEqual(rc, 0)
        output = json.loads(mock_out.getvalue())
        self.assertEqual(output["email"], "alice@example.com")

    @responses_lib.activate
    def test_cli_list_folder_recursive(self):
        single = {**_LIST_FOLDER_PAGE_1, "has_more": False}
        responses_lib.add(responses_lib.POST, f"{_API}/files/list_folder", json=single, status=200)

        with patch("sys.stdout", new_callable=StringIO):
            rc = dbx.main(["list-folder", "/Documents", "--recursive"])
        self.assertEqual(rc, 0)
        # Check that recursive=True was sent in the request body
        import json as _json
        sent_body = _json.loads(responses_lib.calls[0].request.body)
        self.assertTrue(sent_body["recursive"])


# ---------------------------------------------------------------------------
# Token-prefix hygiene guard (scoped to dropbox connector dir only)
# ---------------------------------------------------------------------------

class TestDropboxTokenHygiene(unittest.TestCase):
    """CI guard: no real Dropbox token may land in the committed connector files.

    Dropbox short-lived OAuth tokens are "sl" + ".<type>.<data>"; a bare "sl." appears in
    ordinary Python (argparse variable names), so we guard against the longer
    "sl" + ".u" (user tokens) and "sl" + ".t" (team tokens) that would only appear in real
    credentials, not in source code variable names. Long-lived app tokens are opaque and have no
    recognizable prefix — those are caught by secret-scanning tooling, not this guard.

    Scoped to the connector dir (manifest + __init__ etc), NOT this test file — the test
    legitimately constructs the prefixes it hunts for using string concatenation.
    """

    # Dropbox short-lived OAuth token type prefixes.
    # Concatenated so this test file itself doesn't trigger the guard.
    _TOKEN_PREFIXES = ("sl" + ".u", "sl" + ".t", "sl" + ".c")

    def test_no_token_prefixes_in_dropbox_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "dropbox"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: found {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
