"""Fixture tests for the Recurly integration — manifest-only, driven via the generic lib.api path.

There is no Recurly Python module anymore: the integration is the manifest row. lib.api's
``body_url`` pagination style follows the body ``next`` field — for Recurly a RELATIVE path like
``/accounts?cursor=abc&limit=200`` (null when exhausted). Because base_url has NO path segment,
lib.api's ``_join`` turns ``/accounts?…`` into ``https://v3.recurly.com/accounts?…`` (host
preserved). That relative-join is asserted explicitly below.

No live creds, no network: HTTP is mocked with ``responses``. Bodies mirror Recurly's documented
v2021-02-25 API example payloads, trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_recurly_connector.py -q
"""

import base64
import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://v3.recurly.com"

# Documented example payloads trimmed to support-relevant fields (Recurly v2021-02-25 shapes).

_ACCOUNT_1 = {
    "object": "account",
    "id": "abcd1234-0000-0000-0000-000000000001",
    "code": "usr_42",
    "email": "alice@example.com",
    "company": "Acme Corp",
    "state": "active",
    "balance": {"amount": 0.0, "currency": "USD"},
    "created_at": "2024-01-15T10:00:00Z",
}

_ACCOUNT_2 = {
    "object": "account",
    "id": "abcd1234-0000-0000-0000-000000000002",
    "code": "usr_99",
    "email": "bob@example.com",
    "company": None,
    "state": "active",
    "balance": {"amount": 10.0, "currency": "USD"},
    "created_at": "2024-02-01T10:00:00Z",
}

# Page 1's `next` is a RELATIVE path; lib.api joins it onto base_url, preserving the host.
_NEXT_RELATIVE = "/accounts?cursor=abc123&limit=200"
_NEXT_JOINED = f"{BASE}{_NEXT_RELATIVE}"  # what _join must produce: host preserved

_ACCOUNTS_PAGE_1 = {
    "object": "list",
    "has_more": True,
    "next": _NEXT_RELATIVE,
    "data": [_ACCOUNT_1],
}
_ACCOUNTS_PAGE_2 = {
    "object": "list",
    "has_more": False,
    "next": None,
    "data": [_ACCOUNT_2],
}


class _RecurlyBase(unittest.TestCase):
    def setUp(self):
        # YAML loader is the sole source of truth each test.
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_RECURLY")
        # Encodes cleanly as Basic "apikey:". Avoids any real key prefix.
        os.environ["RC_CONN_RECURLY"] = "test_api_key_dummy"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_RECURLY", None)
        else:
            os.environ["RC_CONN_RECURLY"] = self._saved


# ---------------------------------------------------------------------------
# 1. Manifest loading
# ---------------------------------------------------------------------------

class TestRecurlyManifest(_RecurlyBase):
    def test_yaml_loads_and_maps_every_field(self):
        m = api.load_manifests()
        self.assertIn("recurly", m)
        r = m["recurly"]
        self.assertEqual(r.base_url, "https://v3.recurly.com")
        self.assertEqual(r.auth.strategy, "basic")
        self.assertEqual(r.pagination.style, "body_url")
        self.assertEqual(r.pagination.next_url_field, "next")
        self.assertEqual(r.pagination.items_field, "data")
        self.assertEqual(r.pagination.page_size, 200)
        self.assertEqual(r.rate_limit_remaining_header, "X-RateLimit-Remaining")
        self.assertIn("Accept", r.default_headers)
        self.assertIn("recurly", r.default_headers["Accept"])


# ---------------------------------------------------------------------------
# 2. body_url pagination with a RELATIVE next path (the recurly-critical case)
# ---------------------------------------------------------------------------

class TestRecurlyPagination(_RecurlyBase):
    @responses_lib.activate
    def test_two_page_list_stitches_items(self):
        """collect() follows has_more/next relative path across two pages, in order."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts",
            json=_ACCOUNTS_PAGE_1, status=200,
            headers={"X-RateLimit-Remaining": "1999"},
        )
        # Register page 2 as the FULL joined URL — proves the relative next was joined onto base_url.
        responses_lib.add(
            responses_lib.GET, _NEXT_JOINED,
            json=_ACCOUNTS_PAGE_2, status=200,
            headers={"X-RateLimit-Remaining": "1998"},
        )

        m = api.load_manifests()["recurly"]
        result = api.client(m, token_key="recurly").collect("/accounts")

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual([it["code"] for it in result["items"]], ["usr_42", "usr_99"])
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_relative_next_path_joined_onto_base_url_host_preserved(self):
        """CRITICAL: the relative `next` path is joined onto base_url, preserving the host.

        base_url has no path segment, so `/accounts?cursor=…` -> `https://v3.recurly.com/accounts?cursor=…`.
        The continuation request URL must be exactly that — same host, scheme, and query.
        """
        responses_lib.add(responses_lib.GET, f"{BASE}/accounts", json=_ACCOUNTS_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, _NEXT_JOINED, json=_ACCOUNTS_PAGE_2, status=200)

        m = api.load_manifests()["recurly"]
        api.client(m, token_key="recurly").collect("/accounts")

        self.assertEqual(len(responses_lib.calls), 2)
        cont_url = responses_lib.calls[1].request.url
        self.assertEqual(cont_url, f"{BASE}/accounts?cursor=abc123&limit=200")
        # Host explicitly preserved (not dropped, not pointed at a relative/bogus host).
        from urllib.parse import urlparse
        self.assertEqual(urlparse(cont_url).netloc, "v3.recurly.com")
        self.assertEqual(urlparse(cont_url).scheme, "https")

    @responses_lib.activate
    def test_basic_auth_on_every_request_including_continuation(self):
        """Basic auth Authorization header rides every request, including the relative-next follow."""
        expected_auth = "Basic " + base64.b64encode(b"test_api_key_dummy:").decode()
        responses_lib.add(responses_lib.GET, f"{BASE}/accounts", json=_ACCOUNTS_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, _NEXT_JOINED, json=_ACCOUNTS_PAGE_2, status=200)

        m = api.load_manifests()["recurly"]
        api.client(m, token_key="recurly").collect("/accounts")

        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers.get("Authorization"), expected_auth)

    @responses_lib.activate
    def test_accept_version_header_on_every_request(self):
        """The pinned Recurly-version Accept header rides every request."""
        responses_lib.add(responses_lib.GET, f"{BASE}/accounts", json=_ACCOUNTS_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, _NEXT_JOINED, json=_ACCOUNTS_PAGE_2, status=200)

        m = api.load_manifests()["recurly"]
        api.client(m, token_key="recurly").collect("/accounts")

        for call in responses_lib.calls:
            self.assertIn("recurly", call.request.headers.get("Accept", "").lower())

    @responses_lib.activate
    def test_single_page_no_continuation(self):
        """next=null on first page stops pagination immediately."""
        responses_lib.add(
            responses_lib.GET, f"{BASE}/accounts", json=_ACCOUNTS_PAGE_2, status=200,
        )
        m = api.load_manifests()["recurly"]
        result = api.client(m, token_key="recurly").collect("/accounts")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses_lib.calls), 1)


# ---------------------------------------------------------------------------
# 3. api.pick on Recurly objects
# ---------------------------------------------------------------------------

class TestRecurlyPick(_RecurlyBase):
    def test_pick_account_fields(self):
        picked = api.pick(_ACCOUNT_1, "id,code,email,state,balance.amount,balance.currency")
        self.assertEqual(picked["code"], "usr_42")
        self.assertEqual(picked["email"], "alice@example.com")
        self.assertEqual(picked["state"], "active")
        self.assertEqual(picked["balance.amount"], 0.0)
        self.assertEqual(picked["balance.currency"], "USD")


# ---------------------------------------------------------------------------
# 4. Generic CLI drive
# ---------------------------------------------------------------------------

class TestRecurlyCLI(_RecurlyBase):
    @responses_lib.activate
    def test_cli_paginate_stitches_pages(self):
        """`get recurly /accounts --paginate` stitches pages, basic auth on both, host preserved."""
        responses_lib.add(responses_lib.GET, f"{BASE}/accounts", json=_ACCOUNTS_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, _NEXT_JOINED, json=_ACCOUNTS_PAGE_2, status=200)

        expected_auth = "Basic " + base64.b64encode(b"test_api_key_dummy:").decode()
        rc = api._main(["get", "recurly", "/accounts", "--paginate", "--pick", "code,state"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)
        self.assertEqual(responses_lib.calls[1].request.url, f"{BASE}/accounts?cursor=abc123&limit=200")
        for call in responses_lib.calls:
            self.assertEqual(call.request.headers.get("Authorization"), expected_auth)

    @responses_lib.activate
    def test_cli_single_account_get(self):
        """Single-item GET (no --paginate) hits /accounts/<code> with basic auth."""
        responses_lib.add(responses_lib.GET, f"{BASE}/accounts/usr_42", json=_ACCOUNT_1, status=200)
        rc = api._main(["get", "recurly", "/accounts/usr_42"])
        self.assertEqual(rc, 0)
        expected_auth = "Basic " + base64.b64encode(b"test_api_key_dummy:").decode()
        self.assertEqual(responses_lib.calls[0].request.headers.get("Authorization"), expected_auth)


# ---------------------------------------------------------------------------
# 5. Token-prefix hygiene
# ---------------------------------------------------------------------------

class TestRecurlyHygiene(unittest.TestCase):
    """CI guard: no real Recurly API key prefix may land in the connector dir files.

    Scoped to the connector dir (only manifest.yaml remains) — this test file legitimately names
    the prefixes it hunts for, so scanning itself would be a false positive.
    """

    # Split the literals to avoid triggering the guard on this source file.
    _TOKEN_PREFIXES = ("recurly-private" "-", "rc_" "priv_")

    def test_no_token_prefixes_in_recurly_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "recurly"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file() or path.suffix == ".pyc":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
