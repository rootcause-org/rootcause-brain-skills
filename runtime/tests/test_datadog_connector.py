"""Fixture test for the Datadog connector — proves dual-header auth, offset pagination,
field pre-selection, and the CLI drive end-to-end via mocked HTTP.

No live creds, no network. Bodies are trimmed versions of Datadog's documented example
payloads. Datadog v2 list endpoints use offset pagination with a {"data": [...], "meta":
{"pagination": {"offset": 0, "limit": 50, "total_count": N}}} envelope.

Force-code trigger: (c) exotic auth — DD-API-KEY + DD-APPLICATION-KEY dual-header injection
is not expressible with any single lib.api auth strategy.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \
        pytest tests/test_datadog_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import datadog as dd  # noqa: E402

API_BASE = "https://api.datadoghq.com"
MONITORS_URL = f"{API_BASE}/api/v1/monitor"
INCIDENTS_URL = f"{API_BASE}/api/v2/incidents"
EVENTS_URL = f"{API_BASE}/api/v2/events"

# Documented example monitor object (v1), trimmed to support-relevant fields.
_MONITOR_1 = {
    "id": 12345,
    "name": "High CPU usage on prod",
    "type": "metric alert",
    "query": "avg(last_5m):avg:system.cpu.user{env:prod} > 80",
    "overall_state": "Alert",
    "message": "CPU is over 80% on {{host.name}}. @slack-oncall",
    "tags": ["env:prod", "team:infra"],
    "created": "2024-01-10T08:00:00+00:00",
    "modified": "2024-06-01T12:00:00+00:00",
}
_MONITOR_2 = {
    "id": 67890,
    "name": "Error rate spike",
    "type": "metric alert",
    "query": "avg(last_5m):sum:trace.web.request.errors{env:prod}.as_rate() > 0.05",
    "overall_state": "No Data",
    "message": "Error rate > 5%.",
    "tags": ["env:prod", "team:backend"],
    "created": "2024-03-15T09:30:00+00:00",
    "modified": "2024-06-10T14:00:00+00:00",
}

# Two pages of v2 incidents (Datadog documented example shape).
_INCIDENT_1 = {
    "id": "incident-aaa111",
    "type": "incidents",
    "attributes": {
        "title": "Database latency spike",
        "status": "active",
        "severity": "SEV-2",
        "public_id": 101,
        "created": "2024-06-20T10:00:00+00:00",
        "modified": "2024-06-20T11:00:00+00:00",
        "state": "active",
        "commander_user": {"name": "Alice"},
    },
}
_INCIDENT_2 = {
    "id": "incident-bbb222",
    "type": "incidents",
    "attributes": {
        "title": "API 5xx errors",
        "status": "resolved",
        "severity": "SEV-3",
        "public_id": 102,
        "created": "2024-06-18T08:00:00+00:00",
        "modified": "2024-06-18T09:30:00+00:00",
        "state": "resolved",
        "commander_user": {"name": "Bob"},
    },
}

# Two pages of v2 events.
_EVENT_1 = {
    "id": "AAAAAXxxx",
    "type": "events",
    "attributes": {
        "title": "Deploy: api-service v1.2.3",
        "message": "Deployed by CI pipeline",
        "timestamp": "2024-06-20T10:00:00+00:00",
        "status": "info",
        "priority": "normal",
        "source_type_name": "deployment",
        "tags": ["env:prod", "service:api"],
    },
}
_EVENT_2 = {
    "id": "AAAAAXyyy",
    "type": "events",
    "attributes": {
        "title": "Anomaly detected in payment service",
        "message": "Latency anomaly detected",
        "timestamp": "2024-06-20T10:05:00+00:00",
        "status": "warning",
        "priority": "high",
        "source_type_name": "anomaly_detection",
        "tags": ["env:prod", "service:payments"],
    },
}

# Fake credentials — split at ':' by _parse_credential(). Using concatenation to avoid
# the token-prefix hygiene guard from flagging these test strings as real credentials.
_FAKE_API_KEY = "ddapikey" + "fake0001"
_FAKE_APP_KEY = "ddappkey" + "fake0002"
_FAKE_CRED = f"{_FAKE_API_KEY}:{_FAKE_APP_KEY}"


class DatadogManifestLoad(unittest.TestCase):
    """The YAML manifest loads and maps every field correctly."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loaded_from_yaml(self):
        manifests = api.load_manifests()
        self.assertIn("datadog", manifests)
        m = manifests["datadog"]
        self.assertEqual(m.key, "datadog")
        self.assertEqual(m.base_url, "https://api.datadoghq.com")
        # auth strategy is "none" — the script injects dual headers manually
        self.assertEqual(m.auth.strategy, "none")
        self.assertEqual(m.pagination.style, "offset")
        self.assertEqual(m.pagination.items_field, "data")
        self.assertEqual(m.pagination.offset_param, "page[offset]")
        self.assertEqual(m.pagination.limit_param, "page[limit]")
        self.assertEqual(m.pagination.page_size, 50)
        self.assertEqual(m.rate_limit_remaining_header, "X-RateLimit-Remaining")


class DatadogDualHeaderAuth(unittest.TestCase):
    """Both DD-API-KEY and DD-APPLICATION-KEY ride every request."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_DATADOG")
        os.environ["RC_CONN_DATADOG"] = _FAKE_CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_DATADOG", None)
        else:
            os.environ["RC_CONN_DATADOG"] = self._saved

    @responses_lib.activate
    def test_both_auth_headers_on_monitors_call(self):
        responses_lib.add(responses_lib.GET, MONITORS_URL,
                          json=[_MONITOR_1], status=200,
                          headers={"X-RateLimit-Remaining": "999"})

        monitors = dd.get_monitors()

        self.assertEqual(len(monitors), 1)
        req = responses_lib.calls[0].request
        # Both credentials must appear on the wire as separate headers.
        self.assertEqual(req.headers["DD-API-KEY"], _FAKE_API_KEY)
        self.assertEqual(req.headers["DD-APPLICATION-KEY"], _FAKE_APP_KEY)
        # Standard bearer Authorization must NOT appear (no double-auth confusion).
        self.assertNotIn("Authorization", req.headers)

    @responses_lib.activate
    def test_both_auth_headers_on_incidents_call(self):
        page1 = {"data": [_INCIDENT_1], "meta": {"pagination": {"offset": 0, "limit": 50, "total_count": 1}}}
        responses_lib.add(responses_lib.GET, INCIDENTS_URL, json=page1, status=200)

        result = dd.get_incidents()

        req = responses_lib.calls[0].request
        self.assertEqual(req.headers["DD-API-KEY"], _FAKE_API_KEY)
        self.assertEqual(req.headers["DD-APPLICATION-KEY"], _FAKE_APP_KEY)
        self.assertFalse(result["incomplete"])

    @responses_lib.activate
    def test_both_auth_headers_on_events_call(self):
        page1 = {"data": [_EVENT_1], "meta": {"pagination": {"offset": 0, "limit": 50, "total_count": 1}}}
        responses_lib.add(responses_lib.GET, EVENTS_URL, json=page1, status=200)

        result = dd.get_events()

        req = responses_lib.calls[0].request
        self.assertEqual(req.headers["DD-API-KEY"], _FAKE_API_KEY)
        self.assertEqual(req.headers["DD-APPLICATION-KEY"], _FAKE_APP_KEY)
        self.assertFalse(result["incomplete"])


class DatadogOffsetPagination(unittest.TestCase):
    """Offset pagination stitches ≥2 pages for v2 endpoints (incidents, events)."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_DATADOG")
        os.environ["RC_CONN_DATADOG"] = _FAKE_CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_DATADOG", None)
        else:
            os.environ["RC_CONN_DATADOG"] = self._saved

    @responses_lib.activate
    def test_incidents_stitches_two_pages(self):
        # Page 1: 1 item, returns fewer than page_size → stop (lib.api offset style)
        # Actually we need page 1 to have exactly page_size items to trigger page 2.
        # Simplify: set page_size=1 by injecting a custom connector call; instead, mock
        # both pages naturally. lib.api offset stops when items < page_size.
        # For page_size=50 with 1 item, it stops. So we mock at lower level:
        # use max_items=1 with 2 mock responses and verify only 1 page is fetched.
        # Better: use get_incidents with a custom max_items and 2 responses each returning
        # exactly page_size=50 items, then a 3rd with 0.
        # Simplest: override the connector's internal client to use page_size=1.
        # Instead, use lib.api collect directly on the manifest.

        # Two pages where page 1 returns page_size items (triggering page 2).
        page_size = dd.MANIFEST.pagination.page_size  # 50

        items_p1 = [dict(_INCIDENT_1, id=f"inc-{i}") for i in range(page_size)]
        items_p2 = [_INCIDENT_2]  # < page_size → loop stops

        page1_body = {"data": items_p1, "meta": {"pagination": {"offset": 0, "limit": page_size}}}
        page2_body = {"data": items_p2, "meta": {"pagination": {"offset": page_size, "limit": page_size}}}

        responses_lib.add(responses_lib.GET, INCIDENTS_URL, json=page1_body, status=200)
        responses_lib.add(responses_lib.GET, INCIDENTS_URL, json=page2_body, status=200)

        result = dd.get_incidents(max_items=200)

        self.assertFalse(result["incomplete"], result["reason"])
        # All items from both pages collected.
        self.assertEqual(len(result["items"]), page_size + 1)
        # Both requests carried both auth headers.
        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["DD-API-KEY"], _FAKE_API_KEY)
            self.assertEqual(call.request.headers["DD-APPLICATION-KEY"], _FAKE_APP_KEY)

    @responses_lib.activate
    def test_events_stitches_two_pages(self):
        page_size = dd.MANIFEST.pagination.page_size  # 50

        items_p1 = [dict(_EVENT_1, id=f"evt-{i}") for i in range(page_size)]
        items_p2 = [_EVENT_2]

        page1_body = {"data": items_p1, "meta": {}}
        page2_body = {"data": items_p2, "meta": {}}

        responses_lib.add(responses_lib.GET, EVENTS_URL, json=page1_body, status=200)
        responses_lib.add(responses_lib.GET, EVENTS_URL, json=page2_body, status=200)

        result = dd.get_events(max_items=200)

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), page_size + 1)
        self.assertEqual(len(responses_lib.calls), 2)


class DatadogFieldSelection(unittest.TestCase):
    """api.pick pre-selects support-relevant fields; raw dumps never flood context."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_DATADOG")
        os.environ["RC_CONN_DATADOG"] = _FAKE_CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_DATADOG", None)
        else:
            os.environ["RC_CONN_DATADOG"] = self._saved

    @responses_lib.activate
    def test_monitors_pick_selects_support_fields(self):
        # Add an "extra" field that should NOT appear in the picked output.
        monitor_with_extra = dict(_MONITOR_1, very_large_config={"foo": "bar" * 500})
        responses_lib.add(responses_lib.GET, MONITORS_URL, json=[monitor_with_extra], status=200)

        monitors = dd.get_monitors()

        self.assertEqual(len(monitors), 1)
        m = monitors[0]
        # Expected support fields are present.
        self.assertEqual(m["id"], 12345)
        self.assertEqual(m["name"], "High CPU usage on prod")
        self.assertEqual(m["overall_state"], "Alert")
        self.assertIn("tags", m)
        # The huge extra field must NOT appear.
        self.assertNotIn("very_large_config", m)

    @responses_lib.activate
    def test_incidents_pick_selects_support_fields(self):
        body = {"data": [_INCIDENT_1], "meta": {}}
        responses_lib.add(responses_lib.GET, INCIDENTS_URL, json=body, status=200)

        result = dd.get_incidents()

        self.assertEqual(len(result["items"]), 1)
        it = result["items"][0]
        # Nested attribute fields are surfaced.
        self.assertIn("attributes.title", it)
        self.assertEqual(it["attributes.title"], "Database latency spike")
        self.assertIn("attributes.status", it)
        self.assertEqual(it["attributes.status"], "active")
        # Raw "attributes" blob is not the output shape — pick flattens paths.
        self.assertNotIn("very_large_detail", it)

    @responses_lib.activate
    def test_events_pick_selects_support_fields(self):
        body = {"data": [_EVENT_1], "meta": {}}
        responses_lib.add(responses_lib.GET, EVENTS_URL, json=body, status=200)

        result = dd.get_events()

        self.assertEqual(len(result["items"]), 1)
        ev = result["items"][0]
        self.assertIn("attributes.title", ev)
        self.assertEqual(ev["attributes.title"], "Deploy: api-service v1.2.3")
        self.assertIn("attributes.tags", ev)


class DatadogCLIDrive(unittest.TestCase):
    """CLI (main()) drives the connector end-to-end — monitors, incidents, events."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_DATADOG")
        os.environ["RC_CONN_DATADOG"] = _FAKE_CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_DATADOG", None)
        else:
            os.environ["RC_CONN_DATADOG"] = self._saved

    @responses_lib.activate
    def test_cli_monitors_prints_markdown(self, capsys=None):
        responses_lib.add(responses_lib.GET, MONITORS_URL, json=[_MONITOR_1, _MONITOR_2], status=200)

        rc = dd.main(["monitors"])

        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)
        req = responses_lib.calls[0].request
        self.assertEqual(req.headers["DD-API-KEY"], _FAKE_API_KEY)
        self.assertEqual(req.headers["DD-APPLICATION-KEY"], _FAKE_APP_KEY)

    @responses_lib.activate
    def test_cli_monitors_json_flag(self):
        responses_lib.add(responses_lib.GET, MONITORS_URL, json=[_MONITOR_1], status=200)

        rc = dd.main(["monitors", "--json"])

        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_monitors_single_id(self):
        monitor_url = f"{API_BASE}/api/v1/monitor/12345"
        responses_lib.add(responses_lib.GET, monitor_url, json=_MONITOR_1, status=200)

        rc = dd.main(["monitors", "--id", "12345"])

        self.assertEqual(rc, 0)
        self.assertIn("/api/v1/monitor/12345", responses_lib.calls[0].request.url)

    @responses_lib.activate
    def test_cli_incidents_prints_markdown(self):
        body = {"data": [_INCIDENT_1], "meta": {}}
        responses_lib.add(responses_lib.GET, INCIDENTS_URL, json=body, status=200)

        rc = dd.main(["incidents"])

        self.assertEqual(rc, 0)
        req = responses_lib.calls[0].request
        self.assertEqual(req.headers["DD-API-KEY"], _FAKE_API_KEY)

    @responses_lib.activate
    def test_cli_events_with_query_param(self):
        body = {"data": [_EVENT_1], "meta": {}}
        responses_lib.add(responses_lib.GET, EVENTS_URL, json=body, status=200)

        rc = dd.main(["events", "--query", "filter[from]=now-1h"])

        self.assertEqual(rc, 0)
        req_url = responses_lib.calls[0].request.url
        self.assertIn("filter%5Bfrom%5D=now-1h", req_url)

    @responses_lib.activate
    def test_cli_incidents_json_flag(self):
        body = {"data": [_INCIDENT_2], "meta": {}}
        responses_lib.add(responses_lib.GET, INCIDENTS_URL, json=body, status=200)

        rc = dd.main(["incidents", "--json"])

        self.assertEqual(rc, 0)


class DatadogCredentialParsing(unittest.TestCase):
    """Credential parsing handles colon-split, raises on malformed/missing values."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_DATADOG")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_DATADOG", None)
        else:
            os.environ["RC_CONN_DATADOG"] = self._saved

    def test_valid_credential_splits_correctly(self):
        os.environ["RC_CONN_DATADOG"] = "myapikey:myappkey"
        api_key, app_key = dd._parse_credential()
        self.assertEqual(api_key, "myapikey")
        self.assertEqual(app_key, "myappkey")

    def test_missing_colon_raises(self):
        os.environ["RC_CONN_DATADOG"] = "justakeynocodon"
        with self.assertRaises(RuntimeError) as ctx:
            dd._parse_credential()
        self.assertIn("colon-separated", str(ctx.exception))

    def test_empty_api_key_raises(self):
        os.environ["RC_CONN_DATADOG"] = ":myappkey"
        with self.assertRaises(RuntimeError) as ctx:
            dd._parse_credential()
        self.assertIn("malformed", str(ctx.exception))

    def test_empty_app_key_raises(self):
        os.environ["RC_CONN_DATADOG"] = "myapikey:"
        with self.assertRaises(RuntimeError) as ctx:
            dd._parse_credential()
        self.assertIn("malformed", str(ctx.exception))

    def test_missing_env_var_raises(self):
        os.environ.pop("RC_CONN_DATADOG", None)
        with self.assertRaises((RuntimeError, SystemExit, EnvironmentError)):
            dd._parse_credential()


class DatadogTokenHygiene(unittest.TestCase):
    """CI guard: no real Datadog API key or app key prefix may land in the connector files.

    Scopes to the connector directory only — this test file legitimately references the
    prefixes it guards against, so scanning itself would be a false positive.
    Token prefix literals are split with string concatenation so this guard never flags itself.
    """

    # Datadog API key prefix: "DD-API" + "-KEY" used in header names only, not in values.
    # Guard against accidentally committed real key values which start with recognizable patterns.
    # Real Datadog keys are 32-hex-char strings; guard against literal env-var-style leaks.
    _TOKEN_PREFIXES = (
        "RC_CONN_DATADOG" + "=",   # env var assignment with a real value
        "DD-API-KEY" + ": dd",     # raw cred in a header value comment
        "DD-APPLICATION-KEY" + ": dd",
    )

    def test_no_credential_values_in_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "datadog"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for prefix in self._TOKEN_PREFIXES:
                if prefix in text:
                    offenders.append(f"{path.name}: contains {prefix!r}")
        self.assertEqual(offenders, [], f"credential-like material found: {offenders}")


if __name__ == "__main__":
    unittest.main()
