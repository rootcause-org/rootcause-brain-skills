"""Fixture test for the manifest-ONLY Typeform integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. Bodies mirror Typeform's documented
example payloads (typeform.com/developers), trimmed to support-relevant fields.

Pagination coverage:
- /forms uses 1-based page-number paging (page/page_size) with an `items` envelope — pages stitched.
- /forms/{id}/responses is single-page (page_size≤1000 covers most support queries).

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_typeform_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as resp_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API = "https://api.typeform.com"
FORMS_URL = f"{API}/forms"
RESPONSES_URL = f"{API}/forms/abc123/responses"

# --- Documented example payloads (trimmed) ---
# Page 1: one form, page_size=1 forces a second page.
_FORMS_PAGE_1 = {
    "total_items": 2,
    "page_count": 2,
    "items": [
        {
            "id": "abc123",
            "title": "Customer Feedback",
            "last_updated_at": "2024-01-15T10:00:00Z",
            "_links": {"display": "https://youraccountname.typeform.com/to/abc123"},
        }
    ],
}
_FORMS_PAGE_2 = {
    "total_items": 2,
    "page_count": 2,
    "items": [
        {
            "id": "def456",
            "title": "Support Request",
            "last_updated_at": "2024-02-20T08:30:00Z",
            "_links": {"display": "https://youraccountname.typeform.com/to/def456"},
        }
    ],
}
_FORMS_EMPTY = {
    "total_items": 2,
    "page_count": 2,
    "items": [],  # signals end-of-pages to offset paginator
}

# Single-page responses (Typeform's documented response envelope).
_RESPONSES_PAGE = {
    "total_items": 2,
    "page_count": 1,
    "items": [
        {
            "response_id": "resp_aaa",
            "submitted_at": "2024-03-01T14:22:00Z",
            "token": "token_aaa",
            "hidden": {"user_id": "usr_111"},
            "answers": [
                {
                    "field": {"id": "field_01", "type": "short_text", "ref": "name"},
                    "type": "text",
                    "text": "Alice",
                }
            ],
        },
        {
            "response_id": "resp_bbb",
            "submitted_at": "2024-03-02T09:05:00Z",
            "token": "token_bbb",
            "hidden": {"user_id": "usr_222"},
            "answers": [
                {
                    "field": {"id": "field_01", "type": "short_text", "ref": "name"},
                    "type": "text",
                    "text": "Bob",
                }
            ],
        },
    ],
}


class TypeformManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `typeform`.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_TYPEFORM")
        os.environ["RC_CONN_TYPEFORM"] = "tfp_" + "test_token_fixture_abc123"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_TYPEFORM", None)
        else:
            os.environ["RC_CONN_TYPEFORM"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        """YAML loads correctly and maps every declared field."""
        m = api.load_manifests()
        self.assertIn("typeform", m)
        tf = m["typeform"]
        self.assertEqual(tf.base_url, "https://api.typeform.com")
        self.assertEqual(tf.auth.strategy, "bearer")
        self.assertEqual(tf.pagination.style, "page")
        self.assertEqual(tf.pagination.page_param, "page")
        self.assertEqual(tf.pagination.page_start, 1)  # 1-based
        self.assertEqual(tf.pagination.limit_param, "page_size")
        self.assertEqual(tf.pagination.items_field, "items")
        self.assertEqual(tf.pagination.page_size, 200)
        self.assertEqual(tf.rate_limit_remaining_header, "")
        # No required default_headers for Typeform.
        self.assertNotIn("X-Typeform-Version", tf.default_headers)

    @resp_lib.activate
    def test_page_number_pagination_stitches_two_pages(self):
        """Two pages of /forms are stitched by 1-based page NUMBER; bearer rides every request."""
        # page_size overridden to 1 so each fixture page is "full" → page=1 (full), page=2 (full),
        # page=3 (empty) → stop. The page NUMBER advances 1→2→3, NOT an item-count offset.
        resp_lib.add(resp_lib.GET, FORMS_URL, json=_FORMS_PAGE_1, status=200)
        resp_lib.add(resp_lib.GET, FORMS_URL, json=_FORMS_PAGE_2, status=200)
        resp_lib.add(resp_lib.GET, FORMS_URL, json=_FORMS_EMPTY, status=200)

        api.load_manifests()
        tf = api.MANIFESTS["typeform"]
        import dataclasses
        mani_small = dataclasses.replace(
            tf,
            pagination=dataclasses.replace(tf.pagination, page_size=1),
        )
        c = api.Client(manifest=mani_small, credential="tfp_" + "test_token_fixture_abc123")
        result = c.collect(FORMS_URL)

        self.assertFalse(result["incomplete"], result["reason"])
        ids = [it["id"] for it in result["items"]]
        self.assertIn("abc123", ids)
        self.assertIn("def456", ids)

        # Page NUMBER advances 1 → 2 → 3 (not 0 → 1 → 2 offsets).
        self.assertEqual(len(resp_lib.calls), 3)
        self.assertIn("page=1", resp_lib.calls[0].request.url)
        self.assertIn("page=2", resp_lib.calls[1].request.url)
        self.assertIn("page=3", resp_lib.calls[2].request.url)

        # Bearer rode EVERY request (all page fetches).
        auth_headers = [call.request.headers.get("Authorization", "") for call in resp_lib.calls]
        self.assertTrue(all(h.startswith("Bearer ") for h in auth_headers))
        # Credential value rides through unchanged.
        expected_cred = "Bearer tfp_" + "test_token_fixture_abc123"
        self.assertEqual(auth_headers[0], expected_cred)
        self.assertEqual(auth_headers[1], expected_cred)

    @resp_lib.activate
    def test_single_page_responses_and_pick(self):
        """/responses single-page fetch; pick selects support-relevant fields."""
        resp_lib.add(resp_lib.GET, RESPONSES_URL, json=_RESPONSES_PAGE, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["typeform"])
        body = c.get("/forms/abc123/responses", query={"page_size": 100})

        self.assertEqual(body["total_items"], 2)
        items = body["items"]
        self.assertEqual(len(items), 2)

        # pick pre-selects the support-relevant fields.
        picked = api.pick(items[0], "response_id,submitted_at,token,answers.*.text")
        self.assertEqual(picked["response_id"], "resp_aaa")
        self.assertEqual(picked["submitted_at"], "2024-03-01T14:22:00Z")
        self.assertEqual(picked["token"], "token_aaa")
        self.assertEqual(picked["answers.*.text"], ["Alice"])

        # Bearer present on the request.
        auth = resp_lib.calls[0].request.headers.get("Authorization", "")
        self.assertTrue(auth.startswith("Bearer "), auth)

    @resp_lib.activate
    def test_cli_drives_typeform_get(self):
        """CLI drives a simple GET through the manifest with bearer auth."""
        resp_lib.add(resp_lib.GET, FORMS_URL, json=_FORMS_PAGE_1, status=200)
        rc = api._main(["get", "typeform", "/forms", "--pick", "items.*.id,items.*.title"])
        self.assertEqual(rc, 0)
        self.assertTrue(resp_lib.calls[0].request.url.startswith(FORMS_URL))
        auth = resp_lib.calls[0].request.headers.get("Authorization", "")
        self.assertTrue(auth.startswith("Bearer "))

    @resp_lib.activate
    def test_cli_paginate_forms(self):
        """CLI --paginate auto-pages /forms via offset style."""
        resp_lib.add(resp_lib.GET, FORMS_URL, json=_FORMS_PAGE_1, status=200)
        resp_lib.add(resp_lib.GET, FORMS_URL, json=_FORMS_EMPTY, status=200)
        rc = api._main([
            "get", "typeform", "/forms",
            "--paginate", "--pick", "id,title",
        ])
        self.assertEqual(rc, 0)
        # At least one paginated request was made.
        self.assertGreaterEqual(len(resp_lib.calls), 1)


class TypeformTokenHygiene(unittest.TestCase):
    """CI guard: no real Typeform PAT prefix may land in committed connector files.

    Scoped to the connector dir only — this test file itself names the prefix split
    across string concatenation to avoid triggering its own guard.
    """

    # Typeform PAT prefix: tfp_ (split so the guard can't flag this source file).
    _TOKEN_PREFIXES = ("tfp" "_",)

    def test_no_token_prefixes_in_typeform_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "typeform"
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
