"""Fixture test for the manifest-ONLY Google Docs integration.

No live creds, no network: HTTP is mocked with `responses`. The fixture body mirrors the
Docs API's documents.get response shape, trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --no-project \
        pytest tests/test_googledocs_connector.py -q
"""

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import api  # noqa: E402

API = "https://docs.googleapis.com/v1"
DOCUMENT_ID = "1aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdef"

_DOCUMENT = {
    "documentId": DOCUMENT_ID,
    "title": "Support Notes",
    "revisionId": "AAA123",
    "body": {
        "content": [
            {
                "paragraph": {
                    "elements": [
                        {"textRun": {"content": "Customer reported missing invoice.\n"}},
                        {"textRun": {"content": "Follow up after billing export.\n"}},
                    ]
                }
            }
        ]
    },
}


class GoogleDocsManifestOnly(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GOOGLEDOCS")
        os.environ["RC_CONN_GOOGLEDOCS"] = "ya29." + "docs_test_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GOOGLEDOCS", None)
        else:
            os.environ["RC_CONN_GOOGLEDOCS"] = self._saved
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loaded_from_yaml(self):
        manifests = api.load_manifests()
        self.assertIn("googledocs", manifests)
        gd = manifests["googledocs"]
        self.assertEqual(gd.base_url, API)
        self.assertEqual(gd.auth.strategy, "bearer")
        self.assertEqual(gd.pagination.style, "none")
        self.assertEqual(gd.rate_limit_remaining_header, "")

    @responses.activate
    def test_get_document(self):
        url = f"{API}/documents/{DOCUMENT_ID}"
        responses.add(responses.GET, url, json=_DOCUMENT, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["googledocs"])
        body = c.get(f"documents/{DOCUMENT_ID}")

        self.assertEqual(body["documentId"], DOCUMENT_ID)
        self.assertEqual(body["title"], "Support Notes")
        self.assertEqual(len(responses.calls), 1)
        self.assertEqual(
            responses.calls[0].request.headers["Authorization"],
            "Bearer ya29.docs_test_token",
        )

    @responses.activate
    def test_pick_document_text_runs(self):
        url = f"{API}/documents/{DOCUMENT_ID}"
        responses.add(responses.GET, url, json=_DOCUMENT, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["googledocs"])
        body = c.get(f"documents/{DOCUMENT_ID}")
        picked = api.pick(body, "title,body.content.*.paragraph.elements.*.textRun.content")

        self.assertEqual(picked["title"], "Support Notes")
        self.assertEqual(
            picked["body.content.*.paragraph.elements.*.textRun.content"],
            [["Customer reported missing invoice.\n", "Follow up after billing export.\n"]],
        )

    @responses.activate
    def test_cli_drives_googledocs_get(self):
        url = f"{API}/documents/{DOCUMENT_ID}"
        responses.add(responses.GET, url, json=_DOCUMENT, status=200)

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            rc = api._main([
                "get",
                "googledocs",
                f"documents/{DOCUMENT_ID}",
                "--pick",
                "documentId,title",
            ])

        self.assertEqual(rc, 0)
        out = json.loads(captured.getvalue())
        self.assertEqual(out["documentId"], DOCUMENT_ID)
        self.assertEqual(out["title"], "Support Notes")


class GoogleDocsCassetteHygiene(unittest.TestCase):
    _TOKEN_PREFIXES = ("ya29" ".",)

    def test_no_token_prefixes_in_googledocs_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "googledocs"
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
