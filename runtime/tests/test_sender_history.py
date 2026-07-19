"""CLI contract + degradation tests for lib.sender_history.

The module is a thin, credential-free client over the per-run broker at rc-broker.internal. These
lock down what the AGENT observes at the shell: the invocation surfaced in the prompt is plain
`python -m lib.sender_history` (never `uv run`, which needs a writable uv cache the run container
doesn't reliably grant), and every reachable failure prints a SELF-EXPLANATORY line — never a bare
Python traceback or a naked exit code that teaches the model nothing.

No network: the broker call is monkeypatched. Runs on a bare host with

    cd runtime && uv run --no-project python -m unittest discover -s tests
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import sender_history  # noqa: E402


class Usage(unittest.TestCase):
    """No/empty args → exit 2 with the usage block; the usage text names the plain `python -m`
    invocation, matching the prompt affordance and every other lib.* CLI."""

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = sender_history._main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_no_args_prints_usage_exit_2(self):
        code, _, err = self._run([])
        self.assertEqual(code, 2)
        self.assertIn("usage: python -m lib.sender_history", err)

    def test_usage_string_uses_plain_python_not_uv(self):
        # Regression guard for E2: the container installs `lib` --system, so `python -m` needs no uv
        # cache; `uv run` intermittently died on `/srv/uv-cache/CACHEDIR.TAG: Permission denied`.
        self.assertIn("python -m lib.sender_history", sender_history._USAGE)
        self.assertNotIn("uv run", sender_history._USAGE)

    def test_get_without_ref_prints_usage_exit_2(self):
        code, _, err = self._run(["get"])
        self.assertEqual(code, 2)
        self.assertIn("usage: python -m lib.sender_history get <REF>", err)

    def test_unknown_command_exit_2(self):
        code, _, err = self._run(["frobnicate"])
        self.assertEqual(code, 2)
        self.assertIn("unknown command", err)


class BrokerRefusal(unittest.TestCase):
    """A reachable broker that says no (404 out-of-scope ref, 429 cap) → exit 1 with the server's
    message, never a silent empty."""

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = sender_history._main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_404_surfaces_status_and_body(self):
        err = sender_history.BrokerError(404, "unknown ref for this run")
        with mock.patch.object(sender_history, "_broker_get", side_effect=err):
            code, _, stderr = self._run(["get", "deadbeef"])
        self.assertEqual(code, 1)
        self.assertIn("404", stderr)
        self.assertIn("unknown ref for this run", stderr)

    def test_429_cap_exhausted(self):
        err = sender_history.BrokerError(429, "hydration cap exhausted for this run")
        with mock.patch.object(sender_history, "_broker_get", side_effect=err):
            code, _, stderr = self._run(["get", "abc123"])
        self.assertEqual(code, 1)
        self.assertIn("429", stderr)
        self.assertIn("cap exhausted", stderr)


class BrokerUnreachable(unittest.TestCase):
    """No HTTP status at all (transport failure: no proxy in scope, connection refused, timeout) →
    exit 1 with a self-explanatory 'where this works' pointer, NOT a raw traceback. This is the
    degradation the E2 friction demanded: exit 64/usage-noise/tracebacks teach the model nothing."""

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = sender_history._main(argv)
        return code, out.getvalue(), err.getvalue()

    def _assert_pointer(self, stderr):
        self.assertIn("could not reach the per-run history broker", stderr)
        self.assertIn("main agent loop", stderr)
        self.assertIn("INDEX", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_get_connection_error_degrades(self):
        # Builtin ConnectionError IS an OSError, as is requests' RequestException — the module's catch.
        with mock.patch.object(sender_history, "_broker_get", side_effect=ConnectionError("proxy refused")):
            code, _, stderr = self._run(["get", "abc123"])
        self.assertEqual(code, 1)
        self.assertIn("ConnectionError", stderr)
        self._assert_pointer(stderr)

    def test_list_timeout_degrades(self):
        with mock.patch.object(sender_history, "_broker_get", side_effect=TimeoutError("read timed out")):
            code, _, stderr = self._run(["list"])
        self.assertEqual(code, 1)
        self._assert_pointer(stderr)


class HappyPath(unittest.TestCase):
    """A 2xx returns the raw markdown body; `get` also saves it under /tmp/history so re-reads don't
    burn a capped hydration."""

    def test_list_prints_body(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sender_history, "_broker_get", return_value="=== index ===\n- Re: x"):
            with redirect_stdout(out), redirect_stderr(err):
                code = sender_history._main(["list"])
        self.assertEqual(code, 0)
        self.assertIn("=== index ===", out.getvalue())

    def test_get_saves_and_prints(self):
        body = "# Prior thread\nhello"
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sender_history, "_HISTORY_DIR", tmp):
                with mock.patch.object(sender_history, "_broker_get", return_value=body):
                    with redirect_stdout(out):
                        code = sender_history._main(["get", "19f1e22fc3c6511c"])
            self.assertEqual(code, 0)
            printed = out.getvalue()
            saved_path = Path(printed.splitlines()[0])
            self.assertTrue(saved_path.exists())
            self.assertEqual(saved_path.read_text(encoding="utf-8"), body)
        self.assertIn("hello", printed)

    def test_get_save_failure_still_prints_body(self):
        # The hydration is host-capped: a local save failure must neither discard the fetched body
        # nor be mislabeled as a broker failure.
        body = "# Prior thread\nhello"
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sender_history, "_broker_get", return_value=body):
            with mock.patch.object(sender_history, "_save_thread", side_effect=PermissionError("denied")):
                with redirect_stdout(out), redirect_stderr(err):
                    code = sender_history._main(["get", "abc123"])
        self.assertEqual(code, 0)
        self.assertIn("hello", out.getvalue())
        self.assertIn("retrieved OK but could not save", err.getvalue())
        self.assertNotIn("could not reach the per-run history broker", err.getvalue())


if __name__ == "__main__":
    unittest.main()
