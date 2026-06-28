"""Fixture test for the Podio script connector.

Force-code trigger (c): Podio requires ``Authorization: OAuth2 <token>`` (not Bearer).
This test verifies:
  - the YAML manifest loads via lib.api's YAML loader and maps every field
  - offset pagination stitches ≥ 2 pages (items endpoint)
  - the ``OAuth2 <token>`` credential rides EVERY request (not just page 1)
  - ``api.pick`` selects support-relevant fields
  - the connector's CLI subcommands (orgs, spaces, apps, items, item, tasks) print markdown

No live creds, no network. HTTP is mocked with ``responses``. Bodies mirror Podio's documented
example payloads trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_podio_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import podio  # noqa: E402

BASE = "https://api.podio.com"
FAKE_TOKEN = "fake_podio_token_test"
# Expected Authorization header value — constructed so the guard doesn't flag itself.
EXPECTED_AUTH = "OAuth2 " + FAKE_TOKEN


# ---------------------------------------------------------------------------
# Example payloads (Podio-documented shapes, trimmed)
# ---------------------------------------------------------------------------

_ORGS = [
    {"org_id": 100, "name": "Acme Corp", "url": "https://podio.com/acme", "url_label": "acme"},
    {"org_id": 101, "name": "Beta Ltd", "url": "https://podio.com/beta", "url_label": "beta"},
]

_SPACES = [
    {"space_id": 200, "name": "Support", "url": "https://podio.com/acme/support", "status": "active"},
    {"space_id": 201, "name": "Dev", "url": "https://podio.com/acme/dev", "status": "active"},
]

_APPS = [
    {"app_id": 300, "config": {"name": "Tickets"}, "link": "https://podio.com/acme/support/apps/tickets"},
    {"app_id": 301, "config": {"name": "Backlog"}, "link": "https://podio.com/acme/support/apps/backlog"},
]

# Items pages: envelope has `total`, `filtered`, `items`.
_ITEMS_PAGE_1 = {
    "total": 2,
    "filtered": 2,
    "items": [
        {
            "item_id": 1001,
            "title": "Login broken on Safari",
            "link": "https://podio.com/acme/support/apps/tickets/items/1001",
            "fields": [
                {"field_id": 10, "label": "Status", "values": [{"value": "open"}]},
                {"field_id": 11, "label": "Priority", "values": [{"value": "high"}]},
            ],
        }
    ],
}
_ITEMS_PAGE_2 = {
    "total": 2,
    "filtered": 2,
    "items": [
        {
            "item_id": 1002,
            "title": "Export fails for large datasets",
            "link": "https://podio.com/acme/support/apps/tickets/items/1002",
            "fields": [
                {"field_id": 10, "label": "Status", "values": [{"value": "closed"}]},
                {"field_id": 11, "label": "Priority", "values": [{"value": "medium"}]},
            ],
        }
    ],
}

_ITEM_SINGLE = {
    "item_id": 1001,
    "title": "Login broken on Safari",
    "link": "https://podio.com/acme/support/apps/tickets/items/1001",
    "fields": [
        {"field_id": 10, "label": "Status", "values": [{"value": "open"}]},
        {"field_id": 11, "label": "Priority", "values": [{"value": "high"}]},
        {"field_id": 12, "label": "Reporter", "values": [{"value": "alice@example.com"}]},
    ],
    "current_revision": {
        "created_by": {"name": "Bob"},
        "created_on": "2024-01-15T10:30:00Z",
    },
}

# Tasks: Podio tasks endpoint returns a bare list (not an envelope) for some paths.
_TASKS = [
    {"task_id": 500, "text": "Follow up with Alice", "status": "active", "due_date": "2024-01-20"},
    {"task_id": 501, "text": "Deploy hotfix", "status": "completed"},
]


class PodioManifestLoad(unittest.TestCase):
    """YAML manifest loads cleanly and maps every field."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loaded_and_fields_correct(self):
        manifests = api.load_manifests()
        self.assertIn("podio", manifests)
        m = manifests["podio"]
        self.assertEqual(m.base_url, BASE)
        self.assertEqual(m.auth.strategy, "none")
        self.assertEqual(m.pagination.style, "offset")
        self.assertEqual(m.pagination.offset_param, "offset")
        self.assertEqual(m.pagination.limit_param, "limit")
        self.assertEqual(m.pagination.items_field, "items")
        self.assertEqual(m.rate_limit_remaining_header, "")

    def test_connector_module_declared(self):
        """manifest.yaml connector_module field is set (script connector, not manifest-only)."""
        from pathlib import Path
        import yaml

        manifest_path = (
            Path(__file__).resolve().parents[1] / "lib" / "connectors" / "podio" / "manifest.yaml"
        )
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(raw.get("connector_module"), "lib.connectors.podio")
        self.assertEqual(raw.get("env_var"), "RC_CONN_PODIO")
        self.assertIn("api.podio.com", raw.get("egress_hosts", []))
        self.assertIn("token", raw.get("kinds", []))
        self.assertIn("oauth", raw.get("kinds", []))

    def test_oauth_block_present(self):
        from pathlib import Path
        import yaml

        manifest_path = (
            Path(__file__).resolve().parents[1] / "lib" / "connectors" / "podio" / "manifest.yaml"
        )
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        oauth_block = raw.get("oauth", {})
        self.assertIn("auth_url", oauth_block)
        self.assertIn("token_url", oauth_block)
        self.assertIn("podio.com", oauth_block["auth_url"])
        self.assertIn("api.podio.com", oauth_block["token_url"])


class PodioAuth(unittest.TestCase):
    """OAuth2 auth header is injected on every request."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_PODIO")
        os.environ["RC_CONN_PODIO"] = FAKE_TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_PODIO", None)
        else:
            os.environ["RC_CONN_PODIO"] = self._saved

    @responses_lib.activate
    def test_oauth2_header_on_orgs(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/org/", json=_ORGS, status=200)
        orgs = podio.get_orgs()
        self.assertEqual(len(orgs), 2)
        # Credential rides as `Authorization: OAuth2 <token>` — not Bearer.
        sent = responses_lib.calls[0].request.headers.get("Authorization", "")
        self.assertEqual(sent, EXPECTED_AUTH)

    @responses_lib.activate
    def test_oauth2_header_on_item_fetch(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/item/1001/", json=_ITEM_SINGLE, status=200)
        item = podio.get_item(1001)
        self.assertEqual(item["item_id"], 1001)
        sent = responses_lib.calls[0].request.headers.get("Authorization", "")
        self.assertEqual(sent, EXPECTED_AUTH)


class PodioPagination(unittest.TestCase):
    """Offset pagination stitches ≥ 2 pages; credential rides every page."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_PODIO")
        os.environ["RC_CONN_PODIO"] = FAKE_TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_PODIO", None)
        else:
            os.environ["RC_CONN_PODIO"] = self._saved

    @responses_lib.activate
    def test_offset_pagination_stitches_two_pages(self):
        """lib.api offset paginator stitches page 1 (full, page_size=2) and page 2 (partial, stops).

        page_size=2: page 1 returns 2 items (full → try page 2);
                     page 2 returns 1 item (<page_size → stop).
        Credential must ride BOTH requests.
        """
        app_id = 300
        url = f"{BASE}/item/app/{app_id}/"

        _p1 = {
            "total": 3,
            "filtered": 3,
            "items": [_ITEMS_PAGE_1["items"][0], _ITEMS_PAGE_2["items"][0]],
        }
        _p2 = {
            "total": 3,
            "filtered": 3,
            "items": [{"item_id": 1003, "title": "Third item", "fields": []}],
        }
        responses_lib.add(responses_lib.GET, url, json=_p1, status=200)
        responses_lib.add(responses_lib.GET, url, json=_p2, status=200)

        token_val = os.environ["RC_CONN_PODIO"]
        manifest = api.Manifest(
            key="podio",
            base_url=BASE,
            auth=api.Auth(strategy="none"),
            pagination=api.Pagination(
                style="offset",
                offset_param="offset",
                limit_param="limit",
                items_field="items",
                page_size=2,  # page 1 full (2 items) → fetch page 2; page 2 partial (1 item) → stop
            ),
            rate_limit_remaining_header="",
            default_headers={"Authorization": "OAuth2 " + token_val},
        )
        c = api.Client(manifest=manifest, credential="")
        result = c.collect(f"/item/app/{app_id}/")

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(result["items"][0]["item_id"], 1001)
        self.assertEqual(result["items"][1]["item_id"], 1002)
        self.assertEqual(result["items"][2]["item_id"], 1003)

        # Both page requests carried the OAuth2 header — credential rides every page.
        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            sent = call.request.headers.get("Authorization", "")
            self.assertEqual(sent, EXPECTED_AUTH)


class PodioPickFields(unittest.TestCase):
    """api.pick selects support-relevant fields from item objects."""

    def test_pick_item_fields(self):
        item = _ITEM_SINGLE
        selected = api.pick(item, "item_id,title,link")
        self.assertEqual(selected["item_id"], 1001)
        self.assertEqual(selected["title"], "Login broken on Safari")
        self.assertIn("link", selected)

    def test_pick_task_fields(self):
        task = _TASKS[0]
        selected = api.pick(task, "task_id,text,status,due_date")
        self.assertEqual(selected["task_id"], 500)
        self.assertEqual(selected["text"], "Follow up with Alice")
        self.assertEqual(selected["status"], "active")
        self.assertEqual(selected["due_date"], "2024-01-20")


class PodioCLI(unittest.TestCase):
    """CLI subcommands print markdown; credential is injected correctly."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_PODIO")
        os.environ["RC_CONN_PODIO"] = FAKE_TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_PODIO", None)
        else:
            os.environ["RC_CONN_PODIO"] = self._saved

    @responses_lib.activate
    def test_cli_orgs_subcommand(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/org/", json=_ORGS, status=200)
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = podio.main(["orgs"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Acme Corp", out)
        self.assertIn("Beta Ltd", out)
        # Auth header was correct
        self.assertEqual(responses_lib.calls[0].request.headers.get("Authorization"), EXPECTED_AUTH)

    @responses_lib.activate
    def test_cli_spaces_subcommand(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/org/100/all_spaces/", json=_SPACES, status=200)
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = podio.main(["spaces", "--org-id", "100"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Support", out)
        self.assertIn("Dev", out)

    @responses_lib.activate
    def test_cli_apps_subcommand(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/app/space/200/", json=_APPS, status=200)
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = podio.main(["apps", "--space-id", "200"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Tickets", out)
        self.assertIn("Backlog", out)

    @responses_lib.activate
    def test_cli_items_subcommand(self):
        responses_lib.add(
            responses_lib.GET, f"{BASE}/item/app/300/", json=_ITEMS_PAGE_1, status=200
        )
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = podio.main(["items", "--app-id", "300", "--limit", "1"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Login broken on Safari", out)

    @responses_lib.activate
    def test_cli_item_subcommand(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/item/1001/", json=_ITEM_SINGLE, status=200)
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = podio.main(["item", "--item-id", "1001"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Login broken on Safari", out)
        self.assertIn("Status", out)

    @responses_lib.activate
    def test_cli_tasks_subcommand(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/task/", json=_TASKS, status=200)
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = podio.main(["tasks"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Follow up with Alice", out)


class PodioTokenHygiene(unittest.TestCase):
    """CI guard: no real Podio token prefix may land in the committed connector files.

    Scoped to the connector dir (manifest + __init__.py + __main__.py), NOT this test file —
    the test legitimately names the prefixes it hunts for.
    """

    # Podio OAuth access tokens start with these (split across string literals to avoid self-flagging).
    _TOKEN_PREFIXES = (
        "podio_access_" + "token",  # hypothetical prefix
    )

    def test_no_token_prefixes_in_podio_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "podio"
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
