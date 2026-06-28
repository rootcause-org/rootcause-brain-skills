"""Tests for lib.api's YAML manifest loader — what makes "a manifest row IS the integration" true at
runtime. No network: the one end-to-end CLI drive mocks HTTP with `responses`.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_manifest_loader.py -q
"""

import sys
import tempfile
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402


class LoaderDiscovery(unittest.TestCase):
    def setUp(self):
        # Start from a clean registry each test so an explicit register() elsewhere can't leak in.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_discovers_stripe_and_sentry_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("stripe", m)
        self.assertIn("sentry", m)

    def test_sentry_fields_mapped(self):
        m = api.load_manifests()
        s = m["sentry"]
        self.assertEqual(s.base_url, "https://sentry.io/api/0")
        self.assertEqual(s.auth.strategy, "bearer")
        self.assertEqual(s.pagination.style, "link")
        self.assertEqual(s.rate_limit_remaining_header, "X-Sentry-Rate-Limit-Remaining")

    def test_stripe_fields_mapped(self):
        m = api.load_manifests()
        s = m["stripe"]
        self.assertEqual(s.base_url, "https://api.stripe.com/v1")
        self.assertEqual(s.auth.strategy, "bearer")
        self.assertEqual(s.pagination.style, "cursor")
        self.assertEqual(s.pagination.cursor_param, "starting_after")
        self.assertEqual(s.pagination.has_more_field, "has_more")
        self.assertEqual(s.pagination.items_field, "data")
        self.assertEqual(s.pagination.page_size, 100)

    def test_idempotent_reload(self):
        api.load_manifests()
        first = dict(api.MANIFESTS)
        api.load_manifests()
        self.assertEqual(set(api.MANIFESTS), set(first))

    def test_explicit_register_wins_over_yaml(self):
        # An explicit register() for a YAML key is the source of truth and is never clobbered by a
        # subsequent load (the manifest-vs-Python precedence rule).
        custom = api.Manifest(key="stripe", base_url="https://override.test/v1", auth=api.Auth(strategy="none"))
        api.register(custom)
        api.load_manifests()
        self.assertIs(api.MANIFESTS["stripe"], custom)
        self.assertEqual(api.MANIFESTS["stripe"].base_url, "https://override.test/v1")
        # ...but a YAML-only key still loaded.
        self.assertIn("sentry", api.MANIFESTS)


class MalformedManifest(unittest.TestCase):
    def test_malformed_yaml_raises_naming_file(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "manifest.yaml"
            bad.write_text("key: oops\nauth: [this, is, not, a, mapping]\n", encoding="utf-8")
            with self.assertRaises(api.ManifestError) as cm:
                api._parse_manifest_file(bad)
            self.assertIn(str(bad), str(cm.exception))

    def test_missing_key_raises_naming_file(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "manifest.yaml"
            bad.write_text("base_url: https://x.test\n", encoding="utf-8")
            with self.assertRaises(api.ManifestError) as cm:
                api._parse_manifest_file(bad)
            self.assertIn(str(bad), str(cm.exception))

    def test_not_a_mapping_raises(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "manifest.yaml"
            bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
            with self.assertRaises(api.ManifestError):
                api._parse_manifest_file(bad)


class CliDrivesManifestOnly(unittest.TestCase):
    """Prove a manifest-only integration is drivable end-to-end through the CLI: no per-key Python."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        import os

        self._saved = os.environ.get("RC_CONN_SENTRY")
        os.environ["RC_CONN_SENTRY"] = "tok_sentry_test"

    def tearDown(self):
        import os

        if self._saved is None:
            os.environ.pop("RC_CONN_SENTRY", None)
        else:
            os.environ["RC_CONN_SENTRY"] = self._saved

    @responses.activate
    def test_cli_get_sentry_hits_real_base_with_bearer(self):
        responses.add(
            responses.GET,
            "https://sentry.io/api/0/issues/",
            json=[{"id": "1", "shortId": "ABC-1"}],
            status=200,
        )
        rc = api._main(["get", "sentry", "issues/"])
        self.assertEqual(rc, 0)
        req = responses.calls[0].request
        self.assertTrue(req.url.startswith("https://sentry.io/api/0/issues/"))
        self.assertEqual(req.headers["Authorization"], "Bearer tok_sentry_test")


if __name__ == "__main__":
    unittest.main()
