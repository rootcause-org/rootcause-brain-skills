"""Fixture tests for the Microsoft Outlook Calendar integration (manifest-only, via lib.api).

Outlook Calendar is a manifest-only integration: there is no per-key Python connector. Microsoft
Graph paginates with ``@odata.nextLink`` — an absolute next-page URL in the JSON body. lib.api's
``body_url`` style follows it directly. The critical bit: ``@odata.nextLink`` has dots that are NOT
path segments, so ``next_url_field`` resolution tries the WHOLE field as a literal dict key first
(``field in body``) before any dotted traversal. These tests drive the generic path:

  - the YAML manifest loads and maps every lib.api field (style=body_url, next_url_field literal
    "@odata.nextLink", items_field=value, auth.strategy=bearer, base_url);
  - ``client(m).collect()`` stitches ≥2 Graph-shaped fixture pages in order via @odata.nextLink;
  - the bearer credential rides EVERY request, including the continuation page;
  - ``api.pick`` selects the support-relevant fields;
  - token-prefix hygiene: no real MS Graph token prefix lands in the connector dir.

No live creds, no network. HTTP is mocked with ``responses``. Bodies mirror the real Graph response
shape: {"@odata.context": ..., "value": [...], "@odata.nextLink": "https://graph.microsoft.com/..."}.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_msoutlookcalendar_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

GRAPH = "https://graph.microsoft.com/v1.0"

# ---------------------------------------------------------------------------
# Documented example payloads (real Graph event shape, trimmed)
# ---------------------------------------------------------------------------

_EVENT_1 = {
    "id": "AAMkAGI1",
    "subject": "Sprint planning",
    "start": {"dateTime": "2026-06-29T09:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-06-29T10:00:00.0000000", "timeZone": "UTC"},
    "isAllDay": False,
    "organizer": {"emailAddress": {"name": "Alice", "address": "alice@example.com"}},
    "location": {"displayName": "Room 1"},
}
_EVENT_2 = {
    "id": "AAMkAGI2",
    "subject": "Retro",
    "start": {"dateTime": "2026-06-30T15:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-06-30T16:00:00.0000000", "timeZone": "UTC"},
    "isAllDay": False,
    "organizer": {"emailAddress": {"name": "Bob", "address": "bob@example.com"}},
    "location": {"displayName": "Room 2"},
}


def _page(items: list, next_url: str | None = None) -> dict:
    """Build a Graph collection envelope. body_url stops when @odata.nextLink is absent."""
    body = {
        "@odata.context": f"{GRAPH}/$metadata#users('x')/events",
        "value": items,
    }
    if next_url is not None:
        body["@odata.nextLink"] = next_url  # absolute URL, verbatim
    return body


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.get("RC_CONN_MSOUTLOOKCALENDAR")
        # Fake token with a split JWT-ish prefix so the hygiene guard can't flag this file itself.
        os.environ["RC_CONN_MSOUTLOOKCALENDAR"] = "ey" + "J0_fake_calendar_bearer_000"
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        api.load_manifests()

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("RC_CONN_MSOUTLOOKCALENDAR", None)
        else:
            os.environ["RC_CONN_MSOUTLOOKCALENDAR"] = self._saved_env


# ---------------------------------------------------------------------------
# 1. Manifest loading
# ---------------------------------------------------------------------------

class TestManifest(_Base):
    def test_yaml_loads_and_maps_every_field(self):
        self.assertIn("msoutlookcalendar", api.MANIFESTS)
        m = api.MANIFESTS["msoutlookcalendar"]
        self.assertEqual(m.key, "msoutlookcalendar")
        self.assertEqual(m.base_url, "https://graph.microsoft.com/v1.0")
        self.assertEqual(m.auth.strategy, "bearer")
        self.assertEqual(m.pagination.style, "body_url")
        self.assertEqual(m.pagination.next_url_field, "@odata.nextLink")  # literal key
        self.assertEqual(m.pagination.items_field, "value")

    def test_manifest_yaml_is_manifest_only_and_keeps_egress(self):
        # connector_module/egress_hosts/oauth aren't lib.api Manifest fields; assert on raw YAML.
        import yaml
        raw = yaml.safe_load(
            (Path(__file__).resolve().parents[1]
             / "lib" / "connectors" / "msoutlookcalendar" / "manifest.yaml").read_text()
        )
        self.assertEqual(raw["connector_module"], "")  # manifest-only, no script
        self.assertIn("graph.microsoft.com", raw["egress_hosts"])
        self.assertIn("login.microsoftonline.com", raw["egress_hosts"])
        self.assertIn("oauth", raw)  # oauth block preserved


# ---------------------------------------------------------------------------
# 2. body_url pagination via @odata.nextLink (literal key, absolute URL)
# ---------------------------------------------------------------------------

class TestPagination(_Base):
    @responses_lib.activate
    def test_collect_stitches_two_pages_via_odata_nextlink(self):
        page2_url = f"{GRAPH}/me/events?$skip=10"
        responses_lib.add(
            responses_lib.GET, f"{GRAPH}/me/events",
            json=_page([_EVENT_1], next_url=page2_url), status=200,
        )
        responses_lib.add(
            responses_lib.GET, page2_url,
            json=_page([_EVENT_2], next_url=None), status=200,  # no nextLink ⇒ exhausted
        )

        m = api.MANIFESTS["msoutlookcalendar"]
        result = api.client(m, token_key="msoutlookcalendar").collect("me/events")

        self.assertFalse(result["incomplete"], result["reason"])
        subjects = [it["subject"] for it in result["items"]]
        self.assertEqual(subjects, ["Sprint planning", "Retro"])  # in order
        self.assertEqual(len(responses_lib.calls), 2)
        self.assertIn("$skip=10", responses_lib.calls[1].request.url)

    @responses_lib.activate
    def test_bearer_credential_on_all_pages_including_continuation(self):
        page2_url = f"{GRAPH}/me/events?$skip=10"
        responses_lib.add(
            responses_lib.GET, f"{GRAPH}/me/events",
            json=_page([_EVENT_1], next_url=page2_url), status=200,
        )
        responses_lib.add(
            responses_lib.GET, page2_url,
            json=_page([_EVENT_2], next_url=None), status=200,
        )

        m = api.MANIFESTS["msoutlookcalendar"]
        api.client(m, token_key="msoutlookcalendar").collect("me/events")

        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(auth.startswith("Bearer "), f"Missing Bearer on {call.request.url}")
            self.assertIn("fake_calendar_bearer", auth)

    @responses_lib.activate
    def test_single_page_no_continuation(self):
        responses_lib.add(
            responses_lib.GET, f"{GRAPH}/me/events",
            json=_page([_EVENT_1], next_url=None), status=200,
        )
        m = api.MANIFESTS["msoutlookcalendar"]
        result = api.client(m, token_key="msoutlookcalendar").collect("me/events")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_lib_api_cli_drives_manifest(self):
        page2_url = f"{GRAPH}/me/events?$skip=10"
        responses_lib.add(
            responses_lib.GET, f"{GRAPH}/me/events",
            json=_page([_EVENT_1], next_url=page2_url), status=200,
        )
        responses_lib.add(
            responses_lib.GET, page2_url,
            json=_page([_EVENT_2], next_url=None), status=200,
        )
        rc = api._main([
            "get", "msoutlookcalendar", "me/events", "--paginate",
            "--pick", "subject,start.dateTime,organizer.emailAddress.address",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertTrue(call.request.headers.get("Authorization", "").startswith("Bearer "))


# ---------------------------------------------------------------------------
# 3. api.pick on event fields
# ---------------------------------------------------------------------------

class TestPick(_Base):
    def test_pick_selects_support_fields(self):
        picked = api.pick(_EVENT_1, "id,subject,isAllDay")
        self.assertEqual(picked["id"], "AAMkAGI1")
        self.assertEqual(picked["subject"], "Sprint planning")
        self.assertFalse(picked["isAllDay"])

    def test_pick_nested_organizer_and_time(self):
        picked = api.pick(_EVENT_1, "subject,start.dateTime,organizer.emailAddress.address")
        self.assertEqual(picked["start.dateTime"], "2026-06-29T09:00:00.0000000")
        self.assertEqual(picked["organizer.emailAddress.address"], "alice@example.com")


# ---------------------------------------------------------------------------
# 4. Token-prefix hygiene
# ---------------------------------------------------------------------------

class TestHygiene(unittest.TestCase):
    """CI guard: no real MS Graph access token prefix may land in the connector dir (only
    manifest.yaml remains). Scoped to the connector dir, NOT this test file — the test legitimately
    names the prefixes it hunts for, so scanning itself would be a false positive.

    MS Graph / MSAL access tokens are JWTs starting with "eyJ"; delegated Exchange/Graph tokens
    often start with "EwA". Split each literal with concatenation so the guard can't flag itself.
    """

    _TOKEN_PREFIXES = ("eyJ" "0", "EwA" "0", "Bearer" " ey", "RC_CONN" "_MSOUTLOOKCALENDAR=ey")

    def test_no_token_prefixes_in_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "msoutlookcalendar"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file() or path.suffix == ".pyc":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref}")
        self.assertEqual(offenders, [], f"real token prefix found in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
