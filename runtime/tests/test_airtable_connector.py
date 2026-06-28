"""Fixture test for the manifest-ONLY Airtable integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. The fixture bodies mirror Airtable's
DOCUMENTED example payloads (developers.airtable.com List Records / List Bases), trimmed to
support-relevant fields. Airtable paginates via an opaque `offset` string in the response body
(sent back as the `offset` query param), mapped to lib.api's `cursor` pagination style.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \
        pytest tests/test_airtable_connector.py -q
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

BASE_URL = "https://api.airtable.com/v0"
BASE_ID = "appLkNDICXNqxSDhG"
TABLE_ID = "tblSomeTableId1234"

RECORDS_URL = f"{BASE_URL}/{BASE_ID}/{TABLE_ID}"
BASES_URL = f"{BASE_URL}/meta/bases"

# Two pages of records — first page carries an opaque offset string; second page has none (→ stop).
# Shapes mirror the documented List Records response: { records: [...], offset: "..." }.
_PAGE_1 = {
    "records": [
        {
            "id": "recABCDEF12345678",
            "createdTime": "2023-01-15T10:00:00.000Z",
            "fields": {
                "Name": "Alice",
                "Status": "Active",
                "Email": "alice@example.com",
            },
        },
    ],
    "offset": "itr23sEjsdfEr3282/recABCDEF12345678",
}
_PAGE_2 = {
    "records": [
        {
            "id": "recXYZ9876543210",
            "createdTime": "2023-01-16T11:30:00.000Z",
            "fields": {
                "Name": "Bob",
                "Status": "Churned",
                "Email": "bob@example.com",
            },
        },
    ],
    # No `offset` field → last page
}

# Documented List Bases response.
_BASES_BODY = {
    "bases": [
        {"id": "appLkNDICXNqxSDhG", "name": "Apartment Hunting", "permissionLevel": "create"},
        {"id": "appSW9R5uCNmRmfl6", "name": "Project Tracker", "permissionLevel": "edit"},
    ],
}


class AirtableManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `airtable` (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_AIRTABLE")
        # Synthetic PAT — split so the hygiene guard below doesn't flag THIS file.
        os.environ["RC_CONN_AIRTABLE"] = "pat" + "XXXXXXXXXXXXXXXXXXXX.test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_AIRTABLE", None)
        else:
            os.environ["RC_CONN_AIRTABLE"] = self._saved

    # ------------------------------------------------------------------
    # 1. Manifest loading and field mapping
    # ------------------------------------------------------------------

    def test_manifest_loaded_from_yaml_and_fields_map(self):
        """YAML loader populates every runtime-driving field correctly."""
        m = api.load_manifests()
        self.assertIn("airtable", m)
        a = m["airtable"]

        self.assertEqual(a.base_url, "https://api.airtable.com/v0")
        self.assertEqual(a.auth.strategy, "bearer")

        self.assertEqual(a.pagination.style, "cursor")
        self.assertEqual(a.pagination.cursor_field, "offset")
        self.assertEqual(a.pagination.cursor_param, "offset")
        self.assertEqual(a.pagination.has_more_field, "")
        self.assertEqual(a.pagination.items_field, "records")
        self.assertEqual(a.pagination.page_size, 100)

        # No rate-limit remaining header (Airtable doesn't publish one).
        self.assertEqual(a.rate_limit_remaining_header, "")

    # ------------------------------------------------------------------
    # 2. Cursor pagination stitches ≥2 pages
    # ------------------------------------------------------------------

    @responses_lib.activate
    def test_cursor_pagination_stitches_two_pages(self):
        """Page 1 returns an opaque offset; page 2 omits it — collect() gathers both pages."""
        responses_lib.add(responses_lib.GET, RECORDS_URL, json=_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, RECORDS_URL, json=_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["airtable"])
        result = c.collect(f"{BASE_ID}/{TABLE_ID}", query={"pageSize": 100})

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)

        # First record is from page 1, second from page 2.
        self.assertEqual(result["items"][0]["id"], "recABCDEF12345678")
        self.assertEqual(result["items"][1]["id"], "recXYZ9876543210")

        # Exactly two HTTP calls were made.
        self.assertEqual(len(responses_lib.calls), 2)

    # ------------------------------------------------------------------
    # 3. Credential rides every request (incl. the cursor-follow call)
    # ------------------------------------------------------------------

    @responses_lib.activate
    def test_bearer_credential_on_every_request(self):
        """The bearer token appears in the Authorization header on ALL pages, not just page 1."""
        responses_lib.add(responses_lib.GET, RECORDS_URL, json=_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, RECORDS_URL, json=_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["airtable"])
        c.collect(f"{BASE_ID}/{TABLE_ID}")

        token = "pat" + "XXXXXXXXXXXXXXXXXXXX.test"
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], f"Bearer {token}")

    # ------------------------------------------------------------------
    # 4. Cursor token is forwarded as the `offset` query param on page 2
    # ------------------------------------------------------------------

    @responses_lib.activate
    def test_offset_cursor_forwarded_on_second_request(self):
        """The opaque offset string from page 1 is sent as `offset=` on the second request."""
        responses_lib.add(responses_lib.GET, RECORDS_URL, json=_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, RECORDS_URL, json=_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["airtable"])
        c.collect(f"{BASE_ID}/{TABLE_ID}")

        # Second call must carry the opaque offset from page 1.
        self.assertEqual(len(responses_lib.calls), 2)
        second_url = responses_lib.calls[1].request.url
        self.assertIn("offset=", second_url)
        self.assertIn("itr23sEjsdfEr3282", second_url)

    # ------------------------------------------------------------------
    # 5. api.pick() prunes the nested fields object
    # ------------------------------------------------------------------

    @responses_lib.activate
    def test_pick_selects_support_fields(self):
        """pick() extracts the dotted paths an agent would send instead of the full record."""
        responses_lib.add(responses_lib.GET, RECORDS_URL, json=_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, RECORDS_URL, json=_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["airtable"])
        result = c.collect(f"{BASE_ID}/{TABLE_ID}")
        items = result["items"]

        picked = [api.pick(it, "id,fields.Name,fields.Status,fields.Email,createdTime") for it in items]
        self.assertEqual(picked[0]["id"], "recABCDEF12345678")
        self.assertEqual(picked[0]["fields.Name"], "Alice")
        self.assertEqual(picked[0]["fields.Status"], "Active")
        self.assertEqual(picked[0]["fields.Email"], "alice@example.com")
        self.assertEqual(picked[0]["createdTime"], "2023-01-15T10:00:00.000Z")

    # ------------------------------------------------------------------
    # 6. Non-records endpoint (List Bases, style=cursor, items_field=records)
    # ------------------------------------------------------------------

    @responses_lib.activate
    def test_list_bases_single_page_no_offset(self):
        """List Bases returns a `bases` key — since items_field=records (not bases), the agent
        picks the `bases` field directly from the raw body (single GET, no paginate)."""
        responses_lib.add(responses_lib.GET, BASES_URL, json=_BASES_BODY, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["airtable"])
        body = c.get("meta/bases")

        self.assertIn("bases", body)
        self.assertEqual(len(body["bases"]), 2)
        self.assertEqual(body["bases"][0]["id"], "appLkNDICXNqxSDhG")
        self.assertEqual(body["bases"][0]["name"], "Apartment Hunting")

    # ------------------------------------------------------------------
    # 7. CLI drive (python -m lib.api get airtable …)
    # ------------------------------------------------------------------

    @responses_lib.activate
    def test_cli_drives_airtable_paginate_and_pick(self):
        """The generic lib.api CLI can drive Airtable end-to-end with no bespoke code."""
        responses_lib.add(responses_lib.GET, RECORDS_URL, json=_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, RECORDS_URL, json=_PAGE_2, status=200)

        # Capture stdout so we can assert on the JSON output.
        import sys
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = api._main([
                "get", "airtable", f"{BASE_ID}/{TABLE_ID}",
                "--query", "pageSize=100",
                "--paginate",
                "--pick", "id,fields.Name,fields.Status",
            ])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        output = json.loads(captured.getvalue())
        self.assertIn("items", output)
        self.assertEqual(len(output["items"]), 2)

        # Both requests used the bearer token.
        token = "pat" + "XXXXXXXXXXXXXXXXXXXX.test"
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], f"Bearer {token}")


# ---------------------------------------------------------------------------
# Hygiene: no Airtable PAT prefix may leak into the connector directory.
# ---------------------------------------------------------------------------

class AirtableCassetteHygiene(unittest.TestCase):
    """CI guard: no real Airtable PAT prefix may land in committed connector files.

    Scoped to the connector dir only — this test file legitimately uses the split prefix form
    to avoid triggering itself.
    """

    # Airtable PAT prefix split so this file doesn't self-trigger.
    _TOKEN_PREFIXES = ("pat" ".",)  # real PATs look like pat<base62>.<hex> — the dot after pat<…> is key

    def test_no_pat_prefix_in_airtable_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "airtable"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
