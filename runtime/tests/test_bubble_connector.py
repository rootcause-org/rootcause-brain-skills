"""Fixture test for the Bubble.io connector — no live creds, no network (HTTP mocked with ``responses``).

Two things are proven here:

1. **Token-cheap swagger discovery** — a miniature fixture shaped like a REAL Bubble swagger 2.0
   (``/obj/<type>`` collection + ``/{UniqueID}`` item paths, every operation tagged ``["Data"]``,
   ``definitions`` carrying a ``<type>Body`` companion and DISPLAY-NAME fields with spaces such as
   ``"Created Date"``) drives ``lib.connectors.bubble``. The inventory stays compact even when the
   swagger is bloated with a multi-KB description and a huge enum (the several-MB reality), and — the
   key task-4 property — groups PER TYPE even though the real tags collapse to one ``Data`` bucket.
2. **Manifest correctness + offset pagination** — the YAML row loads with bearer/broker/offset fields, and
   the ``response.results`` offset envelope stitches across pages through the generic ``lib.api`` paginator.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_bubble_connector.py -q
"""

import json
import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import bubble  # noqa: E402

# An obviously-fake admin token — never a real Bubble key.
_FAKE_TOKEN = "bubble_fake_admin_token_0000"
# Broker URL the brokered client targets for the swagger fetch (host joins the app base host-side).
_BROKER_SWAGGER_URL = "http://rc-broker.internal/bubble/meta/swagger.json"

# A long enum to prove truncation; a long endpoint description to prove summary truncation; a multi-KB
# description to prove the inventory stays compact regardless of swagger bloat. (Real Bubble ops carry
# no `summary`; the inventory falls back to `description`, which apps with option sets do populate.)
_STATUS_ENUM = ["new", "pending", "active", "suspended", "cancelled", "archived", "deleted"]
_LONG_DESC = "'status' field of the current Todo — one of the option-set values the app defines " * 3
_BLOAT = "x" * 20000

# Shaped from a real, sanitized Bubble app swagger: app name replaced with a fake ("acme-support"),
# every operation tagged ["Data"], `<type>Body` companion definitions, display-name field keys with
# spaces (`Created Date`, `Modified Date`, `Created By`) plus the always-present `_id`.
MINI_SWAGGER = {
    "swagger": "2.0",
    "info": {"title": "acme-support", "version": "1.0.0"},
    "host": "acme-support.bubbleapps.io",
    "basePath": "/version-test/api/1.1",
    "paths": {
        "/obj/todo": {
            "get": {"tags": ["Data"], "description": _LONG_DESC},
            "post": {"tags": ["Data"], "description": "Create a new Todo"},
        },
        "/obj/todo/{UniqueID}": {
            "get": {"tags": ["Data"]},
            "patch": {"tags": ["Data"]},
            "put": {"tags": ["Data"]},
            "delete": {"tags": ["Data"]},
        },
        "/obj/user": {
            "get": {"tags": ["Data"]},
            "post": {"tags": ["Data"]},
        },
        "/obj/user/{UniqueID}": {
            "get": {"tags": ["Data"]},
        },
        "/wf/reset-password": {
            "post": {"tags": ["Workflow"], "summary": "Trigger the reset-password workflow"},
        },
    },
    "definitions": {
        "todo": {
            "type": "object",
            "properties": {
                "_id": {"type": "string"},
                "Created Date": {"type": "string", "format": "date-time"},
                "Modified Date": {"type": "string", "format": "date-time"},
                "Created By": {"type": "string"},
                "task_name": {"type": "string"},
                "status": {"type": "string", "enum": _STATUS_ENUM},
                "owner_user": {"type": "string"},
                "Slug": {"type": "string", "description": _BLOAT},
            },
        },
        "todoBody": {
            "type": "object",
            "properties": {
                "task_name": {"type": "string"},
                "status": {"type": "string"},
                "owner_user": {"type": "string"},
            },
        },
        "user": {
            "type": "object",
            "properties": {
                "_id": {"type": "string"},
                "Created Date": {"type": "string", "format": "date-time"},
                "Modified Date": {"type": "string", "format": "date-time"},
                "name": {"type": "string"},
                "authentication": {"type": "object"},
            },
        },
        "userBody": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        },
    },
}


class BubbleManifest(unittest.TestCase):
    def test_manifest_yaml_fields(self):
        """The YAML row is bearer/broker with the offset envelope Bubble's Data API needs."""
        m = api._parse_manifest_file(bubble._MANIFEST_PATH)
        self.assertEqual(m.key, "bubble")
        self.assertEqual(m.auth.strategy, "bearer")
        self.assertIn("{app_domain}", m.base_url)  # templated per app
        self.assertEqual(m.pagination.style, "offset")
        self.assertEqual(m.pagination.offset_param, "cursor")
        self.assertEqual(m.pagination.limit_param, "limit")
        self.assertEqual(m.pagination.items_field, "response.results")
        self.assertEqual(m.pagination.page_size, 100)

    def test_help_md_starts_with_fetch(self):
        """help_md must lead with discovery — the first word is the Fetch instruction (contract)."""
        raw = bubble._MANIFEST_PATH.read_text()
        # The YAML `help_md: |` block's first content line starts with "Fetch".
        self.assertRegex(raw, r"help_md:\s*\|\s*\n\s*Fetch ")


class BubbleDiscovery(unittest.TestCase):
    def test_endpoint_extraction_grouped_and_compact(self):
        rows = bubble.collect_endpoints(MINI_SWAGGER)
        pairs = [(r["method"], r["path"]) for r in rows]
        self.assertIn(("GET", "/obj/todo"), pairs)
        self.assertIn(("POST", "/obj/todo"), pairs)
        self.assertIn(("GET", "/obj/user"), pairs)
        self.assertIn(("POST", "/wf/reset-password"), pairs)
        # Grouped by type/tag, alphabetical.
        groups = [r["group"] for r in rows]
        self.assertEqual(groups, sorted(groups, key=str.lower))

        text = bubble.format_endpoints(rows)
        self.assertIn("## todo", text)
        self.assertIn("GET", text)
        # Compact despite a 20 KB swagger description: inventory stays a few hundred tokens.
        self.assertLess(len(text), 1500)

    def test_data_tagged_ops_group_per_type_not_one_data_bucket(self):
        """Task-4 contract: real Bubble tags EVERY object op ``Data``; grouping must still read
        per-type off the ``/obj/<type>`` segment, never collapse into a single ``## Data`` heading."""
        rows = bubble.collect_endpoints(MINI_SWAGGER, "obj")
        groups = {r["group"] for r in rows}
        self.assertEqual(groups, {"todo", "user"})
        self.assertNotIn("Data", groups)
        text = bubble.format_endpoints(rows)
        self.assertIn("## todo", text)
        self.assertIn("## user", text)
        self.assertNotIn("## Data", text)

    def test_summary_truncation(self):
        rows = bubble.collect_endpoints(MINI_SWAGGER)
        # Real ops have no `summary`; the inventory falls back to the op `description`.
        todo_get = next(r for r in rows if r["path"] == "/obj/todo" and r["method"] == "GET")
        self.assertLessEqual(len(todo_get["summary"]), 80)
        self.assertTrue(todo_get["summary"].endswith("…"))

    def test_path_filter_excludes_workflow(self):
        rows = bubble.collect_endpoints(MINI_SWAGGER, "obj")
        paths = {r["path"] for r in rows}
        self.assertEqual(paths, {"/obj/todo", "/obj/todo/{UniqueID}", "/obj/user", "/obj/user/{UniqueID}"})
        self.assertNotIn("/wf/reset-password", paths)

    def test_types_listing_and_enum_truncation(self):
        rows = bubble.collect_types(MINI_SWAGGER)
        by_name = {r["name"]: r for r in rows}
        # Real swagger exposes the object type AND its `<type>Body` write companion.
        self.assertEqual(set(by_name), {"todo", "todoBody", "user", "userBody"})
        fields = {f["name"]: f["type"] for f in by_name["todo"]["fields"]}
        # Display-name field keys carry spaces; `_id` is always present.
        self.assertEqual(fields["Created Date"], "string")
        self.assertEqual(fields["_id"], "string")
        self.assertIn("task_name", fields)
        # Long enum truncated to a few shown + "+N more".
        self.assertTrue(fields["status"].startswith("enum["))
        self.assertIn("+3 more", fields["status"])

        text = bubble.format_types(rows)
        self.assertIn("todo:", text)
        self.assertIn("Created Date (string)", text)
        # The 20 KB bloat description lives under todo.Slug but never floods the listing.
        self.assertLess(len(text), 1200)

    def test_full_type_prints_complete_schema(self):
        out = bubble.format_full_type(MINI_SWAGGER, "todo")
        self.assertIn("# todo", out)
        self.assertIn("Created Date", out)
        # Case-insensitive match and unknown-type guidance.
        self.assertIn("# user", bubble.format_full_type(MINI_SWAGGER, "USER"))
        self.assertIn("Unknown type", bubble.format_full_type(MINI_SWAGGER, "nope"))

    @responses.activate
    def test_fetch_swagger_through_broker(self):
        """End-to-end through lib.api's brokered request path (no client-side auth header)."""
        responses.add(responses.GET, _BROKER_SWAGGER_URL, json=MINI_SWAGGER, status=200)
        saved = os.environ.get("RC_API_BROKERED_KEYS")
        os.environ["RC_API_BROKERED_KEYS"] = "bubble"
        try:
            swagger = bubble.fetch_swagger()
        finally:
            if saved is None:
                os.environ.pop("RC_API_BROKERED_KEYS", None)
            else:
                os.environ["RC_API_BROKERED_KEYS"] = saved
        self.assertEqual(len(responses.calls), 1)
        self.assertNotIn("Authorization", responses.calls[0].request.headers)  # broker attaches host-side
        rows = bubble.collect_endpoints(swagger)
        self.assertTrue(any(r["path"] == "/obj/user" for r in rows))


class BubbleOffsetPagination(unittest.TestCase):
    """Prove the manifest's offset/``response.results`` envelope stitches pages via the generic paginator."""

    _BASE = "https://myapp.bubbleapps.io/api/1.1"

    def _manifest(self):
        m = api._parse_manifest_file(bubble._MANIFEST_PATH)
        # Concrete host (no {app_domain}) + tiny page so two mocked pages exercise the loop.
        return api.Manifest(
            key=m.key,
            base_url=self._BASE,
            auth=m.auth,
            pagination=api.Pagination(
                style="offset",
                offset_param="cursor",
                limit_param="limit",
                page_size=2,
                items_field="response.results",
            ),
            rate_limit_remaining_header=m.rate_limit_remaining_header,
        )

    @responses.activate
    def test_two_page_offset_stitch(self):
        url = f"{self._BASE}/obj/user"
        responses.add(responses.GET, url, json={
            "response": {"results": [{"_id": "a"}, {"_id": "b"}], "cursor": 0, "count": 2, "remaining": 1},
        }, status=200)
        responses.add(responses.GET, url, json={
            "response": {"results": [{"_id": "c"}], "cursor": 2, "count": 1, "remaining": 0},
        }, status=200)

        c = api.Client(manifest=self._manifest(), credential=_FAKE_TOKEN)
        result = c.collect("obj/user")
        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual([it["_id"] for it in result["items"]], ["a", "b", "c"])
        # Page 2 advanced the cursor by the first page's length (offset semantics).
        self.assertEqual(len(responses.calls), 2)
        self.assertIn("cursor=2", responses.calls[1].request.url)


if __name__ == "__main__":
    unittest.main()
