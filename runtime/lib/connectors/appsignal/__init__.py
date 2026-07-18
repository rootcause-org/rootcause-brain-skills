"""AppSignal exception-grounding connector (read-only).

Resolves an 8-char error REFERENCE CODE, an exception name, a background job, or a controller
action into AppSignal exception samples / grouped incidents (class, message, backtrace, action,
occurrence count). Read-only: only ever GraphQL QUERIES and REST GETs — never a mutation.

Force-code trigger (e): AppSignal's public API is GraphQL (POST https://appsignal.com/graphql),
plus a REST endpoint for one sample's full backtrace. ``lib.api`` is GET/REST only, so this
connector issues the calls directly while reusing lib.api's retry/backoff (auth is the token in
the URL query param — AppSignal's documented scheme).

Unlike a project brain's own AppSignal helper, this central connector hardcodes NO org slug or app
ids — ``apps`` discovers what the token can reach, and every other command takes an explicit
``--org`` / ``--app``.

Credential: a personal API token injected as ``RC_CONN_APPSIGNAL`` and passed as ``?token=<value>``.

CLI:
    python -m lib.connectors.appsignal apps [--org SLUG]
    python -m lib.connectors.appsignal search QUERY --org SLUG [--pick a,b]
    python -m lib.connectors.appsignal incidents QUERY --app APP_ID [--since 1d] [--pick a,b]
    python -m lib.connectors.appsignal show SAMPLE_ID [--app APP_ID] [--pick a,b]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests as _requests

from lib import _http_audit, api, oauth

# ---------------------------------------------------------------------------
# Manifest (registers the connector so the YAML loader sees it and `python -m lib.api get
# appsignal …` resolves, even though the script owns every real call).
# ---------------------------------------------------------------------------

GRAPHQL_URL = "https://appsignal.com/graphql"
REST_BASE = "https://appsignal.com/api"

MANIFEST = api.register(
    api.Manifest(
        key="appsignal",
        base_url=GRAPHQL_URL,
        auth=api.Auth(strategy="query_param", name="token"),
        pagination=api.Pagination(style="none"),
        rate_limit_remaining_header="",
        default_headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
)

# `--since` shorthands → AppSignal GraphQL timeframe enums (server-side filtering).
_TIMEFRAME_MAP = {"1h": "R1H", "1d": "R24H", "2d": "R48H", "1w": "R7D", "30d": "R30D"}

# An error reference code is exactly 8 alphanumeric chars (the value a customer pastes).
_REF_RE = re.compile(r"^[A-Za-z0-9]{8}$")

# Request-env keys worth surfacing — the request shape, not the noisy full CGI dump.
_ENV_KEYS = (
    "REQUEST_METHOD",
    "REQUEST_PATH",
    "SERVER_NAME",
    "HTTP_USER_AGENT",
    "HTTP_ACCEPT_LANGUAGE",
    "HTTP_REFERER",
)

# Discovery: the organizations + apps the token can reach. This mirrors the schema AppSignal's own
# frontend uses; it is best-effort — if a schema drift makes it error, every other command still
# works with an explicit --org / --app (which the caller can read off the AppSignal UI).
_VIEWER_QUERY = """
query Viewer {
  viewer {
    organizations {
      id
      name
      slug
      apps { id name environment }
    }
  }
}
"""

# Org-wide sample search — verified against live AppSignal: the query string is passed bare (an
# 8-char reference code resolves directly), and `sampleType: EXCEPTION` is REQUIRED.
_SEARCH_QUERY = """
query Search($organizationSlug: String!, $query: String, $sampleType: SampleTypeEnum) {
  organization(slug: $organizationSlug) {
    search(query: $query, sampleType: $sampleType) {
      ... on ExceptionSample {
        id
        time
        action
        namespace
        exception { name message }
        incident { ... on ExceptionIncident { number } }
        app { id name }
      }
    }
  }
}
"""

# Grouped exception incidents for one app (occurrence counts + first backtrace line).
_INCIDENTS_QUERY = """
query PaginatedIncidents($appId: String!, $query: String, $timeframe: TimeframeEnum, $limit: Int) {
  app(id: $appId) {
    paginatedExceptionIncidents(query: $query, timeframe: $timeframe, limit: $limit, order: LAST) {
      total
      rows {
        number
        exceptionName
        exceptionMessage
        actionNames
        count
        lastOccurredAt
        state
        firstBacktraceLine
      }
    }
  }
}
"""

_rng = random.Random()


# ---------------------------------------------------------------------------
# Transport — token rides the URL (?token=…); retry/backoff reuse lib.api's policy.
# ---------------------------------------------------------------------------


def _retry_after(resp: "_requests.Response", attempt: int) -> float:
    delay = api.parse_retry_after(resp.headers.get("Retry-After"))
    if delay is None:
        delay = api._full_jitter(attempt, api.DEFAULT_BACKOFF_BASE, api.DEFAULT_BACKOFF_CAP, _rng)
    return min(delay, api.MAX_RETRY_AFTER)


def _send(method: str, url: str, *, safe_url: str, **kwargs) -> "_requests.Response":
    """Issue one request with 429/5xx + transient-network retry; return the 2xx response.

    The credential rides ``url`` as ``?token=…``, so a raw ``requests`` failure must NEVER surface:
    ``RequestException`` (ConnectionError/Timeout/SSLError/…) stringifies the full URL — token and
    all. Every ApiError raised here carries ``safe_url`` (token-stripped) instead, and the network
    branch uses ``from None`` so the chained traceback can't print the original token-bearing
    message either. Body text (``resp.text``) is the API's response, never the request URL.
    """
    attempt = 0
    reason = "initial"
    json_body = kwargs.pop("json", None)
    request_data = kwargs.pop("data", None)
    request_files = kwargs.pop("files", None)
    request_headers = kwargs.pop("headers", None)
    request_params = kwargs.pop("params", None)
    if kwargs:
        raise TypeError(f"unsupported request options: {', '.join(sorted(kwargs))}")
    url_token = (parse_qs(urlsplit(url).query).get("token") or [""])[0]
    while True:
        try:
            resp = _http_audit.request(
                method,
                url,
                params=request_params,
                headers=request_headers,
                json_body=json_body,
                data=request_data,
                files=request_files,
                timeout=(api.DEFAULT_CONNECT_TIMEOUT, api.DEFAULT_READ_TIMEOUT),
                attempt=attempt + 1,
                reason=reason,
                audit_url=safe_url,
                endpoint_template=urlsplit(safe_url).path or "/",
                known_secrets=(url_token,),
            )
        except _requests.exceptions.RequestException as e:
            if attempt < api.DEFAULT_MAX_RETRIES:
                time.sleep(api._full_jitter(attempt, api.DEFAULT_BACKOFF_BASE, api.DEFAULT_BACKOFF_CAP, _rng))
                reason = f"retry_transport_{type(e).__name__}"
                attempt += 1
                continue
            raise api.ApiError(0, f"network error after retries: {type(e).__name__}", url=safe_url) from None
        if resp.status_code == 429 and attempt < api.DEFAULT_MAX_RETRIES:
            time.sleep(_retry_after(resp, attempt))
            reason = "retry_status_429"
            attempt += 1
            continue
        if resp.status_code in (500, 502, 503, 504) and attempt < api.DEFAULT_MAX_RETRIES:
            time.sleep(api._full_jitter(attempt, api.DEFAULT_BACKOFF_BASE, api.DEFAULT_BACKOFF_CAP, _rng))
            reason = f"retry_status_{resp.status_code}"
            attempt += 1
            continue
        if not (200 <= resp.status_code < 300):
            raise api.ApiError(resp.status_code, resp.text, url=safe_url)
        return resp


def _gql(query: str, variables: dict | None = None) -> dict:
    """POST one GraphQL query; return the parsed ``data`` dict.

    Raises ``api.ApiError`` on an HTTP error or a GraphQL ``errors`` payload — AppSignal returns
    HTTP 200 even on a bad query, so the errors array is surfaced loudly rather than swallowed.
    """
    token = oauth.token("appsignal")
    body: dict[str, Any] = {"query": query}
    if variables:
        body["variables"] = variables

    resp = _send(
        "POST",
        f"{GRAPHQL_URL}?token={token}",
        safe_url=GRAPHQL_URL,
        json=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        parsed = resp.json()
    except ValueError:
        raise api.ApiError(resp.status_code, f"non-JSON response: {resp.text[:200]}", url=GRAPHQL_URL)
    gql_errors = parsed.get("errors")
    if gql_errors:
        msg = gql_errors[0].get("message", str(gql_errors[0]))
        raise api.ApiError(200, f"AppSignal GraphQL error: {msg}", url=GRAPHQL_URL)
    return parsed.get("data") or {}


def _rest_get(path: str) -> dict:
    """GET a REST endpoint (one sample's full detail); token rides the URL query param."""
    token = oauth.token("appsignal")
    sep = "&" if "?" in path else "?"
    safe_url = f"{REST_BASE}/{path}"
    resp = _send("GET", f"{safe_url}{sep}token={token}", safe_url=safe_url, headers={"Accept": "application/json"})
    try:
        return resp.json() or {}
    except ValueError:
        raise api.ApiError(resp.status_code, f"non-JSON response: {resp.text[:200]}", url=safe_url)


def _timeframe(since: str) -> str:
    return _TIMEFRAME_MAP.get((since or "").strip().lower(), "R24H")


# ---------------------------------------------------------------------------
# apps — discover organizations + apps the token can reach
# ---------------------------------------------------------------------------


def list_apps(org: str | None = None) -> dict:
    """Flatten the token's reachable apps into ``{"items": [...], "incomplete", "reason"}``.

    Each item: ``{org_slug, org_name, app_id, app_name, environment}``. Filter to one org with
    ``org`` (matched against slug or name).
    """
    try:
        data = _gql(_VIEWER_QUERY)
    except api.ApiError as e:
        return {"items": [], "incomplete": True, "reason": f"discovery failed ({e}); pass --org / --app explicitly"}

    orgs = ((data.get("viewer") or {}).get("organizations")) or []
    items: list[dict] = []
    for o in orgs:
        slug = o.get("slug")
        name = o.get("name")
        if org and org not in (slug, name):
            continue
        for app in o.get("apps") or []:
            items.append(
                {
                    "org_slug": slug,
                    "org_name": name,
                    "app_id": app.get("id"),
                    "app_name": app.get("name"),
                    "environment": app.get("environment"),
                }
            )
    return {"items": items, "incomplete": False, "reason": ""}


def _org_slugs(org: str | None) -> list[str]:
    """The org slug(s) to search. Explicit ``org`` wins; else discover all reachable orgs."""
    if org:
        return [org]
    discovered = list_apps()
    slugs = []
    for it in discovered["items"]:
        s = it.get("org_slug")
        if s and s not in slugs:
            slugs.append(s)
    if not slugs:
        reason = discovered.get("reason") or "no organizations reachable with this token"
        raise api.ApiError(0, f"cannot resolve an org slug: {reason}")
    return slugs


# ---------------------------------------------------------------------------
# search — org-wide exception-sample search (resolves reference codes too)
# ---------------------------------------------------------------------------


def is_reference(query: str) -> bool:
    """An AppSignal error reference code is exactly 8 alphanumeric chars."""
    return bool(_REF_RE.match((query or "").strip()))


def search(query: str, org: str | None = None, sample_type: str = "EXCEPTION") -> dict:
    """Org-wide exception-sample search. Returns ``{"items", "incomplete", "reason"}``.

    ``query`` may be an 8-char reference code, an exception name, a job, or a controller action —
    AppSignal resolves all of them through the same org ``search`` field (sampleType EXCEPTION
    required). Each item carries the **sample id** to hand to ``show`` for the full backtrace.
    With no ``org`` every reachable org is searched.
    """
    query = (query or "").strip()
    items: list[dict] = []
    incomplete = False
    reasons: list[str] = []

    try:
        slugs = _org_slugs(org)
    except api.ApiError as e:
        return {"items": [], "incomplete": True, "reason": str(e)}

    for slug in slugs:
        try:
            data = _gql(
                _SEARCH_QUERY,
                {"organizationSlug": slug, "query": query, "sampleType": sample_type},
            )
        except api.ApiError as e:
            incomplete = True
            reasons.append(f"{slug}: {e}")
            continue
        samples = ((data.get("organization") or {}).get("search")) or []
        for s in samples:
            s = dict(s)
            s["org_slug"] = slug
            items.append(s)

    return {"items": items, "incomplete": incomplete, "reason": "; ".join(reasons)}


# ---------------------------------------------------------------------------
# incidents — grouped exception incidents for one app
# ---------------------------------------------------------------------------


def incidents(query: str, app: str, since: str = "1d", limit: int = 20) -> dict:
    """Grouped exception incidents for one app. Returns ``{"items", "incomplete", "reason"}``."""
    if not app:
        return {"items": [], "incomplete": True, "reason": "--app <appId> is required (run `apps` to discover ids)"}
    try:
        data = _gql(
            _INCIDENTS_QUERY,
            {"appId": app, "query": query, "timeframe": _timeframe(since), "limit": limit},
        )
    except api.ApiError as e:
        return {"items": [], "incomplete": True, "reason": str(e)}
    rows = (((data.get("app") or {}).get("paginatedExceptionIncidents")) or {}).get("rows") or []
    return {"items": rows, "incomplete": False, "reason": ""}


# ---------------------------------------------------------------------------
# show — full detail for ONE sample (backtrace + params + request env)
# ---------------------------------------------------------------------------


def show(sample_id: str, app: str | None = None) -> dict:
    """Full detail for ONE exception sample via the REST sample endpoint.

    The app id is the prefix of the sample id (``<appId>-<rest>``), so ``app`` is usually
    optional. ``session_data`` is deliberately dropped — it carries the CSRF token + session
    secrets and is never grounding-relevant.
    """
    sample_id = (sample_id or "").strip()
    app_id = app or (sample_id.split("-", 1)[0] if "-" in sample_id else None)
    if not app_id:
        return {"error": "cannot derive app id — expected a '<appId>-<…>' sample id, or pass --app"}

    try:
        s = _rest_get(f"{app_id}/samples/{sample_id}.json")
    except api.ApiError as e:
        return {"error": str(e)}
    if not s:
        return {"error": "no sample with that id (older than retention, or wrong id)"}

    exc = s.get("exception") or {}
    backtrace = exc.get("backtrace") or []
    env = s.get("environment") or {}
    params = s.get("params")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            pass

    return {
        "sample_id": sample_id,
        "app_id": app_id,
        "exception": exc.get("name"),
        "message": exc.get("message"),
        "action": s.get("action"),
        "namespace": s.get("namespace"),
        "time": s.get("time"),
        "hostname": s.get("hostname"),
        "request_method": s.get("request_method"),
        "path": s.get("path"),
        "incident_id": s.get("incident_id"),
        "backtrace": backtrace,
        "params": params,
        "request_env": {k: env[k] for k in _ENV_KEYS if env.get(k)},
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_envelope(result: dict, pick_paths: str) -> None:
    if pick_paths and isinstance(result.get("items"), list):
        result = dict(result)
        result["items"] = [api.pick(it, pick_paths) for it in result["items"]]
    print(json.dumps(result, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the AppSignal connector."""
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.appsignal",
        description="Read AppSignal exceptions via GraphQL + REST (read-only).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    ap = sub.add_parser("apps", help="list organizations + apps the token can access")
    ap.add_argument("--org", default=None, help="filter to one organization (slug or name)")
    ap.add_argument("--pick", default="", help="comma-separated dotted paths to select")

    se = sub.add_parser("search", help="org-wide exception-sample search (reference code / name / action)")
    se.add_argument("query", help="8-char reference code, exception name, job, or controller#action")
    se.add_argument("--org", default=None, help="organization slug (default: every reachable org)")
    se.add_argument("--type", dest="sample_type", default="EXCEPTION", help="sample type (default EXCEPTION)")
    se.add_argument("--pick", default="", help="comma-separated dotted paths to select")

    inc = sub.add_parser("incidents", help="grouped exception incidents for one app")
    inc.add_argument("query", help="exception name, job, or controller#action (partial match)")
    inc.add_argument("--app", required=True, help="AppSignal app id (run `apps` to discover)")
    inc.add_argument("--since", default="1d", help="lookback: 1h/1d/2d/1w/30d (default 1d)")
    inc.add_argument("--limit", type=int, default=20, help="max incidents (default 20)")
    inc.add_argument("--pick", default="", help="comma-separated dotted paths to select")

    sh = sub.add_parser("show", help="full backtrace + params + request env for one sample id")
    sh.add_argument("sample_id", help="sample id (the app id is its prefix, so --app is optional)")
    sh.add_argument("--app", default=None, help="override the app id (rarely needed)")
    sh.add_argument("--pick", default="", help="comma-separated dotted paths to select")

    args = parser.parse_args(argv)

    if args.cmd == "apps":
        _print_envelope(list_apps(org=args.org), args.pick)
    elif args.cmd == "search":
        _print_envelope(search(args.query, org=args.org, sample_type=args.sample_type), args.pick)
    elif args.cmd == "incidents":
        _print_envelope(incidents(args.query, app=args.app, since=args.since, limit=args.limit), args.pick)
    elif args.cmd == "show":
        result = show(args.sample_id, app=args.app)
        if args.pick and "error" not in result:
            result = api.pick(result, args.pick)
        print(json.dumps(result, indent=2, default=str))
    else:
        parser.error(f"unknown command: {args.cmd!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
