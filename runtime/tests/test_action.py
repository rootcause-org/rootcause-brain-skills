"""Tests for lib.action hosted-action harness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import action, api  # noqa: E402


class ActionHarness(unittest.TestCase):
    def test_params_from_file_and_file_param(self):
        with tempfile.TemporaryDirectory() as td:
            attachment = Path(td) / "invoice.pdf"
            attachment.write_bytes(b"pdf")
            params_path = Path(td) / "params.json"
            params_path.write_text(
                json.dumps({
                    "name": "alice",
                    "attachment": {
                        "path": str(attachment),
                        "filename": "Invoice.pdf",
                        "mime_type": "application/pdf",
                        "size_bytes": 3,
                        "attachment_id": "att_1",
                        "sha256": "abc123",
                    },
                }),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"RC_ACTION_PARAMS": str(params_path)}, clear=False):
                p = action.params([])
            self.assertEqual(p["name"], "alice")
            f = p.file("attachment")
            self.assertEqual(f.filename, "Invoice.pdf")
            self.assertEqual(f.mime_type, "application/pdf")
            self.assertEqual(f.size_bytes, 3)
            self.assertEqual(f.attachment_id, "att_1")
            self.assertEqual(f.sha256, "abc123")
            self.assertEqual(f.read_bytes(), b"pdf")

    def test_params_from_argv_and_optional_param(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            p = action.params(["--params", '{"ok": true}'])
        self.assertTrue(p["ok"])
        self.assertIsNone(p.get("missing"))
        fallback = object()
        self.assertIs(p.get("missing", fallback), fallback)

    def test_missing_required_param_raises_action_error(self):
        p = action.Params({})
        with self.assertRaises(action.ActionError) as cm:
            p["missing"]
        self.assertIn("missing", str(cm.exception))

    def test_ok_fail_and_redaction_write_result(self):
        with tempfile.TemporaryDirectory() as td:
            result_path = Path(td) / "result.json"
            env = {
                "RC_ACTION_RESULT": str(result_path),
                "RC_ACTION_NOTION": "secret-token-123",
                "PODIO_WRITE_TOKEN": "podio-write-token-456",
                "APP_DSN": "postgres://secret-dsn",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(SystemExit) as cm:
                    action.ok("done", {"echo": "secret-token-123 postgres://secret-dsn podio-write-token-456"})
            self.assertEqual(cm.exception.code, 0)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["return_value"]["echo"], "[redacted] [redacted] [redacted]")

            with mock.patch.dict(os.environ, {"RC_ACTION_RESULT": str(result_path)}, clear=True):
                with self.assertRaises(SystemExit) as cm:
                    action.fail("not safe", {"reason": "duplicate"})
            self.assertEqual(cm.exception.code, 0)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["return_value"]["summary"], "not safe")
            self.assertFalse(result["return_value"]["ok"])

    def test_main_runner_writes_return_value(self):
        with tempfile.TemporaryDirectory() as td:
            params_path = Path(td) / "params.json"
            result_path = Path(td) / "result.json"
            params_path.write_text('{"id":"123"}', encoding="utf-8")

            def run(p):
                return {"summary": "done", "id": p["id"]}

            runner = action.main(run)
            with mock.patch.dict(os.environ, {
                "RC_ACTION_PARAMS": str(params_path),
                "RC_ACTION_RESULT": str(result_path),
            }, clear=True):
                with self.assertRaises(SystemExit) as cm:
                    runner()
            self.assertEqual(cm.exception.code, 0)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["return_value"], {"summary": "done", "id": "123"})

    def test_main_runner_wraps_handled_negative(self):
        with tempfile.TemporaryDirectory() as td:
            params_path = Path(td) / "params.json"
            result_path = Path(td) / "result.json"
            params_path.write_text("{}", encoding="utf-8")

            def run(_p):
                return {"ok": False, "summary": "duplicate", "id": "a1"}

            runner = action.main(run)
            with mock.patch.dict(os.environ, {
                "RC_ACTION_PARAMS": str(params_path),
                "RC_ACTION_RESULT": str(result_path),
            }, clear=True):
                with self.assertRaises(SystemExit) as cm:
                    runner()
            self.assertEqual(cm.exception.code, 0)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["return_value"], {"ok": False, "summary": "duplicate", "id": "a1"})

    def test_dry_run_precedence_and_tenant(self):
        with mock.patch.dict(os.environ, {"RC_ACTION_DRY_RUN": "1"}, clear=True):
            self.assertTrue(action.dry_run([]))
            self.assertTrue(action.dry_run(["--dry-run"]))
            self.assertFalse(action.dry_run(["--dry-run", "--commit"]))
        with mock.patch.dict(os.environ, {"RC_TENANT_ID": "t1", "RC_TENANT_SLUG": "acme"}, clear=True):
            self.assertEqual(action.tenant(), ("t1", "acme"))
            self.assertEqual(action.require_tenant(), ("t1", "acme"))
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(action.ActionError):
                action.require_tenant()

    def test_uncaught_exception_excepthook_writes_result(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "boom.py"
            result_path = Path(td) / "result.json"
            script.write_text(
                "from lib import action\n"
                "action.params(['--params', '{}'])\n"
                "raise ValueError('boom')\n",
                encoding="utf-8",
            )
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]), RC_ACTION_RESULT=str(result_path))
            proc = subprocess.run([sys.executable, str(script)], env=env, text=True, capture_output=True, check=False)
            self.assertNotEqual(proc.returncode, 0)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["class"], "ValueError")
            self.assertIn("backtrace", result["error"])


class ActionClientResolution(unittest.TestCase):
    def test_env_var_golden_table(self):
        cases = {
            "airtable.write": "RC_ACTION_AIRTABLE",
            "notion.write": "RC_ACTION_NOTION",
            "googledrive.write": "RC_ACTION_GOOGLEDRIVE",
            "hubspot-eu.write": "RC_ACTION_HUBSPOT_EU",
            "foo_bar.write": "RC_ACTION_FOO_BAR",
            "zendesk.v2.write": "RC_ACTION_ZENDESK",
        }
        for capability, env in cases.items():
            self.assertEqual(action._env_var(capability), env)

    def test_client_resolves_only_action_env(self):
        manifest = api.Manifest(key="notion", base_url="https://api.notion.test")
        with mock.patch.dict(os.environ, {"RC_CONN_NOTION": "read-token", "RC_ACTION_PARAMS": "/tmp/p"}, clear=True):
            with self.assertRaises(action.ActionError) as cm:
                action.client("notion.write", manifest=manifest)
        self.assertIn("notion.write", str(cm.exception))

        with mock.patch.dict(os.environ, {"RC_ACTION_NOTION": "write-token"}, clear=True):
            c = action.client("notion.write", manifest=manifest)
        self.assertTrue(c.allow_writes)
        self.assertEqual(c.credential, "write-token")

    def test_missing_capability_messages(self):
        manifest = api.Manifest(key="notion", base_url="https://api.notion.test")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(action.ActionError) as cm:
                action.client("notion.write", manifest=manifest)
        self.assertIn("not running inside an action execution", str(cm.exception))

        with mock.patch.dict(os.environ, {
            "RC_ACTION_PARAMS": "/tmp/params.json",
            "RC_ACTION_CONNECTIONS": "googledrive.write",
        }, clear=True):
            with self.assertRaises(action.ActionError) as cm:
                action.client("notion.write", manifest=manifest)
        self.assertIn("not declared", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
