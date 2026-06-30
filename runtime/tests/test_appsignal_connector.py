"""Fixture test for the AppSignal exception-grounding script connector.

Force-code trigger (e): AppSignal's public API is GraphQL (POST transport) plus a REST sample
endpoint for the full backtrace; lib.api is GET/REST only. The connector owns every call and
hardcodes no org slug / app id (it discovers them via `apps`).

No live creds, no network: HTTP is mocked with ``responses``. Bodies mirror AppSignal's documented
GraphQL/REST shapes (trimmed to support-relevant fields), verified against the live API.

Tests cover:
  - YAML manifest loads and maps every field; script register() wins over the YAML loader
  - the personal token rides every call as the ?token= URL query param (GraphQL POST + REST GET)
  - apps: viewer discovery flattens organizations → apps
  - search: org-wide sample search returns sample ids; reference-code detection; multi-org fan-out
    falls back to discovery when --org is omitted
  - incidents: app(id) grouped incidents; --app required
  - show: REST sample detail; backtrace/params/env extracted; session_data redacted
  - GraphQL errors array (HTTP 200) surfaces as ApiError
  - api.pick selects support fields; CLI drive for every command
  - token-prefix hygiene guard for this connector's files

    cd runtime && uv run --with . --with pytest --with responses --no-project \\
        pytest tests/test_appsignal_connector.py -q
"""

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests
import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import appsignal as aps  # noqa: E402

GRAPHQL_URL = "https://appsignal.com/graphql"
APP_ID = "674078f483eb67dcd6c4eaf6"
SAMPLE_ID = f"{APP_ID}-179258abcdef"
REST_URL = f"https://appsignal.com/api/{APP_ID}/samples/{SAMPLE_ID}.json"

# A clearly-fake token; the hygiene guard ensures it never appears in connector source.
FAKE_TOKEN = "unit" "_test_token_not_real"

# --- Documented example payloads (trimmed) ---------------------------------

_VIEWER = {
    "data": {
        "viewer": {
            "organizations": [
                {
                    "id": "org_1",
                    "name": "Acme Inc",
                    "slug": "acme",
                    "apps": [
                        {"id": APP_ID, "name": "admin", "environment": "production"},
                        {"id": "app_staging", "name": "admin", "environment": "staging"},
                    ],
                }
            ]
        }
    }
}

_SEARCH = {
    "data": {
        "organization": {
            "search": [
                {
                    "id": SAMPLE_ID,
                    "time": 1719000000,
                    "action": "Avo::PeopleController#show",
                    "namespace": "web",
                    "exception": {"name": "ActiveRecord::RecordNotFound", "message": "Couldn't find Person"},
                    "incident": {"number": 4821},
                    "app": {"id": APP_ID, "name": "admin"},
                }
            ]
        }
    }
}

_SEARCH_EMPTY = {"data": {"organization": {"search": []}}}

_INCIDENTS = {
    "data": {
        "app": {
            "paginatedExceptionIncidents": {
                "total": 1,
                "rows": [
                    {
                        "number": 4821,
                        "exceptionName": "ActiveRecord::RecordNotFound",
                        "exceptionMessage": "Couldn't find Person with 'id'=999",
                        "actionNames": ["Avo::PeopleController#show"],
                        "count": 37,
                        "lastOccurredAt": "2026-06-28T10:00:00Z",
                        "state": "open",
                        "firstBacktraceLine": "app/controllers/avo/people_controller.rb:12",
                    }
                ],
            }
        }
    }
}

# REST sample detail — includes session_data, which MUST be dropped from output.
_SAMPLE = {
    "id": SAMPLE_ID,
    "time": 1719000000,
    "action": "Avo::PeopleController#show",
    "namespace": "web",
    "hostname": "web-1",
    "request_method": "GET",
    "path": "/avo/people/999",
    "incident_id": 4821,
    "exception": {
        "name": "ActiveRecord::RecordNotFound",
        "message": "Couldn't find Person with 'id'=999",
        "backtrace": [
            "app/controllers/avo/people_controller.rb:12:in `show'",
            "actionpack/lib/action_controller/metal/basic_implicit_render.rb:6:in `send_action'",
        ],
    },
    "params": '{"id": "999"}',
    "environment": {
        "REQUEST_METHOD": "GET",
        "REQUEST_PATH": "/avo/people/999",
        "HTTP_USER_AGENT": "Mozilla/5.0",
        "SERVER_NAME": "admin.example.com",
    },
    "session_data": {"_csrf_token": "SECRET-csrf", "user_id": 42},
}

_GQL_ERROR = {"errors": [{"message": "Not authorized for organization 'acme'"}], "data": None}


def _set_token():
    saved = os.environ.get("RC_CONN_APPSIGNAL")
    os.environ["RC_CONN_APPSIGNAL"] = FAKE_TOKEN
    return saved


def _restore_token(saved):
    if saved is None:
        os.environ.pop("RC_CONN_APPSIGNAL", None)
    else:
        os.environ["RC_CONN_APPSIGNAL"] = saved


class _Base(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = _set_token()

    def tearDown(self):
        _restore_token(self._saved)


class AppsignalManifest(_Base):
    def test_manifest_loads_and_maps_fields(self):
        m = api.load_manifests()
        self.assertIn("appsignal", m)
        am = m["appsignal"]
        self.assertEqual(am.base_url, "https://appsignal.com/graphql")
        self.assertEqual(am.auth.strategy, "query_param")
        self.assertEqual(am.auth.name, "token")
        self.assertEqual(am.pagination.style, "none")
        self.assertEqual(am.default_headers.get("Content-Type"), "application/json")

    def test_script_register_wins_over_yaml(self):
        api.register(aps.MANIFEST)
        self.assertIn("appsignal", api.MANIFESTS)
        self.assertNotIn("appsignal", api._YAML_LOADED_KEYS)
        api.load_manifests()
        self.assertIn("appsignal", api.MANIFESTS)
        self.assertNotIn("appsignal", api._YAML_LOADED_KEYS)


class AppsignalAuth(_Base):
    @responses.activate
    def test_token_in_query_param_on_graphql_post(self):
        responses.add(responses.POST, GRAPHQL_URL, json=_VIEWER, status=200)
        aps.list_apps()
        self.assertEqual(len(responses.calls), 1)
        self.assertIn(f"token={FAKE_TOKEN}", responses.calls[0].request.url)

    @responses.activate
    def test_token_in_query_param_on_rest_get(self):
        responses.add(responses.GET, REST_URL, json=_SAMPLE, status=200)
        aps.show(SAMPLE_ID)
        self.assertEqual(len(responses.calls), 1)
        self.assertIn(f"token={FAKE_TOKEN}", responses.calls[0].request.url)

    def test_missing_token_raises_loudly(self):
        _restore_token(None)  # clear it
        os.environ.pop("RC_CONN_APPSIGNAL", None)
        with self.assertRaises(RuntimeError) as ctx:
            aps._gql(aps._VIEWER_QUERY)
        self.assertIn("RC_CONN_APPSIGNAL", str(ctx.exception))

    @mock.patch("lib.connectors.appsignal.time.sleep", lambda *a, **k: None)
    @responses.activate
    def test_network_error_never_leaks_token(self):
        """A transient requests exception must not surface the token-bearing URL.

        The exception requests would raise embeds the full request URL (token and all); the
        connector must re-raise a sanitized ApiError instead, after exhausting retries.
        """
        leaky = requests.exceptions.ConnectionError(
            f"HTTPSConnectionPool: failed to reach {GRAPHQL_URL}?token={FAKE_TOKEN}"
        )
        for _ in range(aps.api.DEFAULT_MAX_RETRIES + 1):
            responses.add(responses.POST, GRAPHQL_URL, body=leaky)

        with self.assertRaises(aps.api.ApiError) as ctx:
            aps._gql(aps._VIEWER_QUERY)

        msg = str(ctx.exception)
        self.assertNotIn(FAKE_TOKEN, msg)
        self.assertNotIn("token=", msg)
        self.assertIn("network error", msg)


class AppsignalApps(_Base):
    @responses.activate
    def test_apps_flattens_orgs_to_apps(self):
        responses.add(responses.POST, GRAPHQL_URL, json=_VIEWER, status=200)
        result = aps.list_apps()
        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)
        first = result["items"][0]
        self.assertEqual(first["org_slug"], "acme")
        self.assertEqual(first["app_id"], APP_ID)
        self.assertEqual(first["environment"], "production")

    @responses.activate
    def test_apps_discovery_error_is_graceful(self):
        responses.add(responses.POST, GRAPHQL_URL, json=_GQL_ERROR, status=200)
        result = aps.list_apps()
        self.assertTrue(result["incomplete"])
        self.assertIn("--org", result["reason"])


class AppsignalSearch(_Base):
    @responses.activate
    def test_search_with_org_returns_sample_ids(self):
        responses.add(responses.POST, GRAPHQL_URL, json=_SEARCH, status=200)
        result = aps.search("340793FE", org="acme")
        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], SAMPLE_ID)
        self.assertEqual(result["items"][0]["org_slug"], "acme")
        # sampleType EXCEPTION must be sent in the variables
        body = json.loads(responses.calls[0].request.body)
        self.assertEqual(body["variables"]["sampleType"], "EXCEPTION")
        self.assertEqual(body["variables"]["query"], "340793FE")

    @responses.activate
    def test_search_without_org_discovers_then_searches(self):
        # First call: viewer discovery. Second call: org-wide search for the one discovered slug.
        responses.add(responses.POST, GRAPHQL_URL, json=_VIEWER, status=200)
        responses.add(responses.POST, GRAPHQL_URL, json=_SEARCH, status=200)
        result = aps.search("NoMethodError")
        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses.calls), 2)
        body = json.loads(responses.calls[1].request.body)
        self.assertEqual(body["variables"]["organizationSlug"], "acme")

    @responses.activate
    def test_search_empty_is_not_incomplete(self):
        responses.add(responses.POST, GRAPHQL_URL, json=_SEARCH_EMPTY, status=200)
        result = aps.search("Z9Z9Z9Z9", org="acme")
        self.assertFalse(result["incomplete"])
        self.assertEqual(result["items"], [])

    def test_reference_code_detection(self):
        self.assertTrue(aps.is_reference("A1B2C3D4"))
        self.assertFalse(aps.is_reference("NoMethodError"))
        self.assertFalse(aps.is_reference("A1B2C3D"))  # 7 chars


class AppsignalIncidents(_Base):
    @responses.activate
    def test_incidents_returns_rows(self):
        responses.add(responses.POST, GRAPHQL_URL, json=_INCIDENTS, status=200)
        result = aps.incidents("RecordNotFound", app=APP_ID, since="1w")
        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["count"], 37)
        body = json.loads(responses.calls[0].request.body)
        self.assertEqual(body["variables"]["appId"], APP_ID)
        self.assertEqual(body["variables"]["timeframe"], "R7D")

    def test_incidents_requires_app(self):
        result = aps.incidents("RecordNotFound", app="")
        self.assertTrue(result["incomplete"])
        self.assertIn("--app", result["reason"])

    @responses.activate
    def test_incidents_api_error_sets_incomplete(self):
        responses.add(responses.POST, GRAPHQL_URL, json=_GQL_ERROR, status=200)
        result = aps.incidents("X", app=APP_ID)
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["items"], [])


class AppsignalShow(_Base):
    @responses.activate
    def test_show_extracts_backtrace_params_env(self):
        responses.add(responses.GET, REST_URL, json=_SAMPLE, status=200)
        result = aps.show(SAMPLE_ID)
        self.assertEqual(result["exception"], "ActiveRecord::RecordNotFound")
        self.assertEqual(result["app_id"], APP_ID)  # derived from sample-id prefix
        self.assertEqual(len(result["backtrace"]), 2)
        self.assertEqual(result["params"], {"id": "999"})
        self.assertEqual(result["request_env"]["REQUEST_PATH"], "/avo/people/999")

    @responses.activate
    def test_show_redacts_session_data(self):
        responses.add(responses.GET, REST_URL, json=_SAMPLE, status=200)
        result = aps.show(SAMPLE_ID)
        blob = json.dumps(result)
        self.assertNotIn("session_data", blob)
        self.assertNotIn("SECRET-csrf", blob)

    def test_show_without_derivable_app_errors(self):
        result = aps.show("no_dash_sample_id")
        self.assertIn("error", result)


class AppsignalGraphQLError(_Base):
    @responses.activate
    def test_graphql_errors_array_raises(self):
        responses.add(responses.POST, GRAPHQL_URL, json=_GQL_ERROR, status=200)
        with self.assertRaises(api.ApiError) as ctx:
            aps._gql(aps._VIEWER_QUERY)
        self.assertIn("AppSignal GraphQL error", str(ctx.exception))
        self.assertIn("Not authorized", str(ctx.exception))


class AppsignalPick(_Base):
    @responses.activate
    def test_pick_narrows_incident_fields(self):
        responses.add(responses.POST, GRAPHQL_URL, json=_INCIDENTS, status=200)
        result = aps.incidents("RecordNotFound", app=APP_ID)
        picked = [api.pick(r, "exceptionName,count,state") for r in result["items"]]
        self.assertEqual(picked[0]["exceptionName"], "ActiveRecord::RecordNotFound")
        self.assertEqual(picked[0]["count"], 37)
        self.assertNotIn("firstBacktraceLine", picked[0])


class AppsignalCLI(_Base):
    def _run(self, argv):
        captured = io.StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            rc = aps.main(argv)
        finally:
            sys.stdout = old
        return rc, captured.getvalue()

    @responses.activate
    def test_cli_apps(self):
        responses.add(responses.POST, GRAPHQL_URL, json=_VIEWER, status=200)
        rc, out = self._run(["apps", "--pick", "org_slug,app_id,environment"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["items"][0]["app_id"], APP_ID)

    @responses.activate
    def test_cli_search(self):
        responses.add(responses.POST, GRAPHQL_URL, json=_SEARCH, status=200)
        rc, out = self._run(["search", "340793FE", "--org", "acme", "--pick", "id,exception.name"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["items"][0]["id"], SAMPLE_ID)

    @responses.activate
    def test_cli_incidents(self):
        responses.add(responses.POST, GRAPHQL_URL, json=_INCIDENTS, status=200)
        rc, out = self._run(["incidents", "RecordNotFound", "--app", APP_ID, "--pick", "exceptionName,count"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["items"][0]["count"], 37)

    @responses.activate
    def test_cli_show(self):
        responses.add(responses.GET, REST_URL, json=_SAMPLE, status=200)
        rc, out = self._run(["show", SAMPLE_ID, "--pick", "exception,action,backtrace"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["exception"], "ActiveRecord::RecordNotFound")
        self.assertNotIn("session_data", out)


class AppsignalTokenHygiene(unittest.TestCase):
    """CI guard: the fake test token must never appear in committed connector source."""

    def test_no_test_token_in_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "appsignal"
        offenders = []
        for path in connector_dir.rglob("*"):
            if path.is_file() and FAKE_TOKEN in path.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
