"""Fixture tests for the Google Analytics 4 + Search Console connector.

Covers what would silently rot: the manifest's multi-field credential + POST read allowlist, the
self-managed refresh-token exchange (minted once, then reused), the GA4 runReport body shape the
presets build, GSC's absolute-date requirement, and the refusal of any POST outside the allowlist.

No live creds, no network — all HTTP is mocked with `responses` and the credential is a fake
RC_CONN_GOOGLE_ANALYTICS JSON object set by the test.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \
        pytest tests/test_google_analytics_connector.py -q
"""

import json
import os
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import google_analytics as ga  # noqa: E402

PROPERTY = "123456789"
SITE = "sc-domain:example.com"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REPORT_URL = f"https://analyticsdata.googleapis.com/v1beta/properties/{PROPERTY}:runReport"
GSC_QUERY_URL = ("https://searchconsole.googleapis.com/webmasters/v3/sites/"
                 "sc-domain%3Aexample.com/searchAnalytics/query")
CRED = json.dumps({
    "GWS_REFRESH_TOKEN": "1//refresh-secret",
    "GWS_CLIENT_ID": "123-abc.apps.googleusercontent.com",
    "GWS_CLIENT_SECRET": "GOCSPX-client-secret",
    "GA_PROPERTY_ID": PROPERTY,
    "GSC_SITE": SITE,
})

_ENV_KEYS = ("RC_CONN_GOOGLE_ANALYTICS",) + ga.FIELD_NAMES


def _args(**kw):
    defaults = {"json": False, "csv": False, "property": None, "site": None, "limit": 10,
                "filter": None, "order_by": None, "start": "28daysAgo", "end": "today",
                "type": "web", "dimensions": None, "metrics": None}
    defaults.update(kw)
    return type("A", (), defaults)()


class _Creds(unittest.TestCase):
    cred = CRED

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        if self.cred is not None:
            os.environ["RC_CONN_GOOGLE_ANALYTICS"] = self.cred
        ga.reset_token_cache()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ga.reset_token_cache()

    @staticmethod
    def mock_token(expires_in: int = 3600):
        responses_lib.add(responses_lib.POST, TOKEN_URL,
                          json={"access_token": "ya29.fake", "expires_in": expires_in})


class GoogleAnalyticsManifest(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loads_with_the_read_post_allowlist(self):
        m = api.load_manifests()["google_analytics"]
        self.assertEqual(m.base_url, "https://analyticsdata.googleapis.com/v1beta")
        self.assertEqual(m.auth.strategy, "bearer")
        self.assertIn("/v1beta/properties/*:runReport", m.allowed_post_paths)
        self.assertIn("/webmasters/v3/sites/*/searchAnalytics/query", m.allowed_post_paths)
        self.assertNotIn("/v1beta/properties/*", m.allowed_post_paths)

    def test_manifest_declares_five_fields_with_gsc_optional(self):
        import yaml

        raw = yaml.safe_load(
            (Path(ga.__file__).parent / "manifest.yaml").read_text(encoding="utf-8"))
        self.assertEqual(raw["credential_exposure"], "env")
        self.assertEqual(raw["env_var"], "RC_CONN_GOOGLE_ANALYTICS")
        fields = {f["key"]: f for f in raw["token_fields"]}
        self.assertEqual(list(fields), list(ga.FIELD_NAMES))
        self.assertTrue(fields["GWS_REFRESH_TOKEN"]["secret"])
        self.assertTrue(fields["GWS_CLIENT_SECRET"]["secret"])
        self.assertFalse(fields["GWS_CLIENT_ID"]["secret"])
        self.assertTrue(fields["GSC_SITE"].get("optional"))
        self.assertEqual(sorted(raw["egress_hosts"]),
                         ["analyticsadmin.googleapis.com", "analyticsdata.googleapis.com",
                          "oauth2.googleapis.com", "searchconsole.googleapis.com"])


class GoogleAnalyticsCredentials(_Creds):
    def test_json_object_is_parsed(self):
        self.assertEqual(ga.default_property(), PROPERTY)
        self.assertEqual(ga.default_site(), SITE)

    def test_individual_env_vars_are_the_fallback(self):
        os.environ.pop("RC_CONN_GOOGLE_ANALYTICS")
        os.environ["GA_PROPERTY_ID"] = "999"
        os.environ["GWS_REFRESH_TOKEN"] = "1//local"
        self.assertEqual(ga.default_property(), "999")

    def test_missing_field_error_names_the_key_not_the_value(self):
        os.environ["RC_CONN_GOOGLE_ANALYTICS"] = json.dumps(
            {"GWS_REFRESH_TOKEN": "1//supersecret", "GA_PROPERTY_ID": PROPERTY})
        with self.assertRaises(RuntimeError) as ctx:
            ga.access_token()
        self.assertIn("GWS_CLIENT_ID", str(ctx.exception))
        self.assertNotIn("supersecret", str(ctx.exception))


class GoogleAnalyticsToken(_Creds):
    @responses_lib.activate
    def test_token_is_minted_once_and_reused(self):
        self.mock_token()
        responses_lib.add(responses_lib.POST, REPORT_URL, json={"rows": []})
        responses_lib.add(responses_lib.POST, REPORT_URL, json={"rows": []})

        ga.run_report(_args(), ["date"], ["sessions"])
        ga.run_report(_args(), ["date"], ["sessions"])

        token_calls = [c for c in responses_lib.calls if c.request.url.startswith(TOKEN_URL)]
        self.assertEqual(len(token_calls), 1)
        self.assertIn("grant_type=refresh_token", token_calls[0].request.body)
        for call in responses_lib.calls[1:]:
            self.assertEqual(call.request.headers["Authorization"], "Bearer ya29.fake")

    @responses_lib.activate
    def test_invalid_grant_is_actionable(self):
        responses_lib.add(responses_lib.POST, TOKEN_URL, status=400,
                          json={"error": "invalid_grant",
                                "error_description": "Token has been expired or revoked."})
        with self.assertRaises(RuntimeError) as ctx:
            ga.access_token()
        message = str(ctx.exception)
        self.assertIn("invalid_grant", message)
        self.assertIn("mint", message)


class GoogleAnalyticsReports(_Creds):
    @responses_lib.activate
    def test_ga_overview_report_body(self):
        self.mock_token()
        responses_lib.add(responses_lib.POST, REPORT_URL, json={
            "dimensionHeaders": [{"name": "date"}],
            "metricHeaders": [{"name": "sessions"}, {"name": "engagementRate"}],
            "rows": [{"dimensionValues": [{"value": "20260901"}],
                      "metricValues": [{"value": "120"}, {"value": "0.6234"}]}],
            "totals": [{"metricValues": [{"value": "120"}, {"value": "0.6234"}]}],
        })
        args = _args(start="2026-08-01", end="2026-08-31", limit=60)
        ga.preset("date", "sessions,engagementRate", "date", 60)(args)

        body = json.loads(responses_lib.calls[-1].request.body)
        self.assertEqual(body["dateRanges"], [{"startDate": "2026-08-01", "endDate": "2026-08-31"}])
        self.assertEqual(body["dimensions"], [{"name": "date"}])
        self.assertEqual(body["metrics"], [{"name": "sessions"}, {"name": "engagementRate"}])
        self.assertEqual(body["orderBys"], [{"dimension": {"dimensionName": "date"}, "desc": False}])
        self.assertEqual(body["metricAggregations"], ["TOTAL"])

    @responses_lib.activate
    def test_ga_landing_filter_and_rate_formatting(self):
        self.mock_token()
        responses_lib.add(responses_lib.POST, REPORT_URL, json={
            "dimensionHeaders": [{"name": "landingPage"}],
            "metricHeaders": [{"name": "sessions"}, {"name": "engagementRate"}],
            "rows": [{"dimensionValues": [{"value": "/pricing"}],
                      "metricValues": [{"value": "40"}, {"value": "0.5"}]}],
        })
        args = _args(filter=["landingPage=*pricing"], limit=25)
        ga.preset("landingPage", "sessions,engagementRate", "sessions:desc", 25)(args)

        body = json.loads(responses_lib.calls[-1].request.body)
        self.assertEqual(body["dimensionFilter"], {"filter": {
            "fieldName": "landingPage",
            "stringFilter": {"matchType": "CONTAINS", "value": "pricing"}}})
        self.assertEqual(body["orderBys"], [{"metric": {"metricName": "sessions"}, "desc": True}])
        self.assertEqual(ga.fmt_metric("engagementRate", "0.5"), "50.0%")


class GoogleSearchConsole(_Creds):
    @responses_lib.activate
    def test_relative_dates_become_absolute(self):
        self.mock_token()
        responses_lib.add(responses_lib.POST, GSC_QUERY_URL, json={"rows": []})

        ga.gsc_query(_args(start="7daysAgo", end="3daysAgo", limit=25), ["query"])

        body = json.loads(responses_lib.calls[-1].request.body)
        self.assertEqual(body["startDate"], (date.today() - timedelta(days=7)).isoformat())
        self.assertEqual(body["endDate"], (date.today() - timedelta(days=3)).isoformat())
        self.assertEqual(body["dimensions"], ["query"])
        self.assertEqual(body["rowLimit"], 25)

    @responses_lib.activate
    def test_filter_group_shape(self):
        self.mock_token()
        responses_lib.add(responses_lib.POST, GSC_QUERY_URL, json={"rows": []})

        ga.gsc_query(_args(filter=["page:contains:/pricing/"], limit=10), ["query", "page"])

        body = json.loads(responses_lib.calls[-1].request.body)
        self.assertEqual(body["dimensionFilterGroups"], [{"groupType": "and", "filters": [
            {"dimension": "page", "operator": "contains", "expression": "/pricing/"}]}])

    def test_bad_date_is_rejected_before_any_call(self):
        with self.assertRaises(RuntimeError):
            ga.abs_date("last week")


class GoogleMethodPolicy(_Creds):
    @responses_lib.activate
    def test_post_outside_the_allowlist_is_refused(self):
        self.mock_token()
        with self.assertRaises(api.MethodPolicyError) as ctx:
            ga.call("POST", ga.ADMIN_HOST, "/v1beta/properties/123/dataStreams",
                    body={"displayName": "x"})
        self.assertIn("action plane", str(ctx.exception))

    @responses_lib.activate
    def test_write_verb_is_refused(self):
        self.mock_token()
        with self.assertRaises(api.MethodPolicyError):
            ga.call("DELETE", ga.ADMIN_HOST, "/v1beta/properties/123")

    def test_api_escape_hatch_refuses_a_foreign_host(self):
        with self.assertRaises(RuntimeError) as ctx:
            ga._resolve("https://evil.example.com/v1beta/properties/1:runReport")
        self.assertIn("analyticsdata.googleapis.com", str(ctx.exception))

    def test_api_escape_hatch_maps_prefixes_to_hosts(self):
        self.assertEqual(ga._resolve("/webmasters/v3/sites")[0], ga.GSC_HOST)
        self.assertEqual(ga._resolve(f"/v1beta/properties/{PROPERTY}/metadata")[0], ga.DATA_HOST)
        self.assertEqual(ga._resolve("/v1beta/accountSummaries")[0], ga.ADMIN_HOST)


if __name__ == "__main__":
    unittest.main()
