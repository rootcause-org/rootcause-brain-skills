"""Fixture tests for the Meta Ads connector.

Covers what would silently rot: the manifest's multi-field credential contract, the JSON-object /
individual-env-var credential parsing, `paging.next` following with the Graph host pinned, the
actions[] flattening and the cents→currency budget conversion.

No live creds, no network — all HTTP is mocked with `responses` and the credential is a fake
RC_CONN_META_ADS JSON object set by the test.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \
        pytest tests/test_meta_ads_connector.py -q
"""

import json
import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import _fields, meta_ads  # noqa: E402

BASE = "https://graph.facebook.com/v24.0"
ACCOUNT = "act_1234567890"
CRED = '{"META_ACCESS_TOKEN":"EAAtest-system-user","META_AD_ACCOUNT_ID":"act_1234567890"}'


class _Creds(unittest.TestCase):
    """Set the injected multi-field credential for the duration of one test class."""

    cred = CRED

    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("RC_CONN_META_ADS", "META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID")}
        for k in self._saved:
            os.environ.pop(k, None)
        if self.cred is not None:
            os.environ["RC_CONN_META_ADS"] = self.cred

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class MetaAdsManifest(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loads(self):
        m = api.load_manifests()["meta_ads"]
        self.assertEqual(m.base_url, BASE)
        self.assertEqual(m.auth.strategy, "bearer")
        self.assertEqual(m.pagination.style, "none")  # the connector owns paging.next
        self.assertEqual(m.allowed_post_paths, ())    # read-only: GET only

    def test_manifest_declares_the_credential_fields(self):
        import yaml

        raw = yaml.safe_load(
            (Path(meta_ads.__file__).parent / "manifest.yaml").read_text(encoding="utf-8"))
        self.assertEqual(raw["credential_exposure"], "env")
        self.assertEqual(raw["env_var"], "RC_CONN_META_ADS")
        self.assertEqual([f["key"] for f in raw["token_fields"]], list(meta_ads.FIELD_NAMES))
        self.assertTrue(raw["token_fields"][0]["secret"])
        self.assertFalse(raw["token_fields"][1]["secret"])


class MetaAdsCredentials(_Creds):
    def test_json_object_is_parsed(self):
        self.assertEqual(meta_ads.default_account(), ACCOUNT)

    def test_individual_env_vars_are_the_fallback(self):
        os.environ.pop("RC_CONN_META_ADS")
        os.environ["META_ACCESS_TOKEN"] = "EAAlocal"
        os.environ["META_AD_ACCOUNT_ID"] = "9876543210"  # bare id is normalized to act_…
        self.assertEqual(meta_ads.default_account(), "act_9876543210")

    def test_missing_field_error_names_the_key_not_the_value(self):
        os.environ["RC_CONN_META_ADS"] = '{"META_ACCESS_TOKEN":"EAAsupersecret"}'
        with self.assertRaises(RuntimeError) as ctx:
            meta_ads.default_account()
        self.assertIn("META_AD_ACCOUNT_ID", str(ctx.exception))
        self.assertNotIn("EAAsupersecret", str(ctx.exception))

    def test_no_credential_at_all_names_the_connection(self):
        os.environ.pop("RC_CONN_META_ADS")
        with self.assertRaises(RuntimeError) as ctx:
            _fields.fields("meta_ads", meta_ads.FIELD_NAMES)
        self.assertIn("meta_ads", str(ctx.exception))


class MetaAdsReads(_Creds):
    @responses_lib.activate
    def test_campaign_row_shape(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/{ACCOUNT}/campaigns", json={"data": [
            {"id": "1", "name": "TRAFFIC - zomer - wave 3 - 2026", "effective_status": "ACTIVE",
             "objective": "OUTCOME_TRAFFIC", "start_time": "2026-06-01T00:00:00+0200",
             "daily_budget": "1550"},
        ]})
        data = meta_ads.get(f"/{ACCOUNT}/campaigns", {"fields": "id"})
        row = data["data"][0]
        self.assertEqual(meta_ads.budget(row), "15.50/day")
        self.assertEqual(row["objective"].replace("OUTCOME_", ""), "TRAFFIC")

    @responses_lib.activate
    def test_bearer_auth_and_cursor_paging_never_leak_the_token(self):
        # Graph's paging.next embeds ?access_token=<secret>. We must use it only as a "there is
        # more" signal, page via cursors.after on the same endpoint, and never return/print it.
        leaky_next = f"{BASE}/{ACCOUNT}/insights?after=CURSOR2&access_token=EAAtest-system-user"
        responses_lib.add(responses_lib.GET, f"{BASE}/{ACCOUNT}/insights", json={
            "data": [{"date_start": "2026-08-01", "spend": "10", "clicks": "5",
                      "actions": [{"action_type": "link_click", "value": "4"},
                                  {"action_type": "landing_page_view", "value": "3"}]}],
            "paging": {"cursors": {"before": "CURSOR1", "after": "CURSOR2"}, "next": leaky_next},
        })
        responses_lib.add(responses_lib.GET, f"{BASE}/{ACCOUNT}/insights", json={
            "data": [{"date_start": "2026-08-02", "spend": "20", "clicks": "9",
                      "actions": [{"action_type": "link_click", "value": "8"}]}],
            "paging": {"cursors": {"before": "CURSOR2", "after": "CURSOR3"}},
        })

        args = type("A", (), {"account": ACCOUNT, "json": False, "csv": False, "since": None,
                              "until": None, "preset": "last_30d", "increment": "all",
                              "breakdown": None, "filter": None, "all_actions": False})()
        data = meta_ads.fetch_insights(args, "account", meta_ads.INSIGHT_FIELDS)

        self.assertEqual(len(data["data"]), 2)
        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], "Bearer EAAtest-system-user")
            self.assertNotIn("access_token", call.request.url)
        # page 2 was requested with the cursor, not by following the leaky next URL
        self.assertIn("after=CURSOR2", responses_lib.calls[1].request.url)
        # nothing that reaches --json output carries the token or the paging envelope
        self.assertNotIn("paging", data)
        dumped = json.dumps(data)
        self.assertNotIn("access_token", dumped)
        self.assertNotIn("EAAtest-system-user", dumped)

        rows = meta_ads.insight_rows(data["data"], ["date_start", "spend", "clicks"], False)
        self.assertEqual(rows[0], ["DATE_START", "SPEND", "CLICKS",
                                   "action:link_click", "action:landing_page_view"])
        self.assertEqual(rows[1], ["2026-08-01", "10.00", "5", "4", "3"])
        self.assertEqual(rows[2][-1], "")  # page-2 row has no landing_page_view

    @responses_lib.activate
    def test_single_page_response_also_drops_paging(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/me/accounts", json={
            "data": [{"id": "1", "name": "Page"}],
            "paging": {"next": f"{BASE}/me/accounts?access_token=EAAtest-system-user"}})
        data = meta_ads.get("/me/accounts", {"fields": "id,name"}, paginate=False)
        self.assertNotIn("paging", data)

    @responses_lib.activate
    def test_filtering_is_json_encoded(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/{ACCOUNT}/insights", json={"data": []})
        args = type("A", (), {"account": ACCOUNT, "json": False, "csv": False, "since": None,
                              "until": None, "preset": "last_30d", "increment": "all",
                              "breakdown": None, "all_actions": False,
                              "filter": ['campaign.name==He said "hi"', "campaign.id==1,2"]})()
        params = meta_ads.insight_params(args, "campaign", ["spend"])
        self.assertEqual(json.loads(params["filtering"]), [
            {"field": "campaign.name", "operator": "EQUAL", "value": 'He said "hi"'},
            {"field": "campaign.id", "operator": "IN", "value": ["1", "2"]},
        ])

    @responses_lib.activate
    def test_all_actions_widens_the_columns(self):
        rows = meta_ads.insight_rows(
            [{"spend": "1", "actions": [{"action_type": "omni_purchase", "value": "2"}]}],
            ["spend"], True)
        self.assertIn("action:omni_purchase", rows[0])

    @responses_lib.activate
    def test_graph_error_body_becomes_an_actionable_message(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/me", status=400, json={
            "error": {"code": 190, "message": "Invalid OAuth access token."}})
        with self.assertRaises(RuntimeError) as ctx:
            meta_ads.get("/me", paginate=False)
        self.assertIn("Graph 190", str(ctx.exception))
        self.assertIn("Invalid OAuth access token", str(ctx.exception))


class MetaAdsHostPinning(_Creds):
    def test_api_refuses_a_foreign_host(self):
        args = type("A", (), {"method": "GET", "url": "https://evil.example.com/act_1/insights",
                              "param": None, "no_paging": True, "json": False, "csv": False,
                              "account": ACCOUNT})()
        with self.assertRaises(RuntimeError) as ctx:
            meta_ads.cmd_api(args)
        self.assertIn("graph.facebook.com", str(ctx.exception))

    def test_api_refuses_a_write_verb(self):
        args = type("A", (), {"method": "POST", "url": "/act_1/campaigns", "param": None,
                              "no_paging": True, "json": False, "csv": False, "account": ACCOUNT})()
        with self.assertRaises(RuntimeError) as ctx:
            meta_ads.cmd_api(args)
        self.assertIn("read-only", str(ctx.exception))

    @responses_lib.activate
    def test_a_foreign_paging_next_is_never_followed(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/{ACCOUNT}/campaigns", json={
            "data": [{"id": "1"}],
            "paging": {"cursors": {"after": "C1"}, "next": "https://evil.example.com/steal"}})
        responses_lib.add(responses_lib.GET, f"{BASE}/{ACCOUNT}/campaigns", json={"data": []})

        meta_ads.get(f"/{ACCOUNT}/campaigns")

        for call in responses_lib.calls:
            self.assertIn("graph.facebook.com", call.request.url)

    def test_refusal_message_omits_the_rejected_urls_query_string(self):
        with self.assertRaises(RuntimeError) as ctx:
            meta_ads._pin("https://evil.example.com/steal?access_token=EAAsupersecret")
        self.assertNotIn("EAAsupersecret", str(ctx.exception))
        self.assertNotIn("steal", str(ctx.exception))
        self.assertIn("evil.example.com", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
