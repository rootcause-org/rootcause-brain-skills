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
from lib.action import airtable, googledrive, notion  # noqa: E402


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
                    "status": {"options": [{"id": "opt_shipped", "name": "shipped"}, {"id": "opt_unpaid", "name": "unpaid"}]},
                },
                "quantity": {"id": "qty", "name": "quantity", "type": "number", "number": {}},
                "customer email": {"id": "email", "name": "customer email", "type": "email", "email": {}},
                "notes": {"id": "notes", "name": "notes", "type": "rich_text", "rich_text": {}},
                "tags": {
                    "id": "tags",
                    "name": "tags",
                    "type": "multi_select",
                    "multi_select": {"options": [{"id": "tag_vip", "name": "vip"}, {"id": "tag_late", "name": "late"}]},
                },
                "paid": {"id": "paid", "name": "paid", "type": "checkbox", "checkbox": {}},
                "website": {"id": "website", "name": "website", "type": "url", "url": {}},
                "phone": {"id": "phone", "name": "phone", "type": "phone_number", "phone_number": {}},
                "due": {"id": "due", "name": "due", "type": "date", "date": {}},
                "related": {"id": "related", "name": "related", "type": "relation", "relation": {}},
                "owner": {"id": "owner", "name": "owner", "type": "people", "people": {}},
                "files": {"id": "files", "name": "files", "type": "files", "files": {}},
                "verification": {"id": "verification", "name": "verification", "type": "verification", "verification": {}},
                "invoice no": {"id": "invoice_no", "name": "invoice no", "type": "unique_id", "unique_id": {}},
                "computed": {"id": "computed", "name": "computed", "type": "formula", "formula": {}},
                "place": {"id": "place", "name": "place", "type": "place", "place": None},
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

    def test_database_validation_covers_common_writable_property_types(self):
        fake = FakeClient()
        values = {
            "product name": "baseball",
            "notes": "gift wrap",
            "status": {"id": "opt_shipped"},
            "tags": ["vip", {"id": "tag_late"}],
            "quantity": 5,
            "customer email": "foo@bar.com",
            "paid": True,
            "website": "https://example.com/order",
            "phone": "+32 2 000 00 00",
            "due": {"start": "2026-07-10", "end": "2026-07-11"},
            "related": ["9f1a4f418a25420a982b1c04128f7120"],
            "owner": [{"id": "user_1"}],
            "files": [{"name": "invoice.pdf", "external": {"url": "https://example.com/invoice.pdf"}}],
            "verification": {"state": "verified", "date": "2026-07-10"},
        }
        with mock.patch.object(notion, "_client", return_value=fake):
            checked = notion.validate_database_values("ds_1", values)

        props = checked.properties
        self.assertEqual(props["status"], {"status": {"id": "opt_shipped"}})
        self.assertEqual(props["tags"], {"multi_select": [{"name": "vip"}, {"id": "tag_late"}]})
        self.assertEqual(props["paid"], {"checkbox": True})
        self.assertEqual(props["website"], {"url": "https://example.com/order"})
        self.assertEqual(props["due"], {"date": {"start": "2026-07-10", "end": "2026-07-11"}})
        self.assertEqual(props["related"], {"relation": [{"id": "9f1a4f418a25420a982b1c04128f7120"}]})
        self.assertEqual(props["owner"], {"people": [{"id": "user_1"}]})
        self.assertEqual(props["files"]["files"][0]["external"]["url"], "https://example.com/invoice.pdf")
        self.assertEqual(props["verification"], {"verification": {"state": "verified", "date": {"start": "2026-07-10"}}})

        with mock.patch.object(notion, "_client", return_value=fake):
            expired = notion.validate_database_values("ds_1", {"verification": {"state": "expired"}})
        self.assertEqual(expired.properties["verification"], {"verification": {"state": "expired"}})

    def test_database_validation_checks_native_payloads_too(self):
        fake = FakeClient()
        values = {
            "status": {"status": {"name": "lost"}},
            "tags": {"multi_select": [{"id": "missing"}]},
            "paid": {"checkbox": "yes"},
        }
        with mock.patch.object(notion, "_client", return_value=fake):
            with self.assertRaises(action.ActionError) as cm:
                notion.validate_database_values("ds_1", values)
        msg = str(cm.exception)
        self.assertIn("Valid values: shipped, unpaid", msg)
        self.assertIn("Valid option IDs: tag_vip (vip), tag_late (late)", msg)
        self.assertIn("column 'paid' is checkbox; provide true or false", msg)

    def test_database_validation_reports_invalid_types_read_only_and_unsupported_columns(self):
        fake = FakeClient()
        values = {
            "invoice no": "RC-1",
            "computed": 3,
            "place": {"name": "Brussels"},
            "customer email": "not-an-email",
            "website": "ftp://example.com",
            "due": "soon",
            "verification": {"state": "retired"},
            "files": [{"external": {"url": "https://example.com/invoice.pdf"}}],
        }
        with mock.patch.object(notion, "_client", return_value=fake):
            with self.assertRaises(action.ActionError) as cm:
                notion.validate_database_values("ds_1", values)
        msg = str(cm.exception)
        self.assertIn("column 'invoice no' is read-only (unique_id)", msg)
        self.assertIn("column 'computed' is read-only (formula)", msg)
        self.assertIn("column 'place' is unsupported by the Notion API (place)", msg)
        self.assertIn("'not-an-email' is not a valid email address", msg)
        self.assertIn("provide an http(s) URL", msg)
        self.assertIn("'soon' is not ISO-8601", msg)
        self.assertIn("Valid values: verified, unverified, expired", msg)
        self.assertIn("external files require a non-empty name", msg)

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


class AirtableFakeClient:
    def __init__(self):
        self.calls = []
        self.schema = {
            "tables": [
                {
                    "id": "tbl_orders",
                    "name": "Orders",
                    "fields": [
                        {"id": "fld_name", "name": "Name", "type": "singleLineText"},
                        {
                            "id": "fld_status",
                            "name": "Status",
                            "type": "singleSelect",
                            "options": {"choices": [{"id": "sel_new", "name": "New"}, {"id": "sel_done", "name": "Done"}]},
                        },
                        {
                            "id": "fld_tags",
                            "name": "Tags",
                            "type": "multipleSelects",
                            "options": {"choices": [{"id": "tag_vip", "name": "VIP"}, {"id": "tag_late", "name": "Late"}]},
                        },
                        {"id": "fld_qty", "name": "Quantity", "type": "number"},
                        {"id": "fld_price", "name": "Price", "type": "currency"},
                        {"id": "fld_percent", "name": "Progress", "type": "percent"},
                        {"id": "fld_done", "name": "Done", "type": "checkbox"},
                        {"id": "fld_email", "name": "Email", "type": "email"},
                        {"id": "fld_url", "name": "Website", "type": "url"},
                        {"id": "fld_phone", "name": "Phone", "type": "phoneNumber"},
                        {"id": "fld_date", "name": "Ship date", "type": "date"},
                        {"id": "fld_at", "name": "Ship at", "type": "dateTime"},
                        {"id": "fld_rating", "name": "Rating", "type": "rating", "options": {"max": 5}},
                        {"id": "fld_duration", "name": "Duration", "type": "duration"},
                        {"id": "fld_barcode", "name": "Barcode", "type": "barcode"},
                        {"id": "fld_notes", "name": "Notes", "type": "multilineText"},
                        {"id": "fld_rich", "name": "Rich notes", "type": "richText"},
                        {"id": "fld_owner", "name": "Owner", "type": "singleCollaborator"},
                        {"id": "fld_watchers", "name": "Watchers", "type": "multipleCollaborators"},
                        {"id": "fld_related", "name": "Related", "type": "multipleRecordLinks"},
                        {"id": "fld_files", "name": "Files", "type": "multipleAttachments"},
                        {"id": "fld_auto", "name": "Auto", "type": "autoNumber"},
                        {"id": "fld_formula", "name": "Computed", "type": "formula"},
                        {"id": "fld_lookup", "name": "Lookup", "type": "multipleLookupValues"},
                        {"id": "fld_ai", "name": "AI", "type": "aiText"},
                    ],
                },
                {"id": "tbl_other", "name": "Returns", "fields": []},
            ]
        }

    def get(self, path, **kw):
        self.calls.append(("GET", path, kw))
        if path == "meta/bases/app_1/tables":
            return self.schema
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, **kw):
        self.calls.append(("POST", path, kw))
        return {"id": "rec_new", "fields": kw["json"]["fields"], "createdTime": "2026-07-09T00:00:00.000Z"}

    def patch(self, path, **kw):
        self.calls.append(("PATCH", path, kw))
        return {"id": path.split("/")[-1], "fields": kw["json"]["fields"], "createdTime": "2026-07-09T00:00:00.000Z"}


class AirtableActions(unittest.TestCase):
    def test_record_create_and_update_shapes(self):
        fake = AirtableFakeClient()
        fields = {"Name": "Baseball", "Status": "Done"}
        with mock.patch.object(airtable, "_client", return_value=fake):
            created = airtable.create_record(base_id="app_1", table_id_or_name="Orders", fields=fields)
            updated = airtable.update_record(base_id="app_1", table_id_or_name="tbl_orders", record_id="rec_1", fields=fields)

        self.assertEqual(created.id, "rec_new")
        self.assertEqual(updated.id, "rec_1")
        self.assertEqual(fake.calls[-3][0:2], ("POST", "app_1/tbl_orders"))
        self.assertEqual(fake.calls[-3][2]["json"], {"fields": {"Name": "Baseball", "Status": "Done"}})
        self.assertEqual(fake.calls[-1][0:2], ("PATCH", "app_1/tbl_orders/rec_1"))
        self.assertEqual(fake.calls[-1][2]["json"]["fields"]["Status"], "Done")

    def test_record_validation_covers_common_writable_field_types(self):
        fake = AirtableFakeClient()
        values = {
            "Name": "Baseball",
            "Status": "New",
            "Tags": ["VIP", "Late"],
            "Quantity": 2,
            "Price": 12.5,
            "Progress": 0.25,
            "Done": True,
            "Email": "customer@example.com",
            "Website": "https://example.com/order",
            "Phone": "+32 2 000 00 00",
            "Ship date": "2026-07-10",
            "Ship at": "2026-07-10T12:34:56.000Z",
            "Rating": 5,
            "Duration": 3600,
            "Barcode": {"text": "1234567890", "type": "code39"},
            "Notes": "gift wrap",
            "Rich notes": "**gift** wrap",
            "Owner": {"email": "owner@example.com"},
            "Watchers": ["usr_1", "grp_1"],
            "Related": ["rec_linked"],
            "Files": [{"url": "https://example.com/invoice.pdf", "filename": "invoice.pdf"}],
        }
        with mock.patch.object(airtable, "_client", return_value=fake):
            checked = airtable.validate_record_fields("app_1", "Orders", values)

        self.assertEqual(checked.fields["Status"], "New")
        self.assertEqual(checked.fields["Tags"], ["VIP", "Late"])
        self.assertEqual(checked.fields["Owner"], {"email": "owner@example.com"})
        self.assertEqual(checked.fields["Watchers"], ["usr_1", "grp_1"])
        self.assertEqual(checked.fields["Related"], ["rec_linked"])
        self.assertEqual(checked.fields["Files"][0]["filename"], "invoice.pdf")
        self.assertEqual(checked.fields["Barcode"], {"text": "1234567890", "type": "code39"})

    def test_record_validation_accepts_field_ids_and_reports_summary(self):
        fake = AirtableFakeClient()
        with mock.patch.object(airtable, "_client", return_value=fake):
            checked = airtable.validate_record_fields("app_1", "tbl_orders", {"fld_status": "Done"})
        self.assertEqual(checked.fields, {"Status": "Done"})
        self.assertIn("Airtable table **Orders**", airtable.record_validation_summary(checked, operation="Dry run: update record"))

    def test_record_validation_reports_bad_selects_unknown_fields_and_suggestions(self):
        fake = AirtableFakeClient()
        values = {"Stauts": "Done", "Status": "Shipped", "Tags": ["VIP", "Cold"]}
        with mock.patch.object(airtable, "_client", return_value=fake):
            with self.assertRaises(action.ActionError) as cm:
                airtable.validate_record_fields("app_1", "Orders", values)
        msg = str(cm.exception)
        self.assertIn("Available fields: Name (singleLineText); Status (singleSelect: New, Done)", msg)
        self.assertIn("Did you mean: Status", msg)
        self.assertIn("'Shipped' is not valid. Valid values: New, Done", msg)
        self.assertIn("'Cold' is not valid. Valid values: VIP, Late", msg)

    def test_record_typecast_allows_new_select_choices_when_explicit(self):
        fake = AirtableFakeClient()
        values = {"Status": "Shipped", "Tags": ["VIP", "Cold"]}
        with mock.patch.object(airtable, "_client", return_value=fake):
            checked = airtable.validate_record_fields("app_1", "Orders", values, typecast=True)
            created = airtable.create_record(base_id="app_1", table_id_or_name="Orders", fields=values, typecast=True)

        self.assertEqual(checked.fields, values)
        self.assertEqual(created.id, "rec_new")
        self.assertEqual(fake.calls[-1][2]["json"], {"fields": values, "typecast": True})

    def test_record_validation_reports_read_only_and_invalid_common_types(self):
        fake = AirtableFakeClient()
        values = {
            "Auto": 1,
            "Computed": 2,
            "Lookup": "x",
            "AI": "x",
            "Done": "yes",
            "Email": "not-email",
            "Website": "ftp://example.com",
            "Ship date": "tomorrow",
            "Ship at": "later",
            "Rating": 6,
            "Duration": 1.5,
            "Files": [{"filename": "invoice.pdf"}],
            "Barcode": {"type": "code39"},
        }
        with mock.patch.object(airtable, "_client", return_value=fake):
            with self.assertRaises(action.ActionError) as cm:
                airtable.validate_record_fields("app_1", "Orders", values)
        msg = str(cm.exception)
        self.assertIn("field 'Auto' is read-only (autoNumber)", msg)
        self.assertIn("field 'Computed' is read-only (formula)", msg)
        self.assertIn("field 'Lookup' is read-only (multipleLookupValues)", msg)
        self.assertIn("field 'AI' is read-only (aiText)", msg)
        self.assertIn("field 'Done' is checkbox; provide true or false", msg)
        self.assertIn("'not-email' is not a valid email address", msg)
        self.assertIn("provide an http(s) URL", msg)
        self.assertIn("'tomorrow' is not an ISO date", msg)
        self.assertIn("'later' is not ISO-8601", msg)
        self.assertIn("provide an integer from 1 to 5", msg)
        self.assertIn("provide a non-negative integer number of seconds", msg)
        self.assertIn("each attachment needs a url or existing id", msg)
        self.assertIn("text must be a non-empty string", msg)

    def test_table_schema_unknown_table_lists_available_tables(self):
        fake = AirtableFakeClient()
        with mock.patch.object(airtable, "_client", return_value=fake):
            with self.assertRaises(action.ActionError) as cm:
                airtable.retrieve_table_schema("app_1", "Order")
        msg = str(cm.exception)
        self.assertIn("Available tables: Orders (tbl_orders); Returns (tbl_other)", msg)
        self.assertIn("Did you mean: Orders", msg)


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
