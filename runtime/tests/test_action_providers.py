"""Tests for minimal lib.action provider write helpers."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import action, api  # noqa: E402
from lib.action import googledrive, notion  # noqa: E402


class FakeClient:
    def __init__(self):
        self.calls = []
        self.reject_page_after = False
        self.data_source = {
            "object": "data_source",
            "id": "ds_1",
            "title": [{"plain_text": "orders"}],
            "properties": {
                "product name": {"id": "title", "name": "product name", "type": "title", "title": {}},
                "status": {
                    "id": "status",
                    "name": "status",
                    "type": "status",
                    "status": {"options": [{"name": "shipped"}, {"name": "unpaid"}]},
                },
                "quantity": {"id": "qty", "name": "quantity", "type": "number", "number": {}},
                "customer email": {"id": "email", "name": "customer email", "type": "email", "email": {}},
            },
        }
        self.blocks = {
            "page_1": [
                {
                    "id": "block_1",
                    "type": "to_do",
                    "parent": {"page_id": "page_1"},
                    "has_children": False,
                    "to_do": {
                        "checked": False,
                        "rich_text": [{"type": "text", "plain_text": "more tips and tricks to best use Notion", "text": {"content": "more tips and tricks to best use Notion"}}],
                    },
                }
            ]
        }

    def get(self, path, **kw):
        self.calls.append(("GET", path, kw))
        if path == "data_sources/ds_1":
            return self.data_source
        if path == "pages/page_1":
            return {"id": "page_1", "url": "https://notion.test/page", "parent": {"data_source_id": "ds_1"}, "properties": {}}
        if path == "blocks/page_1/children":
            return {"results": self.blocks["page_1"], "has_more": False}
        raise AssertionError(f"unexpected GET {path}")

    def patch(self, path, **kw):
        self.calls.append(("PATCH", path, kw))
        if path.endswith("/children"):
            child = kw.get("json", {}).get("children", [{}])[0]
            if self.reject_page_after and path == "blocks/page_1/children" and "after" in kw.get("json", {}):
                raise api.ApiError(400, 'body failed validation: body.after should be not present, instead was `"block_1"`.')
            block_id = "block_1" if child.get("type") == "bookmark" else "block_inserted"
            return {"results": [{"id": block_id}]}
        if path.startswith("blocks/"):
            return {"id": path.split("/")[1], "type": "to_do"}
        return {"id": "page_1", "url": "https://notion.test/page", "properties": {}}

    def post(self, path, **kw):
        self.calls.append(("POST", path, kw))
        return {"id": "page_2", "url": "https://notion.test/new", "properties": {}}


class NotionActions(unittest.TestCase):
    def test_append_file_link_shape(self):
        fake = FakeClient()
        with mock.patch.object(notion, "_client", return_value=fake):
            block = notion.append_file_link(page_id="page_1", title="Invoice", url="https://drive/file")
        self.assertEqual(block.id, "block_1")
        method, path, kw = fake.calls[0]
        self.assertEqual((method, path), ("PATCH", "blocks/page_1/children"))
        child = kw["json"]["children"][0]
        self.assertEqual(child["type"], "bookmark")
        self.assertEqual(child["bookmark"]["url"], "https://drive/file")
        self.assertEqual(child["bookmark"]["caption"][0]["text"]["content"], "Invoice")

    def test_create_and_update_page_shapes(self):
        fake = FakeClient()
        with mock.patch.object(notion, "_client", return_value=fake):
            created = notion.create_page(parent_id="parent_1", title="New page")
            updated = notion.update_properties(page_id="page_1", properties={"Status": {"status": {"name": "Done"}}})
        self.assertEqual(created.id, "page_2")
        self.assertEqual(updated.id, "page_1")
        self.assertEqual(fake.calls[0][1], "pages")
        self.assertEqual(fake.calls[0][2]["json"]["parent"], {"page_id": "parent_1"})
        self.assertEqual(fake.calls[0][2]["json"]["properties"]["title"]["title"][0]["text"]["content"], "New page")
        self.assertEqual(fake.calls[1][1], "pages/page_1")

    def test_database_row_create_validates_schema_and_options(self):
        fake = FakeClient()
        values = {
            "product name": "baseball",
            "status": "shipped",
            "quantity": 5,
            "customer email": "foo@bar.com",
        }
        with mock.patch.object(notion, "_client", return_value=fake):
            checked = notion.validate_database_values("ds_1", values)
            page = notion.create_database_row(database_id="ds_1", values=values)

        self.assertEqual(page.id, "page_2")
        self.assertEqual(checked.properties["status"], {"status": {"name": "shipped"}})
        self.assertEqual(checked.properties["quantity"], {"number": 5})
        _, path, kw = fake.calls[-1]
        self.assertEqual(path, "pages")
        self.assertEqual(kw["json"]["parent"], {"data_source_id": "ds_1"})
        self.assertEqual(kw["json"]["properties"]["product name"]["title"][0]["text"]["content"], "baseball")

    def test_database_validation_reports_bad_select_with_valid_values(self):
        fake = FakeClient()
        with mock.patch.object(notion, "_client", return_value=fake):
            with self.assertRaises(action.ActionError) as cm:
                notion.validate_database_values("ds_1", {"status": "delivered"})
        msg = str(cm.exception)
        self.assertIn("Valid values: shipped, unpaid", msg)

    def test_database_update_checks_record_parent_and_shapes_patch(self):
        fake = FakeClient()
        with mock.patch.object(notion, "_client", return_value=fake):
            page = notion.update_database_row(database_id="ds_1", record_id="page_1", values={"status": "unpaid"})
        self.assertEqual(page.id, "page_1")
        self.assertEqual(fake.calls[-1][0:2], ("PATCH", "pages/page_1"))
        self.assertEqual(fake.calls[-1][2]["json"]["properties"], {"status": {"status": {"name": "unpaid"}}})

    def test_page_replacement_finds_one_editable_block(self):
        fake = FakeClient()
        new_text = "more tips and tricks to best use Notion\n[] and here is another bullet point"
        with mock.patch.object(notion, "_client", return_value=fake):
            match = notion.validate_page_replacement(
                page_id="page_1",
                old_str="more tips and tricks to best use Notion",
                new_str=new_text,
            )
            block = notion.replace_page_text(
                page_id="page_1",
                old_str="more tips and tricks to best use Notion",
                new_str=new_text,
            )
        self.assertEqual(match.block_id, "block_1")
        self.assertEqual(block.id, "block_1")
        self.assertEqual(fake.calls[-2][0:2], ("PATCH", "blocks/block_1"))
        self.assertEqual(fake.calls[-2][2]["json"]["to_do"]["rich_text"][0]["text"]["content"], "more tips and tricks to best use Notion")
        self.assertEqual(fake.calls[-1][0:2], ("PATCH", "blocks/page_1/children"))
        self.assertEqual(fake.calls[-1][2]["json"]["after"], "block_1")
        child = fake.calls[-1][2]["json"]["children"][0]
        self.assertEqual(child["type"], "to_do")
        self.assertFalse(child["to_do"]["checked"])
        self.assertEqual(child["to_do"]["rich_text"][0]["text"]["content"], "and here is another bullet point")

    def test_page_replacement_retries_without_after_when_page_append_rejects_it(self):
        fake = FakeClient()
        fake.reject_page_after = True
        new_text = "more tips and tricks to best use Notion\n[] and here is another bullet point"
        with mock.patch.object(notion, "_client", return_value=fake):
            block = notion.replace_page_text(
                page_id="page_1",
                old_str="more tips and tricks to best use Notion",
                new_str=new_text,
            )
        self.assertEqual(block.id, "block_1")
        self.assertEqual(fake.calls[-2][0:2], ("PATCH", "blocks/page_1/children"))
        self.assertIn("after", fake.calls[-2][2]["json"])
        self.assertEqual(fake.calls[-1][0:2], ("PATCH", "blocks/page_1/children"))
        self.assertNotIn("after", fake.calls[-1][2]["json"])

    def test_page_replacement_no_match_suggests_nearby_text(self):
        fake = FakeClient()
        with mock.patch.object(notion, "_client", return_value=fake):
            with self.assertRaises(action.ActionError) as cm:
                notion.validate_page_replacement(page_id="page_1", old_str="tips to best use docs", new_str="x")
        self.assertIn("Nearby editable text", str(cm.exception))


class GoogleDriveActions(unittest.TestCase):
    @responses.activate
    def test_upload_file_shape(self):
        def cb(request):
            self.assertIn("uploadType=multipart", request.url)
            self.assertIn("fields=id%2Cname%2CwebViewLink", request.url)
            self.assertIn("multipart/related", request.headers["Content-Type"])
            self.assertIn(b'"parents":["folder_1"]', request.body)
            self.assertIn(b'"name":"report.txt"', request.body)
            self.assertIn(b"hello", request.body)
            return (200, {"Content-Type": "application/json"}, '{"id":"file_1","name":"report.txt","webViewLink":"https://drive/file"}')

        responses.add_callback(
            responses.POST,
            "https://www.googleapis.com/upload/drive/v3/files",
            callback=cb,
        )
        googledrive._client.cache_clear()
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "report.txt"
            src.write_text("hello", encoding="utf-8")
            fp = action.FileParam(path=src, filename="report.txt", mime_type="text/plain", size_bytes=5)
            with mock.patch.dict(os.environ, {"RC_ACTION_GOOGLEDRIVE": "write-token"}, clear=True):
                got = googledrive.upload_file(folder_id="folder_1", file=fp)
        self.assertEqual(got.id, "file_1")
        self.assertEqual(got.web_url, "https://drive/file")
        self.assertEqual(responses.calls[0].request.headers["Authorization"], "Bearer write-token")

    def test_upload_attachment_uses_attachment_metadata(self):
        fake = FakeClient()
        fake.upload = mock.Mock(return_value={"id": "file_2", "name": "invoice.pdf", "webViewLink": "https://drive/invoice"})
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "delivered.bin"
            src.write_bytes(b"pdf")
            fp = action.FileParam(path=src, filename="invoice.pdf", mime_type="application/pdf", size_bytes=3)
            with mock.patch.object(googledrive, "_client", return_value=fake):
                got = googledrive.upload_attachment(folder_id="folder_1", attachment=fp)
        self.assertEqual(got.id, "file_2")
        fake.upload.assert_called_once()
        _, kw = fake.upload.call_args
        self.assertEqual(kw["metadata"], {"name": "invoice.pdf", "parents": ["folder_1"]})
        self.assertEqual(kw["content_type"], "application/pdf")


if __name__ == "__main__":
    unittest.main()
