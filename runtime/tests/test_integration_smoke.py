"""Tests for the local connector live-smoke helper.

No real provider calls: mocked HTTP and subprocesses verify env-only token injection, compact output,
and connector CLI invocation without putting secrets in argv.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api, integration_smoke  # noqa: E402


class EnvInjection(unittest.TestCase):
    def test_prepare_token_env_copies_key_specific_local_override(self):
        with mock.patch.dict(
            os.environ,
            {"RC_INTEGRATION_SMOKE_GITHUB": "ghp_live_test"},
            clear=True,
        ):
            target, redact_envs = integration_smoke.prepare_token_env("github")
            self.assertEqual(target, "RC_CONN_GITHUB")
            self.assertEqual(os.environ["RC_CONN_GITHUB"], "ghp_live_test")
            self.assertIn("RC_INTEGRATION_SMOKE_GITHUB", redact_envs)

    def test_existing_rc_conn_wins_over_generic_override(self):
        with mock.patch.dict(
            os.environ,
            {"RC_CONN_GITHUB": "canonical", "RC_INTEGRATION_SMOKE_TOKEN": "generic"},
            clear=True,
        ):
            target, _ = integration_smoke.prepare_token_env("github")
            self.assertEqual(target, "RC_CONN_GITHUB")
            self.assertEqual(os.environ["RC_CONN_GITHUB"], "canonical")


class SmokeCli(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        api.register(
            api.Manifest(
                key="demo",
                base_url="https://api.demo.test/v1",
                auth=api.Auth(strategy="bearer"),
                pagination=api.Pagination(style="none"),
            )
        )

    @responses.activate
    def test_api_checks_use_env_token_and_do_not_print_secret(self):
        responses.add(responses.GET, "https://api.demo.test/v1/me", json={"id": "acct_1", "name": "Demo"})
        responses.add(
            responses.GET,
            "https://api.demo.test/v1/things",
            json=[{"id": "thing_1", "name": "First", "echo": "super-secret-token"}],
        )
        responses.add(responses.GET, "https://api.demo.test/v1/things/thing_1", json={"id": "thing_1", "state": "ok"})

        with mock.patch.dict(os.environ, {"RC_INTEGRATION_SMOKE_DEMO": "super-secret-token"}, clear=True):
            with mock.patch("builtins.print") as printed:
                rc = integration_smoke._main(
                    [
                        "demo",
                        "--identity-path",
                        "/me",
                        "--identity-pick",
                        "id",
                        "--list-path",
                        "/things",
                        "--paginate-list",
                        "--detail-path-template",
                        "/things/{id}",
                        "--detail-id",
                        "thing_1",
                        "--detail-pick",
                        "id,state",
                    ]
                )

        self.assertEqual(rc, 0)
        for call in responses.calls:
            self.assertEqual(call.request.headers["Authorization"], "Bearer super-secret-token")
        payload = json.loads(printed.call_args.args[0])
        self.assertTrue(payload["ok"])
        self.assertEqual([c["name"] for c in payload["checks"]], ["identity", "list", "detail"])
        self.assertNotIn("super-secret-token", printed.call_args.args[0])
        self.assertIn("[redacted]", printed.call_args.args[0])

    @responses.activate
    def test_api_error_is_reported_without_traceback_or_secret(self):
        responses.add(
            responses.GET,
            "https://api.demo.test/v1/me",
            json={"error": "bad token super-secret-token"},
            status=401,
        )
        with mock.patch.dict(os.environ, {"RC_INTEGRATION_SMOKE_DEMO": "super-secret-token"}, clear=True):
            with mock.patch("builtins.print") as printed:
                rc = integration_smoke._main(["demo", "--identity-path", "/me"])

        self.assertEqual(rc, 1)
        payload = json.loads(printed.call_args.args[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["checks"][0]["detail"]["error_type"], "ApiError")
        self.assertNotIn("super-secret-token", printed.call_args.args[0])
        self.assertIn("[redacted]", printed.call_args.args[0])

    def test_list_max_items_requires_paginated_list(self):
        with mock.patch.dict(os.environ, {"RC_INTEGRATION_SMOKE_DEMO": "tok"}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                integration_smoke._main(["demo", "--list-path", "/things", "--list-max-items", "5"])
        self.assertIn("--list-max-items requires --paginate-list", str(cm.exception))

    def test_connector_command_inherits_env_without_secret_argv(self):
        proc = subprocess_result(stdout="using top-secret\n# ok\n")
        with mock.patch.dict(os.environ, {"RC_INTEGRATION_SMOKE_DEMO": "top-secret"}, clear=True):
            integration_smoke.prepare_token_env("demo")
            with mock.patch("subprocess.run", return_value=proc) as run:
                result = integration_smoke.run_connector_cli(
                    "demo",
                    "doctor --live",
                    timeout=7,
                    redact_envs=["RC_CONN_DEMO", "RC_INTEGRATION_SMOKE_DEMO"],
                )
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], [sys.executable, "-m", "lib.connectors.demo"])
        self.assertNotIn("top-secret", argv)
        self.assertEqual(run.call_args.kwargs["env"]["RC_CONN_DEMO"], "top-secret")
        self.assertEqual(result.detail["stdout_lines"][0], "using [redacted]")


def subprocess_result(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    class Result:
        pass

    result = Result()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


if __name__ == "__main__":
    unittest.main()
