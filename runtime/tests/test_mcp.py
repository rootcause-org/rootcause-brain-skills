"""Unit tests for lib.mcp — the remote MCP client. No network: every HTTP call is mocked with
`responses`. Locks down the JSON-RPC envelope handling, SSE parsing, error normalization, and the
env-var contract (RC_CONN_<KEY>_MCP bearer + RC_CONN_<KEY>_MCP_URL endpoint).

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_mcp.py -q
"""

import os
import random
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import mcp  # noqa: E402

URL = "https://mcp.example.test/rpc"


class EnvContract(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("RC_CONN_DEMO_MCP", "RC_CONN_DEMO_MCP_URL")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_env_var_names(self):
        self.assertEqual(mcp._bearer_env("demo"), "RC_CONN_DEMO_MCP")
        self.assertEqual(mcp._url_env("demo"), "RC_CONN_DEMO_MCP_URL")

    def test_client_reads_bearer_and_url_from_env(self):
        os.environ["RC_CONN_DEMO_MCP"] = "secret-bearer"
        os.environ["RC_CONN_DEMO_MCP_URL"] = URL
        c = mcp.client("demo")
        self.assertEqual(c.url, URL)
        self.assertEqual(c.bearer, "secret-bearer")

    def test_missing_url_raises(self):
        os.environ.pop("RC_CONN_DEMO_MCP_URL", None)
        with self.assertRaises(mcp.McpError):
            mcp.resolve_endpoint("demo")


def _client(**kw):
    return mcp.Client(key="demo", url=URL, bearer="secret-bearer", _rng=random.Random(1), _sleeper=lambda s: None, **kw)


class ToolsList(unittest.TestCase):
    @responses.activate
    def test_parses_tool_array(self):
        responses.add(
            responses.POST, URL,
            json={"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "search"}, {"name": "fetch"}]}},
        )
        c = _client()
        result = c.rpc("tools/list")
        names = [t["name"] for t in result["tools"]]
        self.assertEqual(names, ["search", "fetch"])
        # Bearer reached the wire; method/jsonrpc envelope is correct.
        req = responses.calls[0].request
        self.assertEqual(req.headers["Authorization"], "Bearer secret-bearer")
        import json
        body = json.loads(req.body)
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertEqual(body["method"], "tools/list")

    @responses.activate
    def test_tools_helper_returns_list(self):
        os.environ["RC_CONN_DEMO_MCP"] = "secret-bearer"
        os.environ["RC_CONN_DEMO_MCP_URL"] = URL
        try:
            responses.add(
                responses.POST, URL,
                json={"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "x"}]}},
            )
            self.assertEqual(mcp.tools("demo"), [{"name": "x"}])
        finally:
            os.environ.pop("RC_CONN_DEMO_MCP", None)
            os.environ.pop("RC_CONN_DEMO_MCP_URL", None)


class CallResult(unittest.TestCase):
    @responses.activate
    def test_call_returns_result(self):
        responses.add(
            responses.POST, URL,
            json={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "hello"}]}},
        )
        c = _client()
        result = c.rpc("tools/call", {"name": "echo", "arguments": {"msg": "hi"}})
        self.assertEqual(result["content"][0]["text"], "hello")
        import json
        body = json.loads(responses.calls[0].request.body)
        self.assertEqual(body["params"]["name"], "echo")


class ErrorEnvelope(unittest.TestCase):
    @responses.activate
    def test_jsonrpc_error_raises_normalized(self):
        responses.add(
            responses.POST, URL,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method not found"}},
        )
        c = _client()
        with self.assertRaises(mcp.McpError) as cm:
            c.rpc("tools/list")
        self.assertEqual(cm.exception.code, -32601)
        self.assertIn("method not found", str(cm.exception))

    @responses.activate
    def test_http_500_raises_after_retries(self):
        for _ in range(6):
            responses.add(responses.POST, URL, json={"e": 1}, status=500)
        c = _client(max_retries=2)
        with self.assertRaises(mcp.McpError) as cm:
            c.rpc("tools/list")
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(len(responses.calls), 3)  # 1 initial + 2 retries

    @responses.activate
    def test_envelope_without_result_or_error_raises(self):
        responses.add(responses.POST, URL, json={"jsonrpc": "2.0", "id": 1})
        c = _client()
        with self.assertRaises(mcp.McpError):
            c.rpc("tools/list")


class SseTransport(unittest.TestCase):
    @responses.activate
    def test_sse_stream_parsed(self):
        sse = (
            "event: message\n"
            'data: {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "sse-tool"}]}}\n'
            "\n"
        )
        responses.add(
            responses.POST, URL, body=sse, status=200,
            content_type="text/event-stream",
        )
        c = _client()
        result = c.rpc("tools/list")
        self.assertEqual(result["tools"][0]["name"], "sse-tool")

    @responses.activate
    def test_sse_multiline_data_concatenated(self):
        # SSE spec: multiple data: lines in one event join with newlines.
        sse = 'data: {"jsonrpc": "2.0", "id": 1,\ndata:  "result": {"ok": true}}\n\n'
        responses.add(responses.POST, URL, body=sse, status=200, content_type="text/event-stream")
        c = _client()
        self.assertEqual(c.rpc("tools/list"), {"ok": True})

    def test_parse_sse_picks_last_data_frame(self):
        sse = 'data: {"a": 1}\n\ndata: {"jsonrpc":"2.0","result":{"final": true}}\n\n'
        self.assertEqual(mcp._parse_sse_jsonrpc(sse), {"jsonrpc": "2.0", "result": {"final": True}})
        self.assertIsNone(mcp._parse_sse_jsonrpc(""))
        self.assertIsNone(mcp._parse_sse_jsonrpc("data: not-json\n\n"))


class RetryAfter(unittest.TestCase):
    @responses.activate
    def test_429_honours_retry_after(self):
        responses.add(responses.POST, URL, json={"e": 1}, status=429, headers={"Retry-After": "5"})
        responses.add(responses.POST, URL, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        sleeps = []
        c = mcp.Client(key="demo", url=URL, bearer="b", _rng=random.Random(1), _sleeper=sleeps.append)
        self.assertEqual(c.rpc("tools/list"), {"ok": True})
        self.assertEqual(sleeps, [5.0])


if __name__ == "__main__":
    unittest.main()
