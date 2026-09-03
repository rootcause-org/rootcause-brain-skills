"""Tests for the hosted action preflight helpers."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.action import preflight  # noqa: E402


class PreflightHelpers(unittest.TestCase):
    def test_params_prefers_environment_and_requires_object(self):
        with mock.patch.dict(os.environ, {"PREFLIGHT_PARAMS": '{"id": "a1"}'}, clear=True):
            self.assertEqual(preflight.params(["--params", '{"id": "ignored"}']), {"id": "a1"})

        with mock.patch.dict(os.environ, {"PREFLIGHT_PARAMS": "[]"}, clear=True):
            with self.assertRaisesRegex(ValueError, "JSON object"):
                preflight.params([])

    def test_params_supports_local_argv_and_fails_when_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(preflight.params(["--params", '{"count": 2}']), {"count": 2})
            with self.assertRaisesRegex(SystemExit, "no params"):
                preflight.params([])

    def test_result_omits_unsupplied_optional_fields(self):
        self.assertEqual(
            preflight.result(True, "safe"),
            {"ok": True, "summary": "safe", "reason": ""},
        )

    def test_result_includes_resource_url_only_when_non_empty(self):
        self.assertNotIn("resource_url", preflight.result(True, "safe", resource_url=""))
        self.assertEqual(
            preflight.result(True, "Maité · afwezig di 19/09", resource_url="https://admin.example/t/x/1"),
            {
                "ok": True,
                "summary": "Maité · afwezig di 19/09",
                "reason": "",
                "resource_url": "https://admin.example/t/x/1",
            },
        )

    def test_result_preserves_empty_observed_and_failure_class(self):
        self.assertEqual(
            preflight.result(
                False,
                "unavailable",
                "database offline",
                observed={},
                failure_class="infrastructure",
            ),
            {
                "ok": False,
                "summary": "unavailable",
                "reason": "database offline",
                "observed": {},
                "class": "infrastructure",
            },
        )


if __name__ == "__main__":
    unittest.main()
