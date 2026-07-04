"""Fixture tests for manifest-only meeting-note connectors.

No live creds, no network: HTTP is mocked with responses. These tests lock down the catalog rows and
the auth headers agents rely on for support-grounding reads.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import unittest
from pathlib import Path

import responses
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import api, mcp  # noqa: E402

RUNTIME = Path(__file__).resolve().parents[1]


class MeetingNoteBase(unittest.TestCase):
    keys = {
        "RC_CONN_FATHOM": "oauth_fathom_fixture",
        "RC_CONN_FATHOM_APIKEY": "apikey_fathom_fixture",
        "RC_CONN_OTTER": "otter_fixture",
        "RC_CONN_LEEXI": "leexi_id:leexi_secret",
        "RC_CONN_READAI": "readai_fixture",
        "RC_CONN_READAI_MCP": "readai_mcp_fixture",
        "RC_CONN_KRISP_MCP": "krisp_mcp_fixture",
        "RC_CONN_GONG": "gong_access:gong_secret",
    }

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.keys}
        os.environ.update(self.keys)
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        api.load_manifests()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def raw_manifest(self, key: str) -> dict:
        path = RUNTIME / "lib" / "connectors" / key / "manifest.yaml"
        return yaml.safe_load(path.read_text(encoding="utf-8"))


class ManifestRows(MeetingNoteBase):
    def test_expected_manifest_rows_load(self):
        for key in ("fathom", "fathom_apikey", "otter", "leexi", "readai", "krisp", "gong"):
            self.assertIn(key, api.MANIFESTS)
            self.assertEqual(self.raw_manifest(key)["connector_module"], "")

    def test_fathom_oauth_and_api_key_fallback(self):
        oauth = api.MANIFESTS["fathom"]
        self.assertEqual(oauth.base_url, "https://api.fathom.ai/external/v1")
        self.assertEqual(oauth.auth.strategy, "bearer")
        self.assertEqual(oauth.pagination.style, "cursor")
        self.assertEqual(oauth.pagination.cursor_field, "next_cursor")
        self.assertEqual(oauth.pagination.items_field, "items")
        self.assertIn("public_api", self.raw_manifest("fathom")["oauth"]["default_scopes"])

        apikey = api.MANIFESTS["fathom_apikey"]
        self.assertEqual(apikey.auth.strategy, "api_key_header")
        self.assertEqual(apikey.auth.name, "X-Api-Key")

    def test_otter_leexi_readai_krisp_gong_shapes(self):
        otter = api.MANIFESTS["otter"]
        self.assertEqual(otter.auth.strategy, "bearer")
        self.assertEqual(otter.pagination.cursor_field, "meta.next_cursor")
        self.assertEqual(otter.pagination.has_more_field, "meta.has_more")
        self.assertEqual(otter.pagination.items_field, "data")

        leexi = api.MANIFESTS["leexi"]
        self.assertEqual(leexi.auth.strategy, "basic")
        self.assertEqual(leexi.pagination.style, "page")
        self.assertEqual(leexi.pagination.limit_param, "items")
        self.assertEqual(leexi.pagination.items_field, "data")

        readai_raw = self.raw_manifest("readai")
        self.assertEqual(api.MANIFESTS["readai"].auth.strategy, "bearer")
        self.assertIn("meeting:read", readai_raw["oauth"]["default_scopes"])
        self.assertIn("mcp:execute", readai_raw["oauth"]["default_scopes"])
        self.assertEqual(readai_raw["mcp_url_template"], "https://api.read.ai/mcp")

        krisp_raw = self.raw_manifest("krisp")
        self.assertEqual(krisp_raw["kinds"], ["mcp"])
        self.assertEqual(krisp_raw["mcp_url_template"], "https://mcp.krisp.ai/mcp")

        gong = api.MANIFESTS["gong"]
        self.assertEqual(gong.auth.strategy, "basic")
        self.assertIn("/calls/transcript", gong.allowed_post_paths)
        self.assertIn("/calls/extensive", gong.allowed_post_paths)


class AuthAndPagingFixtures(MeetingNoteBase):
    @responses.activate
    def test_fathom_oauth_uses_bearer_and_cursor_paginates(self):
        responses.add(
            responses.GET,
            "https://api.fathom.ai/external/v1/meetings",
            json={"items": [{"recording_id": 1}], "next_cursor": "next"},
        )
        responses.add(
            responses.GET,
            "https://api.fathom.ai/external/v1/meetings?limit=100&cursor=next",
            json={"items": [{"recording_id": 2}], "next_cursor": None},
        )

        result = api.client(api.MANIFESTS["fathom"]).collect("meetings", query={"limit": 100})

        self.assertEqual([item["recording_id"] for item in result["items"]], [1, 2])
        self.assertEqual(responses.calls[0].request.headers["Authorization"], "Bearer oauth_fathom_fixture")
        self.assertIn("cursor=next", responses.calls[1].request.url)

    @responses.activate
    def test_fathom_api_key_uses_x_api_key(self):
        responses.add(
            responses.GET,
            "https://api.fathom.ai/external/v1/recordings/123/transcript",
            json={"transcript": []},
        )

        api.client(api.MANIFESTS["fathom_apikey"]).get("recordings/123/transcript")

        req = responses.calls[0].request
        self.assertEqual(req.headers["X-Api-Key"], "apikey_fathom_fixture")
        self.assertNotIn("Authorization", req.headers)

    @responses.activate
    def test_otter_uses_bearer_and_meta_cursor(self):
        responses.add(
            responses.GET,
            "https://api.otter.ai/v1/conversations",
            json={"meta": {"has_more": True, "next_cursor": "c2"}, "data": [{"id": "c1"}]},
        )
        responses.add(
            responses.GET,
            "https://api.otter.ai/v1/conversations?limit=100&cursor=c2",
            json={"meta": {"has_more": False}, "data": [{"id": "c2"}]},
        )

        result = api.client(api.MANIFESTS["otter"]).collect("conversations", query={"limit": 100})

        self.assertEqual([item["id"] for item in result["items"]], ["c1", "c2"])
        self.assertEqual(responses.calls[0].request.headers["Authorization"], "Bearer otter_fixture")

    @responses.activate
    def test_leexi_uses_basic_auth_and_page_items_params(self):
        responses.add(
            responses.GET,
            "https://public-api.leexi.ai/v1/calls?items=100&page=1",
            json={"data": [{"uuid": "a"}], "pagination": {"count": 1, "items": 100, "page": 1}},
        )

        result = api.client(api.MANIFESTS["leexi"]).collect("calls")

        self.assertEqual(result["items"], [{"uuid": "a"}])
        expected = "Basic " + base64.b64encode(b"leexi_id:leexi_secret").decode()
        self.assertEqual(responses.calls[0].request.headers["Authorization"], expected)

    @responses.activate
    def test_readai_rest_uses_bearer(self):
        responses.add(
            responses.GET,
            "https://api.read.ai/v1/meetings",
            json={"object": "list", "has_more": False, "data": [{"id": "m1"}]},
        )

        body = api.client(api.MANIFESTS["readai"]).get("meetings")

        self.assertEqual(body["data"][0]["id"], "m1")
        self.assertEqual(responses.calls[0].request.headers["Authorization"], "Bearer readai_fixture")

    @responses.activate
    def test_gong_basic_and_read_only_post_allowlist(self):
        responses.add(
            responses.POST,
            "https://api.gong.io/v2/calls/transcript",
            json={"callTranscripts": [{"callId": "c1", "transcript": []}]},
        )

        body = api.client(api.MANIFESTS["gong"]).post("calls/transcript", json={"filter": {"callIds": ["c1"]}})

        self.assertEqual(body["callTranscripts"][0]["callId"], "c1")
        expected = "Basic " + base64.b64encode(b"gong_access:gong_secret").decode()
        req = responses.calls[0].request
        self.assertEqual(req.headers["Authorization"], expected)
        self.assertEqual(json.loads(req.body), {"filter": {"callIds": ["c1"]}})


class McpManifestRows(MeetingNoteBase):
    def test_static_mcp_urls_resolve_from_manifest(self):
        os.environ.pop("RC_CONN_KRISP_MCP_URL", None)
        os.environ.pop("RC_CONN_READAI_MCP_URL", None)

        self.assertEqual(mcp.resolve_endpoint("krisp"), "https://mcp.krisp.ai/mcp")
        self.assertEqual(mcp.resolve_endpoint("readai"), "https://api.read.ai/mcp")

    @responses.activate
    def test_krisp_mcp_client_uses_static_url_and_bearer_env(self):
        responses.add(
            responses.POST,
            "https://mcp.krisp.ai/mcp",
            json={"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "search_meetings"}]}},
        )

        tools = mcp.tools("krisp")

        self.assertEqual(tools, [{"name": "search_meetings"}])
        self.assertEqual(responses.calls[0].request.headers["Authorization"], "Bearer krisp_mcp_fixture")


class Hygiene(MeetingNoteBase):
    def test_no_connector_python_added_for_manifest_only_rows(self):
        for key in ("fathom", "fathom_apikey", "otter", "leexi", "readai", "krisp", "gong"):
            connector_dir = RUNTIME / "lib" / "connectors" / key
            py_files = sorted(p.name for p in connector_dir.glob("*.py"))
            self.assertEqual(py_files, [], f"{key} should remain manifest-only")


if __name__ == "__main__":
    unittest.main()
