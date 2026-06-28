"""Fixture test for the Resend connector (script connector, force-code trigger d).

Resend cursor pagination uses the last item's id as the ``after`` param — there is no scalar
next-cursor field, so the manifest has cursor_field="" and the connector's _resend_next() derives
the cursor manually (mirrors the Stripe pattern).

No live creds, no network: HTTP is mocked with ``responses``. Bodies are Resend's own DOCUMENTED
example payloads, trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_resend_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import resend  # noqa: E402

API = "https://api.resend.com"

# ---------------------------------------------------------------------------
# Documented example payloads (Resend API reference, trimmed to support fields)
# ---------------------------------------------------------------------------

_EMAIL_1 = {
    "id": "4ef9a417-02e9-4d39-ad75-9611e0fcc33c",
    "to": ["delivered@resend.dev"],
    "from": "Acme <onboarding@resend.dev>",
    "created_at": "2026-04-03 22:13:42.674981+00",
    "subject": "Hello World",
    "last_event": "delivered",
    "scheduled_at": None,
}
_EMAIL_2 = {
    "id": "8a1b3c2d-1234-5678-abcd-ef0123456789",
    "to": ["bounced@resend.dev"],
    "from": "Acme <onboarding@resend.dev>",
    "created_at": "2026-04-04 10:00:00.000000+00",
    "subject": "Bounce Test",
    "last_event": "bounced",
    "scheduled_at": None,
}

# Page 1 of /emails: has_more=True → page 2 uses after=<last_id>
_LIST_PAGE_1 = {
    "object": "list",
    "has_more": True,
    "data": [_EMAIL_1],
}
# Page 2 of /emails: has_more=False → stop
_LIST_PAGE_2 = {
    "object": "list",
    "has_more": False,
    "data": [_EMAIL_2],
}

_SINGLE_EMAIL = {
    "object": "email",
    "id": "4ef9a417-02e9-4d39-ad75-9611e0fcc33c",
    "message_id": "<111-222-333@email.example.com>",
    "to": ["delivered@resend.dev"],
    "from": "Acme <onboarding@resend.dev>",
    "created_at": "2026-04-03 22:13:42.674981+00",
    "subject": "Hello World",
    "html": "Congrats on sending your <strong>first email</strong>!",
    "text": None,
    "bcc": [],
    "cc": [],
    "reply_to": [],
    "last_event": "delivered",
    "scheduled_at": None,
    "tags": [{"name": "category", "value": "confirm_email"}],
}

_DOMAINS_PAGE_1 = {
    "object": "list",
    "has_more": False,
    "data": [
        {
            "id": "d91cd9bd-1176-453e-8fc1-35364d380206",
            "name": "example.com",
            "status": "verified",
            "created_at": "2026-04-26 20:21:26.347412+00",
            "region": "us-east-1",
            "capabilities": {"sending": "enabled", "receiving": "disabled"},
        }
    ],
}


# ---------------------------------------------------------------------------
# Helper: standard env setup
# ---------------------------------------------------------------------------

class _ResendBase(unittest.TestCase):
    def setUp(self):
        # Isolate the manifest registry so test ordering doesn't matter.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        # Re-register the connector manifest (clear removed it).
        api.register(resend.MANIFEST)
        self._saved = os.environ.get("RC_CONN_RESEND")
        # Split the prefix literal so the hygiene guard doesn't flag this test file itself.
        os.environ["RC_CONN_RESEND"] = "re" "_test_abc123"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_RESEND", None)
        else:
            os.environ["RC_CONN_RESEND"] = self._saved


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------

class ResendManifestLoad(_ResendBase):
    def test_manifest_loaded_from_yaml(self):
        """load_manifests() discovers and maps every field from manifest.yaml."""
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        m = api.load_manifests()
        self.assertIn("resend", m)
        r = m["resend"]
        self.assertEqual(r.base_url, "https://api.resend.com")
        self.assertEqual(r.auth.strategy, "bearer")
        self.assertEqual(r.pagination.style, "cursor")
        self.assertEqual(r.pagination.cursor_param, "after")
        self.assertEqual(r.pagination.has_more_field, "has_more")
        self.assertEqual(r.pagination.items_field, "data")
        self.assertEqual(r.pagination.page_size, 100)
        # cursor_field is intentionally "" — no scalar cursor in the body.
        self.assertEqual(r.pagination.cursor_field, "")
        self.assertEqual(r.rate_limit_remaining_header, "")


# ---------------------------------------------------------------------------
# Cursor pagination: _resend_next + multi-page _list
# ---------------------------------------------------------------------------

class ResendPagination(_ResendBase):
    def test_resend_next_returns_last_id_when_has_more(self):
        """_resend_next derives the cursor from data[-1].id when has_more is True."""
        nxt = resend._resend_next(_LIST_PAGE_1)
        self.assertEqual(nxt, _EMAIL_1["id"])

    def test_resend_next_returns_none_when_no_more(self):
        self.assertIsNone(resend._resend_next(_LIST_PAGE_2))

    def test_resend_next_returns_none_on_empty_data(self):
        self.assertIsNone(resend._resend_next({"has_more": True, "data": []}))

    @responses_lib.activate
    def test_list_stitches_two_pages(self):
        """_list() follows the after-cursor across two pages, collecting all items."""
        responses_lib.add(
            responses_lib.GET, f"{API}/emails",
            json=_LIST_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.GET, f"{API}/emails",
            json=_LIST_PAGE_2, status=200,
        )

        items = resend._list("/emails", {}, limit_items=50)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["id"], _EMAIL_1["id"])
        self.assertEqual(items[1]["id"], _EMAIL_2["id"])

        # Page 1: no after param.
        req1 = responses_lib.calls[0].request
        self.assertNotIn("after", req1.url)

        # Page 2: after = last item id from page 1.
        req2 = responses_lib.calls[1].request
        self.assertIn(f"after={_EMAIL_1['id']}", req2.url)

    @responses_lib.activate
    def test_bearer_credential_on_every_request(self):
        """The bearer token rides ALL page requests, not just the first."""
        responses_lib.add(responses_lib.GET, f"{API}/emails", json=_LIST_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, f"{API}/emails", json=_LIST_PAGE_2, status=200)

        resend._list("/emails", {}, limit_items=50)

        expected_auth = "Bearer " + "re" + "_test_abc123"
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["Authorization"], expected_auth)


# ---------------------------------------------------------------------------
# list_emails + emails_to_markdown
# ---------------------------------------------------------------------------

class ResendListEmails(_ResendBase):
    @responses_lib.activate
    def test_list_emails_returns_picked_fields(self):
        """list_emails() picks only support-relevant fields from each email."""
        responses_lib.add(responses_lib.GET, f"{API}/emails", json=_LIST_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, f"{API}/emails", json=_LIST_PAGE_2, status=200)

        rows = resend.list_emails(limit=50)

        self.assertEqual(len(rows), 2)
        self.assertIn("id", rows[0])
        self.assertIn("last_event", rows[0])
        self.assertIn("subject", rows[0])
        # pick should have dropped raw-body fields not declared in pick()
        self.assertNotIn("html", rows[0])
        self.assertNotIn("tags", rows[0])

    def test_emails_to_markdown_renders_table(self):
        picked = [
            api.pick(e, "id,to,from,subject,last_event,created_at,scheduled_at")
            for e in [_EMAIL_1, _EMAIL_2]
        ]
        md = resend.emails_to_markdown(picked)
        self.assertIn("# Resend sent emails", md)
        self.assertIn("delivered", md)
        self.assertIn("bounced", md)
        self.assertIn(_EMAIL_1["id"], md)

    def test_emails_to_markdown_empty(self):
        md = resend.emails_to_markdown([])
        self.assertIn("no sent emails found", md)


# ---------------------------------------------------------------------------
# get_email + email_to_markdown
# ---------------------------------------------------------------------------

class ResendGetEmail(_ResendBase):
    @responses_lib.activate
    def test_get_email_picks_support_fields(self):
        responses_lib.add(
            responses_lib.GET,
            f"{API}/emails/{_SINGLE_EMAIL['id']}",
            json=_SINGLE_EMAIL, status=200,
        )

        result = resend.get_email(_SINGLE_EMAIL["id"])

        # Support fields should be present.
        self.assertEqual(result["id"], _SINGLE_EMAIL["id"])
        self.assertEqual(result["last_event"], "delivered")
        self.assertIn("tags", result)
        # The bearer credential was sent.
        self.assertEqual(
            responses_lib.calls[0].request.headers["Authorization"],
            "Bearer " + "re" + "_test_abc123",
        )

    def test_email_to_markdown_renders_status(self):
        e = api.pick(_SINGLE_EMAIL, "id,to,from,subject,last_event,created_at,html,text,tags,bcc,cc,reply_to")
        md = resend.email_to_markdown(e)
        self.assertIn("# Resend email", md)
        self.assertIn("delivered", md)
        self.assertIn("category=confirm_email", md)


# ---------------------------------------------------------------------------
# list_domains + domains_to_markdown
# ---------------------------------------------------------------------------

class ResendDomains(_ResendBase):
    @responses_lib.activate
    def test_list_domains_returns_picked_fields(self):
        responses_lib.add(responses_lib.GET, f"{API}/domains", json=_DOMAINS_PAGE_1, status=200)

        rows = resend.list_domains()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "example.com")
        self.assertEqual(rows[0]["status"], "verified")
        self.assertNotIn("capabilities", rows[0])

    def test_domains_to_markdown_renders_table(self):
        picked = [api.pick(d, "id,name,status,region,created_at") for d in _DOMAINS_PAGE_1["data"]]
        md = resend.domains_to_markdown(picked)
        self.assertIn("# Resend sending domains", md)
        self.assertIn("example.com", md)
        self.assertIn("verified", md)

    def test_domains_to_markdown_empty(self):
        md = resend.domains_to_markdown([])
        self.assertIn("no domains found", md)


# ---------------------------------------------------------------------------
# CLI via api._main (manifest-driven single-page reads still work)
# ---------------------------------------------------------------------------

class ResendCliMain(_ResendBase):
    @responses_lib.activate
    def test_cli_get_single_email_via_lib_api(self):
        """`python -m lib.api get resend /emails/<id>` drives the manifest directly."""
        responses_lib.add(
            responses_lib.GET,
            f"{API}/emails/{_SINGLE_EMAIL['id']}",
            json=_SINGLE_EMAIL, status=200,
        )
        rc = api._main([
            "get", "resend", f"/emails/{_SINGLE_EMAIL['id']}",
            "--pick", "id,last_event,subject",
        ])
        self.assertEqual(rc, 0)
        req = responses_lib.calls[0].request
        self.assertIn("Authorization", req.headers)
        self.assertTrue(req.headers["Authorization"].startswith("Bearer "))

    @responses_lib.activate
    def test_cli_emails_command(self):
        """Script CLI: `python -m lib.connectors.resend emails` prints markdown."""
        responses_lib.add(responses_lib.GET, f"{API}/emails", json=_LIST_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, f"{API}/emails", json=_LIST_PAGE_2, status=200)
        rc = resend.main(["emails", "--limit", "50"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_domains_command(self):
        """Script CLI: `python -m lib.connectors.resend domains` prints markdown."""
        responses_lib.add(responses_lib.GET, f"{API}/domains", json=_DOMAINS_PAGE_1, status=200)
        rc = resend.main(["domains"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_single_email_command(self):
        """Script CLI: `python -m lib.connectors.resend email <id>` prints markdown."""
        responses_lib.add(
            responses_lib.GET,
            f"{API}/emails/{_SINGLE_EMAIL['id']}",
            json=_SINGLE_EMAIL, status=200,
        )
        rc = resend.main(["email", _SINGLE_EMAIL["id"]])
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Token-prefix hygiene guard (scoped to the connector dir)
# ---------------------------------------------------------------------------

class ResendCassetteHygiene(unittest.TestCase):
    """CI guard: no real Resend API key prefix may land in the connector files.

    Scopes to the connector dir only — NOT this test file, which legitimately
    names the prefixes it hunts for (split with concatenation to avoid self-flagging).
    """

    # Resend API keys are prefixed `re_` (live) and `re_test_` (test-mode).
    # Split with string concatenation so the guard doesn't flag itself.
    _TOKEN_PREFIXES = ("re" "_live_", "re" "_test_")

    def test_no_token_prefixes_in_resend_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "resend"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
