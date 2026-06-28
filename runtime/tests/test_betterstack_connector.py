"""Fixture tests for the Better Stack connector (script connector — force-code trigger d).

Tests cover:
  - YAML loads via lib.api's manifest loader and maps every field correctly.
  - Better Stack pagination (_betterstack_pages) stitches ≥2 pages correctly by following
    ``pagination.next`` (a body-embedded full URL), stopping when next is null.
  - The bearer credential rides EVERY request including continuation pages (trigger d invariant).
  - api.pick selects the support-relevant fields from JSON:API shaped objects.
  - get_monitors / get_incidents fetch and pre-select the right fields.
  - monitors_to_markdown / incidents_to_markdown render correctly (status, cause, timestamps).
  - The connector CLI (main()) drives the monitors and incidents commands.
  - Token-prefix hygiene: no real Better Stack API token prefix lands in the connector files.

No live creds, no network. HTTP is mocked with ``responses``.
Bodies mirror Better Stack's documented example payloads, trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_betterstack_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

# Import the connector AFTER lib (it registers the manifest on import).
import lib.connectors.betterstack as bs  # noqa: E402

API_BASE = "https://uptime.betterstack.com/api/v2"

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# JSON:API shape: { "id": "…", "type": "…", "attributes": { … } }
# ---------------------------------------------------------------------------

_MONITOR_1 = {
    "id": "2",
    "type": "monitor",
    "attributes": {
        "url": "https://uptime.betterstack.com",
        "pronounceable_name": "Uptime homepage",
        "monitor_type": "keyword",
        "status": "up",
        "last_checked_at": "2020-09-01T14:17:46.000Z",
        "check_frequency": 30,
        "paused_at": None,
        "created_at": "2020-02-18T13:38:16.586Z",
        "updated_at": "2020-09-08T13:10:20.202Z",
    },
}

_MONITOR_2 = {
    "id": "3",
    "type": "monitor",
    "attributes": {
        "url": "https://example.com/api/health",
        "pronounceable_name": "API health check",
        "monitor_type": "status",
        "status": "down",
        "last_checked_at": "2020-09-02T10:00:00.000Z",
        "check_frequency": 60,
        "paused_at": None,
        "created_at": "2020-03-01T08:00:00.000Z",
        "updated_at": "2020-09-02T10:00:00.000Z",
    },
}

_INCIDENT_1 = {
    "id": "25",
    "type": "incident",
    "attributes": {
        "name": "uptime homepage",
        "url": "https://uptime.betterstack.com/",
        "http_method": "get",
        "cause": "Status 404",
        "incident_group_id": None,
        "started_at": "2020-03-09T17:37:56.662Z",
        "acknowledged_at": None,
        "acknowledged_by": None,
        "resolved_at": None,
        "resolved_by": None,
        "status": "Started",
        "team_name": "Testing team",
        "response_content": "Not found",
        "regions": ["us", "eu", "as", "au"],
    },
}

_INCIDENT_2 = {
    "id": "26",
    "type": "incident",
    "attributes": {
        "name": "API health check",
        "url": "https://example.com/api/health",
        "http_method": "get",
        "cause": "Timeout",
        "incident_group_id": None,
        "started_at": "2020-03-10T09:00:00.000Z",
        "acknowledged_at": "2020-03-10T09:05:00.000Z",
        "acknowledged_by": "alice@example.com",
        "resolved_at": "2020-03-10T10:00:00.000Z",
        "resolved_by": "alice@example.com",
        "status": "Resolved",
        "team_name": "Testing team",
        "response_content": "",
        "regions": ["us"],
    },
}


def _page(items: list, next_url: str | None = None) -> dict:
    """Build a Better Stack list response envelope with pagination."""
    return {
        "data": items,
        "pagination": {
            "first": f"{API_BASE}/monitors?page=1",
            "last": f"{API_BASE}/monitors?page=2",
            "prev": None,
            "next": next_url,
        },
    }


# ---------------------------------------------------------------------------
# Helper: save/restore env around each test
# ---------------------------------------------------------------------------

class _BetterStackBase(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.get("RC_CONN_BETTERSTACK")
        # Split the prefix so the hygiene guard can't flag this file itself.
        # Better Stack tokens look like "bt_<random>" or are just opaque strings.
        os.environ["RC_CONN_BETTERSTACK"] = "bt_" + "test_fakesecrettoken0000000_abcdef"

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("RC_CONN_BETTERSTACK", None)
        else:
            os.environ["RC_CONN_BETTERSTACK"] = self._saved_env


# ---------------------------------------------------------------------------
# 1. Manifest loading
# ---------------------------------------------------------------------------

class TestBetterStackManifest(_BetterStackBase):
    def test_yaml_loads_and_maps_every_field(self):
        """YAML manifest loads via lib.api loader and maps every field."""
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        m = api.load_manifests()
        self.assertIn("betterstack", m)
        b = m["betterstack"]
        self.assertEqual(b.key, "betterstack")
        self.assertEqual(b.base_url, "https://uptime.betterstack.com/api/v2")
        self.assertEqual(b.auth.strategy, "bearer")
        self.assertEqual(b.pagination.style, "none")
        self.assertEqual(b.pagination.items_field, "data")
        self.assertEqual(b.pagination.page_size, 50)
        self.assertEqual(b.rate_limit_remaining_header, "")

    def test_connector_registers_manifest(self):
        """Connector's register() call makes the manifest drivable via lib.api."""
        self.assertIn("betterstack", api.MANIFESTS)
        m = api.MANIFESTS["betterstack"]
        self.assertEqual(m.base_url, "https://uptime.betterstack.com/api/v2")
        self.assertEqual(m.auth.strategy, "bearer")


# ---------------------------------------------------------------------------
# 2. Pagination: body-embedded pagination.next (trigger d)
# ---------------------------------------------------------------------------

class TestBetterStackPagination(_BetterStackBase):
    @responses_lib.activate
    def test_pagination_stitches_two_pages_via_body_next_url(self):
        """_betterstack_pages follows pagination.next as an absolute URL across 2 pages."""
        page2_url = f"{API_BASE}/monitors?page=2"
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/monitors",
            json=_page([_MONITOR_1], next_url=page2_url),
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            page2_url,
            json=_page([_MONITOR_2], next_url=None),
            status=200,
        )

        all_items = []
        for batch in bs._betterstack_pages("monitors"):
            all_items.extend(batch)

        self.assertEqual(len(all_items), 2)
        self.assertEqual(all_items[0]["id"], "2")
        self.assertEqual(all_items[1]["id"], "3")
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_bearer_credential_on_all_pages_including_continuation(self):
        """Bearer token rides every request, including the continuation (page 2)."""
        page2_url = f"{API_BASE}/monitors?page=2"
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/monitors",
            json=_page([_MONITOR_1], next_url=page2_url),
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            page2_url,
            json=_page([_MONITOR_2], next_url=None),
            status=200,
        )

        list(bs._betterstack_pages("monitors"))

        for call in responses_lib.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(auth.startswith("Bearer "), f"Missing Bearer on {call.request.url}")
            self.assertIn("fakesecrettoken", auth)

    @responses_lib.activate
    def test_single_page_no_continuation(self):
        """pagination.next=null on first page stops pagination immediately."""
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/monitors",
            json=_page([_MONITOR_1], next_url=None),
            status=200,
        )

        all_items = []
        for batch in bs._betterstack_pages("monitors"):
            all_items.extend(batch)

        self.assertEqual(len(all_items), 1)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_pagination_next_url_extracted_correctly(self):
        """_pagination_next helper extracts the full next URL from the body."""
        body = _page([_MONITOR_1], next_url=f"{API_BASE}/monitors?page=3")
        nxt = bs._pagination_next(body)
        self.assertEqual(nxt, f"{API_BASE}/monitors?page=3")

    def test_pagination_next_none_when_null(self):
        """_pagination_next returns None when next is null."""
        body = _page([_MONITOR_1], next_url=None)
        self.assertIsNone(bs._pagination_next(body))

    def test_pagination_next_none_when_missing(self):
        """_pagination_next returns None when pagination key is absent."""
        self.assertIsNone(bs._pagination_next({"data": []}))
        self.assertIsNone(bs._pagination_next(None))


# ---------------------------------------------------------------------------
# 3. api.pick on JSON:API shaped objects
# ---------------------------------------------------------------------------

class TestBetterStackPick(_BetterStackBase):
    def test_pick_monitor_attributes(self):
        """api.pick can extract fields from the JSON:API attributes envelope."""
        picked = api.pick(_MONITOR_1, "id,attributes.status,attributes.pronounceable_name,attributes.url")
        self.assertEqual(picked["id"], "2")
        self.assertEqual(picked["attributes.status"], "up")
        self.assertEqual(picked["attributes.pronounceable_name"], "Uptime homepage")

    def test_pick_incident_attributes(self):
        picked = api.pick(
            _INCIDENT_1,
            "id,attributes.name,attributes.status,attributes.cause,attributes.started_at,attributes.resolved_at",
        )
        self.assertEqual(picked["id"], "25")
        self.assertEqual(picked["attributes.status"], "Started")
        self.assertEqual(picked["attributes.cause"], "Status 404")
        self.assertIsNone(picked.get("attributes.resolved_at"))


# ---------------------------------------------------------------------------
# 4. get_monitors / get_incidents field pre-selection
# ---------------------------------------------------------------------------

class TestGetMonitors(_BetterStackBase):
    @responses_lib.activate
    def test_get_monitors_returns_preselected_fields(self):
        """get_monitors fetches and pre-selects support-relevant fields."""
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/monitors",
            json=_page([_MONITOR_1, _MONITOR_2], next_url=None),
            status=200,
        )
        mons = bs.get_monitors()
        self.assertEqual(len(mons), 2)
        m = mons[0]
        self.assertEqual(m["id"], "2")
        self.assertEqual(m["name"], "Uptime homepage")
        self.assertEqual(m["status"], "up")
        self.assertEqual(m["url"], "https://uptime.betterstack.com")
        self.assertEqual(m["check_frequency"], 30)
        self.assertIsNone(m["paused_at"])
        self.assertIsNotNone(m["last_checked_at"])
        # Only support-relevant fields present (no raw API dump).
        self.assertNotIn("created_at", m)


class TestGetIncidents(_BetterStackBase):
    @responses_lib.activate
    def test_get_incidents_global_fetches_open_incidents(self):
        """get_incidents() with no monitor_id hits /incidents and pre-selects fields."""
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/incidents",
            json=_page([_INCIDENT_1], next_url=None),
            status=200,
        )
        incs = bs.get_incidents()
        self.assertEqual(len(incs), 1)
        inc = incs[0]
        self.assertEqual(inc["id"], "25")
        self.assertEqual(inc["name"], "uptime homepage")
        self.assertEqual(inc["cause"], "Status 404")
        self.assertEqual(inc["status"], "Started")
        self.assertIsNone(inc["resolved_at"])
        # Only support-relevant fields present.
        self.assertNotIn("response_content", inc)

    @responses_lib.activate
    def test_get_incidents_for_specific_monitor(self):
        """get_incidents(monitor_id=…) hits /monitors/{id}/incidents."""
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/monitors/2/incidents",
            json=_page([_INCIDENT_1, _INCIDENT_2], next_url=None),
            status=200,
        )
        incs = bs.get_incidents(monitor_id="2")
        self.assertEqual(len(incs), 2)
        self.assertEqual(incs[0]["id"], "25")
        self.assertEqual(incs[1]["id"], "26")
        self.assertIsNotNone(incs[1]["resolved_at"])
        # Confirm the request hit the nested path.
        req_url = responses_lib.calls[0].request.url
        self.assertIn("/monitors/2/incidents", req_url)

    @responses_lib.activate
    def test_get_incidents_pagination_stitches_pages(self):
        """get_incidents stitches multiple pages via _betterstack_pages."""
        page2_url = f"{API_BASE}/incidents?page=2"
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/incidents",
            json=_page([_INCIDENT_1], next_url=page2_url),
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            page2_url,
            json=_page([_INCIDENT_2], next_url=None),
            status=200,
        )
        incs = bs.get_incidents()
        self.assertEqual(len(incs), 2)
        self.assertEqual(incs[0]["id"], "25")
        self.assertEqual(incs[1]["id"], "26")
        self.assertEqual(len(responses_lib.calls), 2)
        # Bearer token on both calls.
        for call in responses_lib.calls:
            self.assertIn("Bearer", call.request.headers.get("Authorization", ""))


# ---------------------------------------------------------------------------
# 5. Markdown rendering
# ---------------------------------------------------------------------------

class TestMarkdownRendering(_BetterStackBase):
    def test_monitors_to_markdown_up_monitor(self):
        mons = [bs._pick_monitor(_MONITOR_1)]
        md = bs.monitors_to_markdown(mons)
        self.assertIn("# Better Stack Monitors", md)
        self.assertIn("UP", md)
        self.assertIn("Uptime homepage", md)
        self.assertIn("30s", md)

    def test_monitors_to_markdown_down_monitor(self):
        mons = [bs._pick_monitor(_MONITOR_2)]
        md = bs.monitors_to_markdown(mons)
        self.assertIn("DOWN", md)
        self.assertIn("API health check", md)

    def test_monitors_to_markdown_empty(self):
        md = bs.monitors_to_markdown([])
        self.assertIn("no monitors", md)

    def test_incidents_to_markdown_unresolved(self):
        incs = [bs._pick_incident(_INCIDENT_1)]
        md = bs.incidents_to_markdown(incs)
        self.assertIn("# Better Stack Incidents", md)
        self.assertIn("UNRESOLVED", md)
        self.assertIn("Status 404", md)
        self.assertIn("uptime homepage", md)
        self.assertIn("2020-03-09", md)

    def test_incidents_to_markdown_resolved(self):
        incs = [bs._pick_incident(_INCIDENT_2)]
        md = bs.incidents_to_markdown(incs)
        self.assertIn("resolved", md)
        self.assertIn("Timeout", md)

    def test_incidents_to_markdown_with_monitor_id_scope(self):
        incs = [bs._pick_incident(_INCIDENT_1)]
        md = bs.incidents_to_markdown(incs, monitor_id="2")
        self.assertIn("for monitor 2", md)

    def test_incidents_to_markdown_empty(self):
        md = bs.incidents_to_markdown([])
        self.assertIn("no incidents", md)


# ---------------------------------------------------------------------------
# 6. CLI drive (connector main)
# ---------------------------------------------------------------------------

class TestBetterStackCLI(_BetterStackBase):
    @responses_lib.activate
    def test_cli_monitors_command(self):
        """CLI 'monitors' command fetches and prints monitor markdown."""
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/monitors",
            json=_page([_MONITOR_1, _MONITOR_2], next_url=None),
            status=200,
        )
        rc = bs.main(["monitors"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_cli_incidents_command(self):
        """CLI 'incidents' command fetches global incidents and prints markdown."""
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/incidents",
            json=_page([_INCIDENT_1], next_url=None),
            status=200,
        )
        rc = bs.main(["incidents"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_incidents_with_monitor_id(self):
        """CLI 'incidents --monitor-id <id>' hits the nested monitor incidents path."""
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/monitors/2/incidents",
            json=_page([_INCIDENT_1, _INCIDENT_2], next_url=None),
            status=200,
        )
        rc = bs.main(["incidents", "--monitor-id", "2"])
        self.assertEqual(rc, 0)
        req_url = responses_lib.calls[0].request.url
        self.assertIn("/monitors/2/incidents", req_url)

    @responses_lib.activate
    def test_lib_api_cli_drives_manifest_single_page(self):
        """python -m lib.api get betterstack monitors works for single-page reads."""
        responses_lib.add(
            responses_lib.GET,
            f"{API_BASE}/monitors",
            json=_page([_MONITOR_1], next_url=None),
            status=200,
        )
        rc = api._main(["get", "betterstack", "monitors"])
        self.assertEqual(rc, 0)
        # Confirm bearer credential rode the request.
        auth = responses_lib.calls[0].request.headers.get("Authorization", "")
        self.assertTrue(auth.startswith("Bearer "))
        self.assertIn("fakesecrettoken", auth)


# ---------------------------------------------------------------------------
# 7. Token-prefix hygiene
# ---------------------------------------------------------------------------

class TestBetterStackHygiene(unittest.TestCase):
    """CI guard: no real Better Stack API token prefix may land in the connector files.

    Scoped to the connector dir (manifest + __init__ + __main__), NOT this test file — the test
    legitimately names the prefixes it hunts for, so scanning itself would be a false positive.
    """

    # Better Stack tokens begin with "bt_" followed by a long hex string.
    # Split the literal so the hygiene guard can't flag this file itself.
    _TOKEN_PREFIXES = ("bt_" "live",)  # concatenated; real live tokens follow "bt_live_..."

    def test_no_token_prefixes_in_betterstack_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "betterstack"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix in (".pyc",):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref}")
        self.assertEqual(offenders, [], f"real token prefix found in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
