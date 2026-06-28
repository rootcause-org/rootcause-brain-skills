"""Fixture test for the manifest-ONLY Supabase integration — proves a catalogued connector with NO
bespoke Python is drivable end-to-end through ``lib.api``'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with ``responses``. Bodies are shaped from the Supabase
Management API documented example payloads (https://api.supabase.com/api/v1), trimmed to
support-relevant fields.

Most Supabase Management API list endpoints return a flat JSON array (no envelope), so the
manifest pagination style is ``none`` — every page fetch returns the full list in one call.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_supabase_connector.py -q
"""

import io
import json
import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

API = "https://api.supabase.com"
PROJECTS_URL = f"{API}/v1/projects"
PROJECT_REF = "abcdefghijklmnopqrst"
HEALTH_URL = f"{API}/v1/projects/{PROJECT_REF}/health"
FUNCTIONS_URL = f"{API}/v1/projects/{PROJECT_REF}/functions"
AUTH_CONFIG_URL = f"{API}/v1/projects/{PROJECT_REF}/config/auth"
ORGS_URL = f"{API}/v1/organizations"

# Documented example payloads trimmed to support-relevant fields.
_PROJECTS = [
    {
        "id": "123456789",
        "ref": PROJECT_REF,
        "organization_id": "org_abc123",
        "organization_slug": "acme",
        "name": "production",
        "region": "eu-central-1",
        "created_at": "2024-01-15T10:00:00.000Z",
        "status": "ACTIVE_HEALTHY",
        "database": {
            "host": f"db.{PROJECT_REF}.supabase.co",
            "version": "15.1.0.147",
            "postgres_engine": "15",
        },
    },
    {
        "id": "987654321",
        "ref": "zyxwvutsrqponmlkjihg",
        "organization_id": "org_abc123",
        "organization_slug": "acme",
        "name": "staging",
        "region": "eu-central-1",
        "created_at": "2024-02-01T08:00:00.000Z",
        "status": "ACTIVE_HEALTHY",
        "database": {
            "host": "db.zyxwvutsrqponmlkjihg.supabase.co",
            "version": "15.1.0.147",
            "postgres_engine": "15",
        },
    },
]

_HEALTH = [
    {"name": "auth", "healthy": True, "status": "ACTIVE_HEALTHY", "error": ""},
    {"name": "db", "healthy": True, "status": "ACTIVE_HEALTHY", "error": ""},
    {"name": "realtime", "healthy": True, "status": "ACTIVE_HEALTHY", "error": ""},
    {"name": "storage", "healthy": False, "status": "COMING_UP", "error": "service unavailable"},
    {"name": "functions", "healthy": True, "status": "ACTIVE_HEALTHY", "error": ""},
]

_FUNCTIONS = [
    {
        "id": "aaaaaaaaaaaaaaaaaaaaaaaa",
        "slug": "send-welcome-email",
        "name": "send-welcome-email",
        "status": "ACTIVE",
        "version": 3,
        "created_at": 1704067200000,
        "updated_at": 1706745600000,
        "verify_jwt": True,
        "import_map": False,
    },
    {
        "id": "bbbbbbbbbbbbbbbbbbbbbbbb",
        "slug": "process-webhook",
        "name": "process-webhook",
        "status": "ACTIVE",
        "version": 1,
        "created_at": 1704153600000,
        "updated_at": 1704153600000,
        "verify_jwt": False,
        "import_map": False,
    },
]

_AUTH_CONFIG = {
    "site_url": "https://app.example.com",
    "disable_signup": False,
    "jwt_exp": 3600,
    "mailer_autoconfirm": False,
    "sms_autoconfirm": False,
    "external_email_enabled": True,
    "external_phone_enabled": False,
}

_ORGS = [
    {"id": "org_abc123", "name": "Acme Corp", "slug": "acme"},
]


class SupabaseManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates 'supabase' (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_SUPABASE")
        # Token prefix split so the hygiene guard in this very file doesn't flag itself.
        os.environ["RC_CONN_SUPABASE"] = "sbp_" + "test_personal_access_token_fixture"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_SUPABASE", None)
        else:
            os.environ["RC_CONN_SUPABASE"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        """YAML loads cleanly and maps every field the rest of the tests depend on."""
        m = api.load_manifests()
        self.assertIn("supabase", m)
        s = m["supabase"]
        self.assertEqual(s.key, "supabase")
        self.assertEqual(s.base_url, "https://api.supabase.com")
        self.assertEqual(s.auth.strategy, "bearer")
        self.assertEqual(s.pagination.style, "none")
        self.assertEqual(s.rate_limit_remaining_header, "")  # no remaining header

    @responses.activate
    def test_bearer_credential_on_every_request(self):
        """The PAT rides the Authorization header on each request, never leaked to query string."""
        responses.add(responses.GET, PROJECTS_URL, json=_PROJECTS, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["supabase"])
        result = c.get("v1/projects")

        self.assertEqual(len(result), 2)
        self.assertEqual(responses.calls[0].request.headers["Authorization"],
                         "Bearer sbp_" + "test_personal_access_token_fixture")
        # Credential must NOT appear in the query string.
        self.assertNotIn("sbp_", responses.calls[0].request.url)

    @responses.activate
    def test_single_page_returns_full_array(self):
        """pagination=none: a single fetch returns the full list as-is; collect works too."""
        responses.add(responses.GET, PROJECTS_URL, json=_PROJECTS, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["supabase"])
        result = c.collect("v1/projects")

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)
        # Flat array — items ARE the page body; no envelope unwrapping needed.
        refs = [it["ref"] for it in result["items"]]
        self.assertIn(PROJECT_REF, refs)
        # Only one HTTP call made (style=none stops after the first page).
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_pick_selects_support_relevant_project_fields(self):
        """api.pick narrows the big project object to the handful support needs."""
        responses.add(responses.GET, PROJECTS_URL, json=_PROJECTS, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["supabase"])
        result = c.collect("v1/projects")

        picked = [api.pick(it, "ref,name,region,status") for it in result["items"]]
        self.assertEqual(picked[0]["ref"], PROJECT_REF)
        self.assertEqual(picked[0]["name"], "production")
        self.assertEqual(picked[0]["status"], "ACTIVE_HEALTHY")
        # Nested field not in pick must be absent.
        self.assertNotIn("database", picked[0])

    @responses.activate
    def test_health_endpoint_returns_service_array(self):
        """Health endpoint returns a bare array of service statuses; unhealthy services visible."""
        responses.add(responses.GET, HEALTH_URL, json=_HEALTH, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["supabase"])
        body = c.get(f"v1/projects/{PROJECT_REF}/health")

        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 5)
        unhealthy = [s for s in body if not s["healthy"]]
        self.assertEqual(len(unhealthy), 1)
        self.assertEqual(unhealthy[0]["name"], "storage")

        picked = [api.pick(s, "name,healthy,status,error") for s in body]
        self.assertEqual(picked[0]["name"], "auth")
        self.assertTrue(picked[0]["healthy"])

    @responses.activate
    def test_functions_endpoint(self):
        """Edge Functions list returns bare array; pick selects relevant function metadata."""
        responses.add(responses.GET, FUNCTIONS_URL, json=_FUNCTIONS, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["supabase"])
        result = c.collect(f"v1/projects/{PROJECT_REF}/functions")

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 2)

        picked = [api.pick(fn, "slug,name,status,version,verify_jwt") for fn in result["items"]]
        self.assertEqual(picked[0]["slug"], "send-welcome-email")
        self.assertEqual(picked[0]["status"], "ACTIVE")
        self.assertEqual(picked[0]["version"], 3)
        self.assertTrue(picked[0]["verify_jwt"])

    @responses.activate
    def test_auth_config_endpoint(self):
        """Auth config endpoint returns a single object (not a list)."""
        responses.add(responses.GET, AUTH_CONFIG_URL, json=_AUTH_CONFIG, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["supabase"])
        body = c.get(f"v1/projects/{PROJECT_REF}/config/auth")

        self.assertIsInstance(body, dict)
        self.assertEqual(body["site_url"], "https://app.example.com")
        self.assertFalse(body["disable_signup"])

        picked = api.pick(body, "site_url,disable_signup,jwt_exp,mailer_autoconfirm")
        self.assertEqual(picked["jwt_exp"], 3600)

    @responses.activate
    def test_cli_drives_supabase_with_bearer(self):
        """CLI `python -m lib.api get supabase …` works with no bespoke code."""
        responses.add(responses.GET, PROJECTS_URL, json=_PROJECTS, status=200)

        rc = api._main([
            "get", "supabase", "v1/projects",
            "--pick", "ref,name,status",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses.calls), 1)
        self.assertEqual(responses.calls[0].request.headers["Authorization"],
                         "Bearer sbp_" + "test_personal_access_token_fixture")

    @responses.activate
    def test_cli_paginate_flag_collects_all_items(self):
        """--paginate with style=none still returns the single page's items in the collect envelope."""
        responses.add(responses.GET, PROJECTS_URL, json=_PROJECTS, status=200)

        # Capture stdout
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = api._main([
                "get", "supabase", "v1/projects",
                "--paginate",
                "--pick", "ref,status",
            ])
        self.assertEqual(rc, 0)
        output = json.loads(buf.getvalue())
        self.assertIn("items", output)
        self.assertEqual(len(output["items"]), 2)
        self.assertFalse(output["incomplete"])

    @responses.activate
    def test_orgs_endpoint(self):
        """Organizations list returns bare array."""
        responses.add(responses.GET, ORGS_URL, json=_ORGS, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["supabase"])
        body = c.get("v1/organizations")

        self.assertIsInstance(body, list)
        self.assertEqual(body[0]["slug"], "acme")


class SupabaseCassetteHygiene(unittest.TestCase):
    """CI guard: no real Supabase PAT prefix may land in the connector dir.

    Scopes to the connector dir (manifest), NOT this test file — the test legitimately
    names the prefixes it checks, so scanning itself would be a false positive.
    """

    # Supabase PAT prefix: `sbp_` (split to avoid triggering itself)
    _TOKEN_PREFIXES = ("sbp" "_",)

    def test_no_token_prefixes_in_supabase_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "supabase"
        offenders = []
        for path in sorted(connector_dir.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains {pref}")
        self.assertEqual(offenders, [], f"token-like material in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
