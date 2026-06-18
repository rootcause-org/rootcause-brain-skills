"""Telemetry is a no-op without a key, never raises when disabled, and scrubs credential keys.

These run offline (no PostHog network): the disabled path short-circuits before any send, and the
scrub test exercises pure logic. Run with the rest of the suite:

    cd runtime && uv run --no-project python -m unittest discover -s tests
"""

import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import telemetry  # noqa: E402


class DisabledIsNoOp(unittest.TestCase):
    def setUp(self):
        # Guarantee the disabled path regardless of the host env the suite runs under.
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("POSTHOG_PROJECT_API_KEY", None)

    def tearDown(self):
        self._env.stop()

    def test_lib_imports_without_key(self):
        import lib  # noqa: PLC0415

        self.assertIn("telemetry", lib.__all__)
        self.assertIs(lib.telemetry, telemetry)

    def test_install_capture_flush_do_not_raise(self):
        telemetry.install()  # idempotent + no-op without a key
        telemetry.capture_exception(Exception("x"))
        telemetry.capture_exception()  # no active exception → still safe
        telemetry.flush()


class Scrub(unittest.TestCase):
    def _event(self, props):
        return types.SimpleNamespace(properties=props)

    def test_redacts_credential_like_keys(self):
        props = {
            "api_key": "sk-live-123",
            "Authorization": "Bearer abc",
            "PG_TOKEN": "t",
            "db_password": "p",
            "run_id": "r1",
            "model": "x",
        }
        telemetry._scrub(self._event(props))
        self.assertEqual(props["api_key"], "[redacted]")
        self.assertEqual(props["Authorization"], "[redacted]")
        self.assertEqual(props["PG_TOKEN"], "[redacted]")
        self.assertEqual(props["db_password"], "[redacted]")
        self.assertEqual(props["run_id"], "r1")  # benign keys untouched
        self.assertEqual(props["model"], "x")

    def test_no_properties_is_safe(self):
        ev = types.SimpleNamespace()  # event without .properties
        self.assertIs(telemetry._scrub(ev), ev)


class RunContext(unittest.TestCase):
    def test_context_skips_absent_and_sets_group(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ["RC_PROJECT_ID"] = "momentum-tools"
            os.environ["RC_RUN_ID"] = "abc123"
            distinct_id, props, groups = telemetry._run_context()
        self.assertEqual(distinct_id, "momentum-tools")
        self.assertEqual(groups, {"project": "momentum-tools"})
        self.assertEqual(props["component"], "workspace")
        self.assertEqual(props["$exception_level"], "error")
        self.assertEqual(props["run_id"], "abc123")
        self.assertNotIn("session_id", props)  # absent env skipped


if __name__ == "__main__":
    unittest.main()
