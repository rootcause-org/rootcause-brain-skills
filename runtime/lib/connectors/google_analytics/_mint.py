"""``google_analytics mint`` — the OPERATOR-laptop step that produces the connection's five fields.

This is the only place in the connector that prints secrets, and it is deliberate: it runs on the
operator's own machine (it needs a browser and a loopback listener), and its whole output is the
credential the operator pastes into ReplyPen. It has no place inside a run workspace and simply
fails there — no browser, no loopback.

Google has no ReplyPen-verified OAuth app for the analytics scopes yet, so the customer supplies a
"Desktop app" OAuth client from a Cloud project they own and we mint an offline refresh token
against it.
"""

from __future__ import annotations

import http.server
import json
import secrets
import socket
import threading
import urllib.parse
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ("https://www.googleapis.com/auth/analytics.readonly",
          "https://www.googleapis.com/auth/webmasters.readonly")


def add_parser(add) -> None:
    s = add("mint", cmd_mint,
            "OPERATOR LAPTOP ONLY: browser OAuth flow that prints the connection fields "
            "(including secrets) for pasting into ReplyPen. Needs a browser; fails in a workspace.")
    s.add_argument("--client-json", help="the Google 'Desktop app' OAuth client JSON download")
    s.add_argument("--client-id")
    s.add_argument("--client-secret")
    s.add_argument("--property", help="GA4 property id (omitted: the flow lists what you can see)")
    s.add_argument("--site", help="Search Console property, e.g. sc-domain:example.com")


def _client_pair(a) -> tuple[str, str]:
    if a.client_json:
        with open(a.client_json, encoding="utf-8") as fh:
            raw = json.load(fh)
        block = raw.get("installed") or raw.get("web") or raw
        cid, secret = block.get("client_id", ""), block.get("client_secret", "")
        if not (cid and secret):
            raise RuntimeError(f"{a.client_json} has no client_id/client_secret "
                               "(download the OAuth client of type 'Desktop app')")
        return cid, secret
    if a.client_id and a.client_secret:
        return a.client_id, a.client_secret
    raise RuntimeError("pass --client-json client.json, or --client-id and --client-secret")


class _Handler(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        _Handler.result = {k: v[0] for k, v in query.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"authentication flow has completed - you can close this tab")

    def log_message(self, *args):  # silence the default stderr access log
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _authorize(client_id: str) -> tuple[str, str]:
    """Run the loopback consent flow; returns (code, redirect_uri)."""
    port = _free_port()
    redirect_uri = f"http://localhost:{port}/"
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    _Handler.result = {}
    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("Open this URL and consent as the Google account that has access to the GA4 property "
          "and the Search Console property:\n")
    print(url, "\n", flush=True)
    try:
        webbrowser.open(url)
    except Exception:  # headless box: the printed URL is the fallback
        pass

    thread.join(timeout=300)
    server.server_close()
    result = _Handler.result
    if not result:
        raise RuntimeError("timed out waiting for the OAuth redirect (5 min)")
    if result.get("state") != state:
        raise RuntimeError("OAuth state mismatch — restart the flow")
    if result.get("error"):
        raise RuntimeError(f"Google returned {result['error']}")
    code = result.get("code", "")
    if not code:
        raise RuntimeError("no authorization code in the redirect")
    return code, redirect_uri


def _exchange(code: str, redirect_uri: str, client_id: str,
              client_secret: str) -> tuple[str, str, float]:
    """Swap the authorization code for (refresh_token, access_token, expires_in)."""
    from lib import _http_audit

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    resp = _http_audit.request("POST", TOKEN_URL, data=payload, timeout=(10, 30),
                               endpoint_template="/token",
                               known_secrets=(code, client_secret))
    body = resp.json() if resp.content else {}
    if resp.status_code != 200:
        raise RuntimeError(
            f"token exchange failed: {body.get('error_description') or resp.text[:300]}")
    refresh = body.get("refresh_token", "")
    if not refresh:
        raise RuntimeError("Google returned no refresh_token — revoke this app at "
                           "myaccount.google.com/permissions and re-run (a prior grant can "
                           "suppress it even with prompt=consent)")
    return refresh, body.get("access_token", ""), float(body.get("expires_in", 3600))


def cmd_mint(a) -> None:
    from . import ADMIN, ADMIN_HOST, GSC, GSC_HOST, call, _set_cached_token

    client_id, client_secret = _client_pair(a)
    code, redirect_uri = _authorize(client_id)
    refresh_token, access_token, expires_in = _exchange(code, redirect_uri, client_id, client_secret)
    _set_cached_token(access_token, expires_in)

    prop, site = a.property or "", a.site or ""
    if not prop or not site:
        print("\nWhat this grant can see:\n")
        try:
            summaries = call("GET", ADMIN_HOST, f"{ADMIN}/accountSummaries", params={"pageSize": 200})
            for acc in summaries.get("accountSummaries", []):
                for p in acc.get("propertySummaries", []):
                    print(f"  GA4 property {p.get('property', '').split('/')[-1]}  "
                          f"{p.get('displayName', '')}  ({acc.get('displayName', '')})")
        except RuntimeError as exc:
            print(f"  (GA4 properties unavailable: {exc})")
        try:
            for s in call("GET", GSC_HOST, f"{GSC}/sites").get("siteEntry", []):
                print(f"  GSC site {s.get('siteUrl', '')}  ({s.get('permissionLevel', '')})")
        except RuntimeError as exc:
            print(f"  (Search Console sites unavailable: {exc})")
        print("\nRe-run with --property/--site to bake the choice into the printed fields, or fill "
              "them into the JSON below by hand.\n")

    print("Paste these fields into the ReplyPen connection (they contain secrets — "
          "treat them like a password):\n")
    print(json.dumps({
        "GWS_REFRESH_TOKEN": refresh_token,
        "GWS_CLIENT_ID": client_id,
        "GWS_CLIENT_SECRET": client_secret,
        "GA_PROPERTY_ID": prop,
        "GSC_SITE": site,
    }, indent=2))
