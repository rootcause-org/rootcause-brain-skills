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

from lib import action  # noqa: E402
from lib.action import googledrive, notion  # noqa: E402


class FakeClient:
    def __init__(self):
        self.calls = []

    def patch(self, path, **kw):
        self.calls.append(("PATCH", path, kw))
        if path.startswith("blocks/"):
            return {"results": [{"id": "block_1"}]}
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
