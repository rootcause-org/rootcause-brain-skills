"""Fixture tests for the Microsoft Outlook Mail integration (manifest-only, via lib.api).

Outlook Mail is a manifest-only integration: there is no per-key Python connector. Microsoft Graph
paginates with ``@odata.nextLink`` — an absolute next-page URL in the JSON body. lib.api's
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
        pytest tests/test_msoutlookmail_connector.py -q
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
# Documented example payloads (real Graph message shape, trimmed to support fields)
# ---------------------------------------------------------------------------

_MSG_1 = {
    "id": "AAMkADk1",
    "subject": "Invoice #1001",
    "from": {"emailAddress": {"name": "Billing", "address": "billing@example.com"}},
    "receivedDateTime": "2026-06-27T08:00:00Z",
    "bodyPreview": "Your invoice is attached.",
    "isRead": False,
}
_MSG_2 = {
    "id": "AAMkADk2",
    "subject": "Re: Onboarding",
    "from": {"emailAddress": {"name": "Carol", "address": "carol@example.com"}},
    "receivedDateTime": "2026-06-27T09:30:00Z",
    "bodyPreview": "Thanks for the setup help.",
    "isRead": True,
}


def _page(items: list, next_url: str | None = None) -> dict:
    """Build a Graph collection envelope. body_url stops when @odata.nextLink is absent."""
    body = {
        "@odata.context": f"{GRAPH}/$metadata#users('x')/messages",
        "value": items,
    }
    if next_url is not None:
        body["@odata.nextLink"] = next_url  # absolute URL, verbatim
    return body


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.get("RC_CONN_MSOUTLOOKMAIL")
        # Fake token with a split JWT-ish prefix so the hygiene guard can't flag this file itself.
        os.environ["RC_CONN_MSOUTLOOKMAIL"] = "ey" + "J0_fake_mail_bearer_000"
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        api.load_manifests()

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("RC_CONN_MSOUTLOOKMAIL", None)
        else:
            os.environ["RC_CONN_MSOUTLOOKMAIL"] = self._saved_env


# ---------------------------------------------------------------------------
# 1. Manifest loading
# ---------------------------------------------------------------------------

class TestManifest(_Base):
    def test_yaml_loads_and_maps_every_field(self):
        self.assertIn("msoutlookmail", api.MANIFESTS)
        m = api.MANIFESTS["msoutlookmail"]
        self.assertEqual(m.key, "msoutlookmail")
        self.assertEqual(m.base_url, "https://graph.microsoft.com/v1.0")
        self.assertEqual(m.auth.strategy, "bearer")
        self.assertEqual(m.pagination.style, "body_url")
        self.assertEqual(m.pagination.next_url_field, "@odata.nextLink")  # literal key
        self.assertEqual(m.pagination.items_field, "value")
        self.assertEqual(m.pagination.page_size, 50)

    def test_manifest_yaml_is_manifest_only_and_keeps_egress(self):
        # connector_module/egress_hosts/oauth aren't lib.api Manifest fields; assert on raw YAML.
        import yaml
        raw = yaml.safe_load(
            (Path(__file__).resolve().parents[1]
             / "lib" / "connectors" / "msoutlookmail" / "manifest.yaml").read_text()
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
        page2_url = f"{GRAPH}/me/messages?$skip=50"
        responses_lib.add(
            responses_lib.GET, f"{GRAPH}/me/messages",
            json=_page([_MSG_1], next_url=page2_url), status=200,
        )
        responses_lib.add(
            responses_lib.GET, page2_url,
            json=_page([_MSG_2], next_url=None), status=200,  # no nextLink ⇒ exhausted
        )

        m = api.MANIFESTS["msoutlookmail"]
        result = api.client(m, token_key="msoutlookmail").collect("me/messages")

        self.assertFalse(result["incomplete"], result["reason"])
        subjects = [it["subject"] for it in result["items"]]
        self.assertEqual(subjects, ["Invoice #1001", "Re: Onboarding"])  # in order
        self.assertEqual(len(responses_lib.calls), 2)
        self.assertIn("$skip=50", responses_lib.calls[1].request.url)

    @responses_lib.activate
    def test_bearer_credential_on_all_pages_including_continuation(self):
        page2_url = f"{GRAPH}/me/messages?$skip=50"
        responses_lib.add(
            responses_lib.GET, f"{GRAPH}/me/messages",
            json=_page([_MSG_1], next_url=page2_url), status=200,
        )
        responses_lib.add(
            responses_lib.GET, page2_url,
            json=_page([_MSG_2], next_url=None), status=200,
        )

        m = api.MANIFESTS["msoutlookmail"]
        api.client(m, token_key="msoutlookmail").collect("me/messages")

        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(auth.startswith("Bearer "), f"Missing Bearer on {call.request.url}")
            self.assertIn("fake_mail_bearer", auth)

    @responses_lib.activate
    def test_single_page_no_continuation(self):
        responses_lib.add(
            responses_lib.GET, f"{GRAPH}/me/messages",
            json=_page([_MSG_1], next_url=None), status=200,
        )
        m = api.MANIFESTS["msoutlookmail"]
        result = api.client(m, token_key="msoutlookmail").collect("me/messages")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_lib_api_cli_drives_manifest(self):
        page2_url = f"{GRAPH}/me/messages?$skip=50"
        responses_lib.add(
            responses_lib.GET, f"{GRAPH}/me/messages",
            json=_page([_MSG_1], next_url=page2_url), status=200,
        )
        responses_lib.add(
            responses_lib.GET, page2_url,
            json=_page([_MSG_2], next_url=None), status=200,
        )
        rc = api._main([
            "get", "msoutlookmail", "me/messages", "--paginate",
            "--pick", "id,subject,from.emailAddress.address,receivedDateTime,isRead",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertTrue(call.request.headers.get("Authorization", "").startswith("Bearer "))


# ---------------------------------------------------------------------------
# 3. api.pick on message fields
# ---------------------------------------------------------------------------

class TestPick(_Base):
    def test_pick_selects_support_fields(self):
        picked = api.pick(_MSG_1, "id,subject,receivedDateTime,isRead")
        self.assertEqual(picked["id"], "AAMkADk1")
        self.assertEqual(picked["subject"], "Invoice #1001")
        self.assertFalse(picked["isRead"])

    def test_pick_nested_sender_address(self):
        picked = api.pick(_MSG_1, "subject,from.emailAddress.address")
        self.assertEqual(picked["from.emailAddress.address"], "billing@example.com")


# ---------------------------------------------------------------------------
# 4. Token-prefix hygiene
# ---------------------------------------------------------------------------

class TestHygiene(unittest.TestCase):
    """CI guard: no real MS Graph/Outlook token prefix may land in the connector dir (only
    manifest.yaml remains). Scoped to the connector dir, NOT this test file — the test legitimately
    names the prefixes it hunts for, so scanning itself would be a false positive.

    MS Graph / MSAL access tokens are JWTs starting with "eyJ"; delegated Exchange/Graph tokens
    often start with "EwA". Split each literal with concatenation so the guard can't flag itself.
    """

    _TOKEN_PREFIXES = ("eyJ" "0", "EwA" "0", "Bearer" " ey", "RC_CONN" "_MSOUTLOOKMAIL=ey")

    def test_no_token_prefixes_in_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "msoutlookmail"
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
