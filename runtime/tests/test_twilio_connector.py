"""Fixture test for the Twilio script connector.

Force-code trigger (d): Twilio paginates via ``next_page_uri`` in the JSON body (a relative path
to follow verbatim), which none of lib.api's generic pagination styles can express. The script
connector ``_twilio_pages()`` follows ``next_page_uri`` directly. These tests verify:

- The manifest YAML loads via lib.api's loader and maps every field correctly.
- ``next_page_uri`` pagination stitches ≥2 pages end-to-end.
- The basic-auth credential rides EVERY request, including ``next_page_uri`` follows.
- AccountSid is correctly extracted from the credential.
- Field pre-selection with ``api.pick`` extracts support-relevant fields.
- The CLI drives messages/calls/numbers subcommands.

No live creds, no network: HTTP is mocked with ``responses``. Bodies mirror Twilio's documented
example payloads, trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_twilio_connector.py -q
"""

import base64
import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import twilio as tw  # noqa: E402

# ---------------------------------------------------------------------------
# Test fixtures — Twilio documented example payloads, trimmed to support fields
# ---------------------------------------------------------------------------

# Fake credential: "AccountSid:AuthToken" — split so the hygiene guard below doesn't flag itself.
_ACCOUNT_SID = "AC" + "test1234567890abcdef1234567890ab"
_AUTH_TOKEN = "auth_token_test_abc123"
_CRED = f"{_ACCOUNT_SID}:{_AUTH_TOKEN}"

BASE = "https://api.twilio.com"
_MESSAGES_PATH = f"/2010-04-01/Accounts/{_ACCOUNT_SID}/Messages.json"
_MESSAGES_URL = BASE + _MESSAGES_PATH
_CALLS_PATH = f"/2010-04-01/Accounts/{_ACCOUNT_SID}/Calls.json"
_CALLS_URL = BASE + _CALLS_PATH
_NUMBERS_PATH = f"/2010-04-01/Accounts/{_ACCOUNT_SID}/IncomingPhoneNumbers.json"
_NUMBERS_URL = BASE + _NUMBERS_PATH

# Page 1 of messages — advertises next_page_uri pointing at page 2.
_MESSAGES_PAGE_1_NEXT = f"/2010-04-01/Accounts/{_ACCOUNT_SID}/Messages.json?Page=1&PageToken=PASM"
_MESSAGES_PAGE_1 = {
    "messages": [
        {
            "sid": "SM" + "aaaabbbbccccddddeeeeffffaaaabbbb",
            "to": "+14155552671",
            "from": "+14155551234",
            "status": "delivered",
            "direction": "outbound-api",
            "date_sent": "Mon, 16 Aug 2010 03:45:01 +0000",
            "body": "Hello World",
            "num_segments": "1",
            "error_code": None,
            "error_message": None,
        }
    ],
    "next_page_uri": _MESSAGES_PAGE_1_NEXT,
    "page": 0,
    "page_size": 50,
    "start": 0,
    "end": 0,
    "uri": _MESSAGES_PATH,
    "first_page_uri": _MESSAGES_PATH,
    "previous_page_uri": None,
}

# Page 2 of messages — no next_page_uri → stop.
_MESSAGES_PAGE_2 = {
    "messages": [
        {
            "sid": "SM" + "00001111222233334444555566667777",
            "to": "+14155552671",
            "from": "+14155559876",
            "status": "failed",
            "direction": "outbound-api",
            "date_sent": "Mon, 16 Aug 2010 02:00:00 +0000",
            "body": "Retry me",
            "num_segments": "1",
            "error_code": 30006,
            "error_message": "Landline or unreachable carrier",
        }
    ],
    "next_page_uri": None,
    "page": 1,
    "page_size": 50,
    "start": 1,
    "end": 1,
    "uri": _MESSAGES_PAGE_1_NEXT,
    "first_page_uri": _MESSAGES_PATH,
    "previous_page_uri": _MESSAGES_PATH,
}

# Single page of calls.
_CALLS_PAGE = {
    "calls": [
        {
            "sid": "CA" + "aaaabbbbccccddddeeeeffffaaaabbbb",
            "to": "+14155552671",
            "from": "+14155551234",
            "status": "completed",
            "direction": "outbound-api",
            "start_time": "Mon, 16 Aug 2010 03:45:01 +0000",
            "duration": "90",
            "price": "-0.02000",
            "price_unit": "USD",
        }
    ],
    "next_page_uri": None,
    "page": 0,
    "page_size": 50,
    "start": 0,
    "end": 0,
    "uri": _CALLS_PATH,
    "first_page_uri": _CALLS_PATH,
    "previous_page_uri": None,
}

# Single page of phone numbers.
_NUMBERS_PAGE = {
    "incoming_phone_numbers": [
        {
            "sid": "PN" + "aaaabbbbccccddddeeeeffffaaaabbbb",
            "phone_number": "+14155552671",
            "friendly_name": "(415) 555-2671",
            "status": "in-use",
            "capabilities": {"voice": True, "sms": True, "mms": False, "fax": False},
        }
    ],
    "next_page_uri": None,
    "page": 0,
    "page_size": 50,
    "start": 0,
    "end": 0,
    "uri": _NUMBERS_PATH,
    "first_page_uri": _NUMBERS_PATH,
    "previous_page_uri": None,
}


def _basic_header(cred: str) -> str:
    """Build the expected Basic auth header value for a given credential string."""
    encoded = base64.b64encode(cred.encode()).decode()
    return f"Basic {encoded}"


class TwilioManifestLoad(unittest.TestCase):
    """The YAML manifest loads cleanly and maps every lib.api field correctly."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_fields(self):
        m = api.load_manifests()
        self.assertIn("twilio", m)
        t = m["twilio"]
        self.assertEqual(t.key, "twilio")
        self.assertEqual(t.base_url, "https://api.twilio.com/2010-04-01")
        self.assertEqual(t.auth.strategy, "basic")
        # pagination style is `none` — the script drives paging
        self.assertEqual(t.pagination.style, "none")
        self.assertEqual(t.rate_limit_remaining_header, "")


class TwilioAuth(unittest.TestCase):
    """Basic-auth credential is presented correctly on every request."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_TWILIO")
        os.environ["RC_CONN_TWILIO"] = _CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_TWILIO", None)
        else:
            os.environ["RC_CONN_TWILIO"] = self._saved

    @responses_lib.activate
    def test_basic_auth_on_single_page(self):
        responses_lib.add(responses_lib.GET, _MESSAGES_URL, json=_MESSAGES_PAGE_2, status=200)
        sid = tw._account_sid()
        self.assertEqual(sid, _ACCOUNT_SID)
        msgs = tw.list_messages(sid, limit=10)
        self.assertEqual(len(msgs), 1)
        auth_header = responses_lib.calls[0].request.headers["Authorization"]
        self.assertEqual(auth_header, _basic_header(_CRED))

    def test_account_sid_extraction(self):
        sid = tw._account_sid()
        self.assertEqual(sid, _ACCOUNT_SID)
        self.assertTrue(sid.startswith("AC"))

    def test_bad_credential_raises(self):
        os.environ["RC_CONN_TWILIO"] = "notvalid:token"
        with self.assertRaises(RuntimeError, msg="should raise on missing AC prefix"):
            tw._account_sid()


class TwilioPagination(unittest.TestCase):
    """next_page_uri pagination stitches ≥2 pages; credential rides every request."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_TWILIO")
        os.environ["RC_CONN_TWILIO"] = _CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_TWILIO", None)
        else:
            os.environ["RC_CONN_TWILIO"] = self._saved

    @responses_lib.activate
    def test_next_page_uri_stitches_two_pages(self):
        """Page 1 advertises next_page_uri → page 2 is fetched and results are merged."""
        responses_lib.add(responses_lib.GET, _MESSAGES_URL, json=_MESSAGES_PAGE_1, status=200)
        # Page 2 is reached via the absolute URL built from next_page_uri.
        responses_lib.add(
            responses_lib.GET,
            BASE + _MESSAGES_PAGE_1_NEXT,
            json=_MESSAGES_PAGE_2,
            status=200,
        )
        msgs = tw.list_messages(_ACCOUNT_SID, limit=50)
        # Both pages stitched: 1 + 1 = 2 messages.
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["sid"], "SM" + "aaaabbbbccccddddeeeeffffaaaabbbb")
        self.assertEqual(msgs[1]["sid"], "SM" + "00001111222233334444555566667777")
        # Two HTTP calls were made.
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_basic_auth_rides_link_follow(self):
        """The basic-auth credential is sent on BOTH the initial request and the next_page_uri follow."""
        responses_lib.add(responses_lib.GET, _MESSAGES_URL, json=_MESSAGES_PAGE_1, status=200)
        responses_lib.add(
            responses_lib.GET,
            BASE + _MESSAGES_PAGE_1_NEXT,
            json=_MESSAGES_PAGE_2,
            status=200,
        )
        tw.list_messages(_ACCOUNT_SID, limit=50)
        expected = _basic_header(_CRED)
        self.assertEqual(responses_lib.calls[0].request.headers["Authorization"], expected)
        self.assertEqual(responses_lib.calls[1].request.headers["Authorization"], expected)

    @responses_lib.activate
    def test_limit_stops_before_exhausting_pages(self):
        """When limit is satisfied on page 1, page 2 is not fetched."""
        responses_lib.add(responses_lib.GET, _MESSAGES_URL, json=_MESSAGES_PAGE_1, status=200)
        msgs = tw.list_messages(_ACCOUNT_SID, limit=1)
        self.assertEqual(len(msgs), 1)
        # Only one HTTP call: limit hit on first page, no follow.
        self.assertEqual(len(responses_lib.calls), 1)


class TwilioCallsAndNumbers(unittest.TestCase):
    """list_calls and list_numbers work with single-page responses."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_TWILIO")
        os.environ["RC_CONN_TWILIO"] = _CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_TWILIO", None)
        else:
            os.environ["RC_CONN_TWILIO"] = self._saved

    @responses_lib.activate
    def test_list_calls(self):
        responses_lib.add(responses_lib.GET, _CALLS_URL, json=_CALLS_PAGE, status=200)
        calls = tw.list_calls(_ACCOUNT_SID, limit=10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "completed")
        self.assertEqual(calls[0]["direction"], "outbound-api")

    @responses_lib.activate
    def test_list_numbers(self):
        responses_lib.add(responses_lib.GET, _NUMBERS_URL, json=_NUMBERS_PAGE, status=200)
        nums = tw.list_numbers(_ACCOUNT_SID, limit=10)
        self.assertEqual(len(nums), 1)
        self.assertEqual(nums[0]["phone_number"], "+14155552671")
        self.assertEqual(nums[0]["status"], "in-use")


class TwilioPick(unittest.TestCase):
    """api.pick selects support-relevant fields from message/call objects."""

    def test_pick_message_fields(self):
        msg = _MESSAGES_PAGE_1["messages"][0]
        picked = api.pick(msg, tw._MESSAGE_FIELDS)
        self.assertEqual(picked["sid"], "SM" + "aaaabbbbccccddddeeeeffffaaaabbbb")
        self.assertEqual(picked["status"], "delivered")
        self.assertEqual(picked["direction"], "outbound-api")
        self.assertIn("body", picked)

    def test_pick_call_fields(self):
        call = _CALLS_PAGE["calls"][0]
        picked = api.pick(call, tw._CALL_FIELDS)
        self.assertEqual(picked["status"], "completed")
        self.assertEqual(picked["duration"], "90")

    def test_pick_number_fields(self):
        num = _NUMBERS_PAGE["incoming_phone_numbers"][0]
        picked = api.pick(num, tw._NUMBER_FIELDS)
        self.assertEqual(picked["phone_number"], "+14155552671")
        self.assertEqual(picked["status"], "in-use")
        self.assertIn("capabilities", picked)


class TwilioCLI(unittest.TestCase):
    """CLI subcommands drive the connector and print markdown."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_TWILIO")
        os.environ["RC_CONN_TWILIO"] = _CRED

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_TWILIO", None)
        else:
            os.environ["RC_CONN_TWILIO"] = self._saved

    @responses_lib.activate
    def test_cli_messages(self):
        responses_lib.add(responses_lib.GET, _MESSAGES_URL, json=_MESSAGES_PAGE_2, status=200)
        rc = tw.main(["messages", "--limit", "5"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_cli_calls(self):
        responses_lib.add(responses_lib.GET, _CALLS_URL, json=_CALLS_PAGE, status=200)
        rc = tw.main(["calls", "--limit", "5"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_cli_numbers(self):
        responses_lib.add(responses_lib.GET, _NUMBERS_URL, json=_NUMBERS_PAGE, status=200)
        rc = tw.main(["numbers", "--limit", "10"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_cli_messages_with_filters(self):
        """Filters are forwarded as query params."""
        responses_lib.add(responses_lib.GET, _MESSAGES_URL, json=_MESSAGES_PAGE_2, status=200)
        rc = tw.main(["messages", "--to", "+14155552671", "--status", "delivered", "--limit", "5"])
        self.assertEqual(rc, 0)
        url = responses_lib.calls[0].request.url
        self.assertIn("To=%2B14155552671", url)
        self.assertIn("Status=delivered", url)


class TwilioTokenHygiene(unittest.TestCase):
    """No real Twilio AccountSid or AuthToken prefix may land in the connector dir files.

    Scopes to the connector dir only. This test file legitimately names the prefix it scans for,
    so we must NOT scan ourselves — we scan only the connector dir.
    """

    # Real Twilio AccountSids start with "AC" but that's generic. Real auth tokens have no
    # distinguished prefix — we guard against the composite pattern in committed artifacts.
    # The test credential uses a split literal so the guard itself doesn't trip.
    _CRED_COMPOSITE = "AC" + "test"  # split; only guard real-looking 34-char SIDs in practice

    def test_no_real_credentials_in_connector_dir(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "twilio"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            # A real AccountSid is 34 chars starting with AC. Guard against patterns that look like
            # a real token embedded in source (not the split-literal test cred above).
            # We check for the full 34-char "AC" + 32 hex chars pattern.
            import re
            if re.search(r"AC[0-9a-f]{32}", text):
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"real-looking AccountSid present in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
