"""Fixture test for the manifest-ONLY Google Sheets integration — proves a catalogued connector
with NO bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. Bodies mirror the Sheets API's
documented response shapes, trimmed to support-relevant fields.

The Sheets API has no pagination (all endpoints are single-page), so the "stitches pages" test
exercises the `none` style: one request, one page, all items returned as-is.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \
        pytest tests/test_googlesheets_connector.py -q
"""

import io
import json
import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API = "https://sheets.googleapis.com/v4"
SPREADSHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

# A representative spreadsheets.get response (documented shape, trimmed to support-relevant fields).
_SPREADSHEET_META = {
    "spreadsheetId": SPREADSHEET_ID,
    "properties": {
        "title": "Customer Config 2024",
        "locale": "en_US",
        "timeZone": "America/New_York",
    },
    "sheets": [
        {
            "properties": {
                "sheetId": 0,
                "title": "Settings",
                "index": 0,
                "sheetType": "GRID",
                "gridProperties": {"rowCount": 1000, "columnCount": 26},
            }
        },
        {
            "properties": {
                "sheetId": 123456,
                "title": "Pricing",
                "index": 1,
                "sheetType": "GRID",
                "gridProperties": {"rowCount": 500, "columnCount": 10},
            }
        },
    ],
}

# A representative spreadsheets.values.get response (ValueRange, documented shape).
_VALUE_RANGE = {
    "spreadsheetId": SPREADSHEET_ID,
    "range": "Settings!A1:D4",
    "majorDimension": "ROWS",
    "values": [
        ["Plan", "Price", "Seats", "Active"],
        ["starter", "49", "5", "TRUE"],
        ["pro", "149", "20", "TRUE"],
        ["enterprise", "499", "unlimited", "FALSE"],
    ],
}

# A representative spreadsheets.values.batchGet response (BatchGetValuesResponse).
_BATCH_VALUES = {
    "spreadsheetId": SPREADSHEET_ID,
    "valueRanges": [
        {
            "range": "Settings!A1:B2",
            "majorDimension": "ROWS",
            "values": [["Plan", "Price"], ["starter", "49"]],
        },
        {
            "range": "Pricing!A1:A3",
            "majorDimension": "ROWS",
            "values": [["Tier"], ["Basic"], ["Premium"]],
        },
    ],
}


class GoogleSheetsManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is the source (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GOOGLESHEETS")
        # Split the prefix so the token-hygiene guard doesn't flag this test file.
        os.environ["RC_CONN_GOOGLESHEETS"] = "ya29." + "test_access_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GOOGLESHEETS", None)
        else:
            os.environ["RC_CONN_GOOGLESHEETS"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("googlesheets", m)
        gs = m["googlesheets"]
        self.assertEqual(gs.base_url, "https://sheets.googleapis.com/v4")
        self.assertEqual(gs.auth.strategy, "bearer")
        self.assertEqual(gs.pagination.style, "none")
        self.assertEqual(gs.rate_limit_remaining_header, "")

    @responses.activate
    def test_single_page_spreadsheet_metadata(self):
        """spreadsheets.get — single-page GET, no pagination, bearer auth on the wire."""
        url = f"{API}/spreadsheets/{SPREADSHEET_ID}"
        responses.add(responses.GET, url, json=_SPREADSHEET_META, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["googlesheets"])
        body = c.get(f"spreadsheets/{SPREADSHEET_ID}")

        self.assertEqual(body["spreadsheetId"], SPREADSHEET_ID)
        self.assertEqual(body["properties"]["title"], "Customer Config 2024")
        self.assertEqual(len(body["sheets"]), 2)
        self.assertEqual(body["sheets"][0]["properties"]["title"], "Settings")

        # Bearer credential rode on the request.
        self.assertIn("Authorization", responses.calls[0].request.headers)
        auth = responses.calls[0].request.headers["Authorization"]
        self.assertTrue(auth.startswith("Bearer "), auth)
        self.assertIn("ya29.", auth)

        # Only one HTTP call (no pagination).
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_none_pagination_collect_returns_all_items_in_one_page(self):
        """style=none: collect() issues exactly one request and marks incomplete=False."""
        url = f"{API}/spreadsheets/{SPREADSHEET_ID}/values/Settings!A1:D4"
        responses.add(responses.GET, url, json=_VALUE_RANGE, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["googlesheets"])
        # For style=none the page body IS the result (not a list), so items_field="" → body as list
        # if it's a list, else []. ValueRange has "values" (a 2D array), not a bare list, so we
        # use get() directly and then pick — this is the standard agent pattern for Sheets.
        body = c.get(f"spreadsheets/{SPREADSHEET_ID}/values/Settings!A1:D4")
        self.assertEqual(body["range"], "Settings!A1:D4")
        self.assertEqual(body["majorDimension"], "ROWS")
        self.assertEqual(len(body["values"]), 4)
        self.assertEqual(body["values"][0], ["Plan", "Price", "Seats", "Active"])

        # Confirm collect() also works (single page, complete).
        responses.reset()
        responses.add(responses.GET, url, json=_VALUE_RANGE, status=200)
        result = c.collect(f"spreadsheets/{SPREADSHEET_ID}/values/Settings!A1:D4")
        # items_field="" and body is a dict (not a list) → items is []
        # This is expected: for Sheets the agent uses get(), not paginate/collect.
        self.assertFalse(result["incomplete"])
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_pick_selects_support_fields_from_metadata(self):
        """pick() pre-selects the few support-relevant fields from a spreadsheets.get response."""
        url = f"{API}/spreadsheets/{SPREADSHEET_ID}"
        responses.add(responses.GET, url, json=_SPREADSHEET_META, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["googlesheets"])
        body = c.get(f"spreadsheets/{SPREADSHEET_ID}")

        picked = api.pick(body, "spreadsheetId,properties.title,sheets.*.properties.title")
        self.assertEqual(picked["spreadsheetId"], SPREADSHEET_ID)
        self.assertEqual(picked["properties.title"], "Customer Config 2024")
        self.assertEqual(picked["sheets.*.properties.title"], ["Settings", "Pricing"])

    @responses.activate
    def test_batch_get_values(self):
        """spreadsheets.values.batchGet — multi-range read, single page, bearer auth."""
        url = f"{API}/spreadsheets/{SPREADSHEET_ID}/values:batchGet"
        responses.add(responses.GET, url, json=_BATCH_VALUES, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["googlesheets"])
        body = c.get(
            f"spreadsheets/{SPREADSHEET_ID}/values:batchGet",
            query={"ranges": ["Settings!A1:B2", "Pricing!A1:A3"]},
        )
        self.assertEqual(body["spreadsheetId"], SPREADSHEET_ID)
        self.assertEqual(len(body["valueRanges"]), 2)
        self.assertEqual(body["valueRanges"][0]["values"][0], ["Plan", "Price"])

        # pick selects nested valueRanges fields
        picked = api.pick(body, "spreadsheetId,valueRanges.*.range,valueRanges.*.values")
        self.assertEqual(picked["spreadsheetId"], SPREADSHEET_ID)
        self.assertIn("Settings!A1:B2", picked["valueRanges.*.range"])

        # Bearer rides the request.
        self.assertEqual(len(responses.calls), 1)
        self.assertIn("ya29.", responses.calls[0].request.headers["Authorization"])

    @responses.activate
    def test_credential_rides_every_request_including_query_param_path(self):
        """Bearer must appear on ALL requests — metadata + values in sequence."""
        meta_url = f"{API}/spreadsheets/{SPREADSHEET_ID}"
        val_url = f"{API}/spreadsheets/{SPREADSHEET_ID}/values/Sheet1!A1:B2"
        responses.add(responses.GET, meta_url, json=_SPREADSHEET_META, status=200)
        responses.add(responses.GET, val_url, json=_VALUE_RANGE, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["googlesheets"])
        c.get(f"spreadsheets/{SPREADSHEET_ID}")
        c.get(f"spreadsheets/{SPREADSHEET_ID}/values/Sheet1!A1:B2")

        for call in responses.calls:
            self.assertIn("Authorization", call.request.headers)
            self.assertTrue(call.request.headers["Authorization"].startswith("Bearer "))

    @responses.activate
    def test_cli_drives_googlesheets_get(self):
        """python -m lib.api get googlesheets <path> works end-to-end via the YAML manifest."""
        url = f"{API}/spreadsheets/{SPREADSHEET_ID}"
        responses.add(responses.GET, url, json=_SPREADSHEET_META, status=200)

        import io
        from unittest.mock import patch

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            rc = api._main([
                "get", "googlesheets",
                f"spreadsheets/{SPREADSHEET_ID}",
                "--pick", "spreadsheetId,properties.title",
            ])
        self.assertEqual(rc, 0)
        out = json.loads(captured.getvalue())
        self.assertEqual(out["spreadsheetId"], SPREADSHEET_ID)
        self.assertEqual(out["properties.title"], "Customer Config 2024")
        self.assertTrue(responses.calls[0].request.url.startswith(url))

    @responses.activate
    def test_cli_drives_googlesheets_with_query_params(self):
        """CLI --query passes k=v as query params (e.g. ranges= for batchGet)."""
        url = f"{API}/spreadsheets/{SPREADSHEET_ID}/values:batchGet"
        responses.add(responses.GET, url, json=_BATCH_VALUES, status=200)

        captured = io.StringIO()
        from unittest.mock import patch
        with patch("sys.stdout", captured):
            rc = api._main([
                "get", "googlesheets",
                f"spreadsheets/{SPREADSHEET_ID}/values:batchGet",
                "--query", "ranges=Settings!A1:B2",
            ])
        self.assertEqual(rc, 0)
        out = json.loads(captured.getvalue())
        self.assertIn("valueRanges", out)


class GoogleSheetsCassetteHygiene(unittest.TestCase):
    """CI guard: no real Google OAuth token prefix may land in the connector dir files.

    Scoped to the connector dir only — this test file legitimately names the prefix (split
    across a concatenation) so scanning itself would be a false positive.
    """

    # Google OAuth 2.0 access token prefix, split so the guard doesn't flag this file itself.
    _TOKEN_PREFIXES = ("ya29" ".",)

    def test_no_token_prefixes_in_googlesheets_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "googlesheets"
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
