"""Fixture test for the manifest-ONLY Grafana Cloud integration — proves a catalogued connector
with NO bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. Bodies are Grafana's documented example
payloads (https://grafana.com/docs/grafana/latest/developers/http_api/), trimmed to the fields
relevant for support reads.

Grafana paginates the legacy /api/* surface with offset params (page/perpage). We test:
  - Two-page stitch on /api/org/users/lookup (proves offset loop works).
  - Single-page (complete) result on /api/datasources (most endpoints return all in one shot).
  - Bearer credential on every request (including the second offset page).
  - `api.pick` prunes the response to support-relevant fields.
  - CLI drive via `api._main` works for both single and paginated calls.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_grafana_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

# Stack URL the project brain would configure (per-stack subdomain).
STACK = "https://myorg.grafana.net"
USERS_URL = f"{STACK}/api/org/users/lookup"
DATASOURCES_URL = f"{STACK}/api/datasources"
ALERT_RULES_URL = f"{STACK}/api/v1/provisioning/alert-rules"
ANNOTATIONS_URL = f"{STACK}/api/annotations"


# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields)
# ---------------------------------------------------------------------------

_USERS_PAGE_1 = [
    {"login": "alice", "name": "Alice Admin", "email": "alice@example.com", "role": "Admin"},
    {"login": "bob", "name": "Bob Viewer", "email": "bob@example.com", "role": "Viewer"},
]
_USERS_PAGE_2 = [
    {"login": "carol", "name": "Carol Editor", "email": "carol@example.com", "role": "Editor"},
]

_DATASOURCES = [
    {
        "id": 1,
        "uid": "P1809F7CD0C75ACF3",
        "name": "Prometheus",
        "type": "prometheus",
        "url": "http://prometheus:9090",
        "isDefault": True,
    },
    {
        "id": 2,
        "uid": "PD8C576611E62080A",
        "name": "Loki",
        "type": "loki",
        "url": "http://loki:3100",
        "isDefault": False,
    },
]

_ALERT_RULES = [
    {
        "uid": "AUID001",
        "title": "High CPU Usage",
        "ruleGroup": "infra",
        "labels": {"severity": "critical"},
        "annotations": {"summary": "CPU above 90%"},
        "for": "5m",
        "noDataState": "NoData",
        "execErrState": "Alerting",
    },
    {
        "uid": "AUID002",
        "title": "Pod OOMKilled",
        "ruleGroup": "k8s",
        "labels": {"severity": "warning"},
        "annotations": {"summary": "Container killed by OOM"},
        "for": "0s",
        "noDataState": "OK",
        "execErrState": "OK",
    },
]

_ANNOTATIONS = [
    {
        "id": 1124,
        "dashboardUID": "uGlb_lG7z",
        "panelId": 2,
        "time": 1507266395000,
        "text": "Deployed v2.3.1",
        "tags": ["deploy", "production"],
    },
]


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class GrafanaManifestLoaded(unittest.TestCase):
    """Manifest loads cleanly and all declared fields map correctly."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GRAFANA")
        os.environ["RC_CONN_GRAFANA"] = "glsa_testtoken_" + "abc123"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GRAFANA", None)
        else:
            os.environ["RC_CONN_GRAFANA"] = self._saved

    def test_manifest_fields(self):
        m = api.load_manifests()
        self.assertIn("grafana", m)
        g = m["grafana"]
        self.assertEqual(g.auth.strategy, "bearer")
        # Offset pagination with Grafana's page/perpage naming.
        self.assertEqual(g.pagination.style, "offset")
        self.assertEqual(g.pagination.offset_param, "page")
        self.assertEqual(g.pagination.limit_param, "perpage")
        self.assertEqual(g.pagination.page_size, 1000)
        # No documented rate-limit remaining header.
        self.assertEqual(g.rate_limit_remaining_header, "")
        # No required default headers on the legacy /api/* surface.
        self.assertEqual(g.default_headers, {})


class GrafanaOffsetPagination(unittest.TestCase):
    """Two-page offset stitch and bearer-on-every-request coverage."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GRAFANA")
        os.environ["RC_CONN_GRAFANA"] = "glsa_testtoken_" + "abc123"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GRAFANA", None)
        else:
            os.environ["RC_CONN_GRAFANA"] = self._saved

    @responses.activate
    def test_offset_pagination_stitches_two_pages(self):
        """Page 1 returns page_size items; page 2 returns fewer — loop stops."""
        # Grafana's manifest declares page_size=1000, but for the test we drive with a small
        # page size so two fixture pages are realistic without large payloads.
        api.load_manifests()
        g = api.MANIFESTS["grafana"]
        # Use a custom client with page_size=2 to force two pages from our 3-item fixture.
        custom_manifest = api.Manifest(
            key=g.key,
            base_url=g.base_url,
            auth=g.auth,
            pagination=api.Pagination(
                style="offset",
                offset_param="page",
                limit_param="perpage",
                page_size=2,
            ),
            rate_limit_remaining_header=g.rate_limit_remaining_header,
            default_headers=g.default_headers,
        )
        c = api.Client(manifest=custom_manifest, credential="glsa_testtoken_" + "abc123")

        # Page 1: returns 2 users (= page_size) → continue.
        # Page 2: returns 1 user (< page_size) → stop.
        responses.add(responses.GET, USERS_URL, json=_USERS_PAGE_1, status=200)
        responses.add(responses.GET, USERS_URL, json=_USERS_PAGE_2, status=200)

        result = c.collect(USERS_URL, max_pages=10)

        self.assertFalse(result["incomplete"], result["reason"])
        logins = [u["login"] for u in result["items"]]
        self.assertEqual(logins, ["alice", "bob", "carol"])

        # Bearer credential on both page requests.
        for call in responses.calls:
            self.assertEqual(
                call.request.headers["Authorization"],
                "Bearer glsa_testtoken_" + "abc123",
            )

    @responses.activate
    def test_datasources_single_page_complete(self):
        """Datasources return all items in one page — no second request."""
        api.load_manifests()
        c = api.client(api.MANIFESTS["grafana"])

        responses.add(responses.GET, DATASOURCES_URL, json=_DATASOURCES, status=200)

        # Single GET — not paginated, just a plain .get().
        body = c.get(DATASOURCES_URL)
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0]["type"], "prometheus")
        self.assertEqual(body[1]["uid"], "PD8C576611E62080A")

        # Bearer present.
        self.assertEqual(
            responses.calls[0].request.headers["Authorization"],
            "Bearer glsa_testtoken_" + "abc123",
        )

    @responses.activate
    def test_pick_prunes_alert_rules(self):
        """pick() selects the support-relevant fields from alert rule objects."""
        api.load_manifests()
        c = api.client(api.MANIFESTS["grafana"])

        responses.add(responses.GET, ALERT_RULES_URL, json=_ALERT_RULES, status=200)

        rules = c.get(ALERT_RULES_URL)
        picked = [api.pick(r, "uid,title,ruleGroup,labels.severity,annotations.summary,for") for r in rules]

        self.assertEqual(picked[0]["uid"], "AUID001")
        self.assertEqual(picked[0]["title"], "High CPU Usage")
        self.assertEqual(picked[0]["labels.severity"], "critical")
        self.assertEqual(picked[0]["annotations.summary"], "CPU above 90%")
        self.assertEqual(picked[0]["for"], "5m")
        self.assertEqual(picked[1]["labels.severity"], "warning")

    @responses.activate
    def test_annotations_single_page(self):
        """Annotations endpoint returns a bare JSON array (no envelope)."""
        api.load_manifests()
        c = api.client(api.MANIFESTS["grafana"])

        responses.add(responses.GET, ANNOTATIONS_URL, json=_ANNOTATIONS, status=200)

        body = c.get(ANNOTATIONS_URL, query={"limit": 200})
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["id"], 1124)
        self.assertIn("deploy", body[0]["tags"])


class GrafanaCLIDrive(unittest.TestCase):
    """CLI drive via api._main for manifest-only Grafana reads."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_GRAFANA")
        os.environ["RC_CONN_GRAFANA"] = "glsa_testtoken_" + "abc123"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_GRAFANA", None)
        else:
            os.environ["RC_CONN_GRAFANA"] = self._saved

    @responses.activate
    def test_cli_get_datasources_with_pick(self):
        """CLI GET with --pick returns pruned fields; bearer on the wire."""
        responses.add(responses.GET, DATASOURCES_URL, json=_DATASOURCES, status=200)

        rc = api._main([
            "get", "grafana", DATASOURCES_URL,
            "--pick", "uid,name,type,isDefault",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(responses.calls[0].request.headers["Authorization"],
                         "Bearer glsa_testtoken_" + "abc123")

    @responses.activate
    def test_cli_get_alert_rules(self):
        """CLI GET alert rules; single page, no --paginate needed."""
        responses.add(responses.GET, ALERT_RULES_URL, json=_ALERT_RULES, status=200)

        rc = api._main([
            "get", "grafana", ALERT_RULES_URL,
            "--pick", "uid,title,ruleGroup",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(responses.calls[0].request.url.startswith(ALERT_RULES_URL))


class GrafanaCassetteHygiene(unittest.TestCase):
    """CI guard: no real Grafana service-account token prefix may appear in committed files.

    Scoped to the connector dir, NOT this test file — the test legitimately names the prefixes
    it hunts for. Prefixes are split with concatenation to avoid the guard flagging itself.
    """

    # Grafana service-account token prefix: glsa_
    _TOKEN_PREFIXES = ("glsa" "_",)

    def test_no_token_prefixes_in_grafana_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "grafana"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
