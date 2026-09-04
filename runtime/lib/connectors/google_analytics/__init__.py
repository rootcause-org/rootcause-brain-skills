"""Google Analytics 4 + Search Console connector — read-only marketing/SEO grounding.

Force-code triggers: a multi-field credential (refresh token + OAuth client pair + the default
property/site, see ``lib.connectors._fields``), an OAuth refresh the runtime performs itself, three
API hosts behind one connector key, and POST-shaped reads (GA4 ``:runReport`` and GSC
``searchAnalytics/query`` are POST by design) that must stay inside an explicit read allowlist.

Read-only posture: GET everywhere, plus exactly the documented read POSTs below. Any other
POST/PUT/PATCH/DELETE is refused by ``lib.api``'s method policy — writes go through an action.

CLI:
    python -m lib.connectors.google_analytics ga-overview [--start 28daysAgo --end today]
    python -m lib.connectors.google_analytics ga-landing | ga-pages | ga-sources | ga-events | …
    python -m lib.connectors.google_analytics gsc-queries | gsc-pages | gsc-query …
    python -m lib.connectors.google_analytics api GET /v1beta/properties/123/metadata
    python -m lib.connectors.google_analytics mint --client-json client.json   # operator laptop
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote, urlparse

from lib import _http_audit, api
from lib.connectors import _fields, _tables

KEY = "google_analytics"
FIELD_NAMES = ("GWS_REFRESH_TOKEN", "GWS_CLIENT_ID", "GWS_CLIENT_SECRET",
               "GA_PROPERTY_ID", "GSC_SITE")

TOKEN_URL = "https://oauth2.googleapis.com/token"
DATA_HOST = "analyticsdata.googleapis.com"
ADMIN_HOST = "analyticsadmin.googleapis.com"
GSC_HOST = "searchconsole.googleapis.com"
ALLOWED_HOSTS = (DATA_HOST, ADMIN_HOST, GSC_HOST)

DATA = "/v1beta"      # relative to https://analyticsdata.googleapis.com
ADMIN = "/v1beta"     # relative to https://analyticsadmin.googleapis.com
GSC = "/webmasters/v3"
INSPECT = "/v1/urlInspection/index:inspect"

# The ONLY POST endpoints this connector may reach. Both the connector's own calls and the `api`
# escape hatch go through lib.api's allowlist check, so a non-read POST is refused with the
# action-plane hint instead of silently hitting Google.
_POST_ALLOW = {
    DATA_HOST: ("/v1beta/properties/*:runReport",
                "/v1beta/properties/*:runRealtimeReport",
                "/v1beta/properties/*:batchRunReports"),
    ADMIN_HOST: (),
    GSC_HOST: ("/webmasters/v3/sites/*/searchAnalytics/query",
               "/v1/urlInspection/index:inspect"),
}

# Shorthand path prefix -> host, for the generic `api` passthrough.
_PREFIX_HOST = (
    ("/v1beta/properties", DATA_HOST),
    ("/v1beta/accountSummaries", ADMIN_HOST),
    ("/admin/", ADMIN_HOST),
    ("/webmasters/", GSC_HOST),
    ("/v1/urlInspection", GSC_HOST),
)

DURATION_METRICS = {"averageSessionDuration", "userEngagementDuration"}
RATE_METRICS = {"engagementRate", "bounceRate", "conversionRate", "cartToViewRate", "purchaserRate"}
OVERVIEW_METRICS = "sessions,activeUsers,screenPageViews,engagementRate,averageSessionDuration"
REL_DATE = re.compile(r"^(\d+)daysAgo$")

RAW = False  # --csv: unformatted numbers so a spreadsheet can compute on them

_TOKEN: tuple[str, float] | None = None  # (access_token, expiry epoch) — in-process cache


# ---------------------------------------------------------------------------
# Credentials + OAuth refresh
# ---------------------------------------------------------------------------


def credentials() -> dict[str, str]:
    return _fields.fields(KEY, FIELD_NAMES)


def reset_token_cache() -> None:
    """Drop the cached access token (tests, and after a credential change)."""
    global _TOKEN
    _TOKEN = None


def _set_cached_token(token: str, expires_in: float) -> None:
    """Seed the cache — lets ``mint`` reuse its just-minted token for the discovery listings."""
    global _TOKEN
    _TOKEN = (token, time.time() + expires_in)


def access_token() -> str:
    """Return a cached access token, refreshing it from the refresh token when stale.

    Google access tokens live ~1h; one run does many calls, so we mint once and reuse.
    """
    global _TOKEN
    if _TOKEN and _TOKEN[1] > time.time() + 60:
        return _TOKEN[0]

    vals = credentials()
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": _fields.require(vals, "GWS_REFRESH_TOKEN", key=KEY),
        "client_id": _fields.require(vals, "GWS_CLIENT_ID", key=KEY),
        "client_secret": _fields.require(vals, "GWS_CLIENT_SECRET", key=KEY),
    }
    resp = _http_audit.request(
        "POST", TOKEN_URL, data=payload, timeout=(10, 30),
        endpoint_template="/token", known_secrets=tuple(payload.values()),
    )
    if resp.status_code != 200:
        try:
            err = resp.json()
        except ValueError:
            err = {}
        code = err.get("error", "")
        if code == "invalid_grant":
            raise RuntimeError(
                "Google refused the refresh token (invalid_grant): it was revoked, expired after "
                "6 months of disuse, or belongs to a different OAuth client. Re-mint it on a "
                "laptop with `python -m lib.connectors.google_analytics mint --client-json "
                "client.json` and paste the new fields into the connection."
            )
        raise RuntimeError(
            f"Google token refresh failed (HTTP {resp.status_code} {code or 'error'}): "
            f"{err.get('error_description', resp.text[:300])}"
        )
    body = resp.json()
    token = body.get("access_token", "")
    if not token:
        raise RuntimeError("Google token endpoint returned no access_token")
    _TOKEN = (token, time.time() + float(body.get("expires_in", 3600)))
    return token


def default_property() -> str:
    return _fields.require(credentials(), "GA_PROPERTY_ID", key=KEY)


def default_site() -> str:
    return _fields.require(credentials(), "GSC_SITE", key=KEY)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _client(host: str) -> api.Client:
    if host not in _POST_ALLOW:
        raise RuntimeError(f"refusing non-Google host {host!r}")
    manifest = api.Manifest(
        key=KEY,
        base_url=f"https://{host}",
        auth=api.Auth(strategy="bearer"),
        allowed_post_paths=_POST_ALLOW[host],
    )
    return api.Client(manifest=manifest, credential=access_token())


def call(method: str, host: str, path: str, *, params: dict | None = None,
         body: Any | None = None) -> Any:
    """One read call against a pinned Google host; POSTs must be in that host's read allowlist."""
    client = _client(host)
    try:
        return client.request(method.upper(), path, query=params or {}, json_body=body)
    except api.ApiError as exc:
        raise RuntimeError(_google_error(exc, host)) from exc


def _google_error(exc: api.ApiError, host: str) -> str:
    try:
        err = json.loads(exc.body).get("error") or {}
    except ValueError:
        err = {}
    message = err.get("message") or exc.body[:400]
    hint = ""
    if exc.status == 403:
        hint = ("\nhint: either the API is not enabled on your Google Cloud project "
                f"({host}), or the connected Google account lacks Viewer access on the GA4 "
                "property / Search Console property.")
    return f"Google {exc.status}: {message}{hint}"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def fmt_num(value: float, kind: str = "int") -> str:
    """kind: int | pct (fraction in, % out) | dec1"""
    if RAW:
        return f"{value:g}"
    return {"int": f"{value:,.0f}", "pct": f"{value * 100:.1f}%", "dec1": f"{value:.1f}"}[kind]


def fmt_metric(name: str, raw: str) -> str:
    if RAW:
        return str(raw)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    if name in DURATION_METRICS:
        return f"{int(value) // 60}:{int(value) % 60:02d}"
    if name in RATE_METRICS or name.endswith("Rate"):
        return f"{value * 100:.1f}%"
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _emit(rows: list[list[str]], a) -> None:
    _tables.emit(rows, getattr(a, "csv", False))


def abs_date(value: str) -> str:
    """GSC only accepts YYYY-MM-DD; translate the GA relative forms ourselves."""
    today = date.today()
    if value == "today":
        return today.isoformat()
    if value == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    m = REL_DATE.match(value)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise RuntimeError(f"bad date {value!r} (use YYYY-MM-DD, today, yesterday or NdaysAgo)")
    return value


def property_id(value: str | None = None) -> str:
    """Accept a bare id or an analytics.google.com URL (…/p472273541/…)."""
    raw = (value or default_property()).strip()
    if raw.isdigit():
        return raw
    found = re.search(r"/p(\d{6,})", raw) or re.search(r"properties/(\d+)", raw)
    if not found:
        raise RuntimeError(f"no GA4 property id found in {raw!r}")
    return found.group(1)


def site_path(site: str | None = None) -> str:
    return quote(site or default_site(), safe="")


# ---------------------------------------------------------------------------
# GA4
# ---------------------------------------------------------------------------

FILTER_OPS = [("==", "exact"), ("=~", "regex"), ("=^", "prefix"), ("=*", "contains")]
_MATCH_TYPE = {"exact": "EXACT", "regex": "FULL_REGEXP", "prefix": "BEGINS_WITH",
               "contains": "CONTAINS"}


def dimension_filter(specs: list[str] | None) -> dict | None:
    """'dim==v' exact · 'dim=~re' regex · 'dim=^v' beginsWith · 'dim=*v' contains."""
    filters = []
    for spec in specs or []:
        for token, kind in FILTER_OPS:
            if token in spec:
                name, _, value = spec.partition(token)
                string = {"matchType": _MATCH_TYPE[kind], "value": value}
                filters.append({"filter": {"fieldName": name.strip(), "stringFilter": string}})
                break
        else:
            raise RuntimeError(
                f"bad --filter {spec!r} (use dim==value, dim=~regex, dim=^prefix or dim=*contains)")
    if not filters:
        return None
    return filters[0] if len(filters) == 1 else {"andGroup": {"expressions": filters}}


def order_bys(spec: str | None, metrics: list[str]) -> list[dict] | None:
    if not spec:
        return None
    name, _, direction = spec.partition(":")
    key = ({"metric": {"metricName": name}} if name in metrics
           else {"dimension": {"dimensionName": name}})
    return [{**key, "desc": direction.lower() in ("desc", "d")}]


def run_report(a, dimensions: list[str], metrics: list[str],
               date_ranges: list[dict] | None = None) -> dict:
    body: dict[str, Any] = {
        "dateRanges": date_ranges or [{"startDate": a.start, "endDate": a.end}],
        "dimensions": [{"name": d} for d in dimensions],
        "metrics": [{"name": m} for m in metrics],
        "limit": a.limit,
        "metricAggregations": ["TOTAL"],
    }
    filt = dimension_filter(getattr(a, "filter", None))
    if filt:
        body["dimensionFilter"] = filt
    order = order_bys(getattr(a, "order_by", None), metrics)
    if order:
        body["orderBys"] = order
    path = f"{DATA}/properties/{property_id(getattr(a, 'property', None))}:runReport"
    return call("POST", DATA_HOST, path, body=body)


def report_rows(data: dict) -> list[list[str]]:
    dims = [h["name"] for h in data.get("dimensionHeaders", [])]
    mets = [h["name"] for h in data.get("metricHeaders", [])]
    rows = [[*dims, *mets]]
    for row in data.get("rows", []):
        rows.append(
            [v.get("value", "") for v in row.get("dimensionValues", [])]
            + [fmt_metric(mets[i], v.get("value", ""))
               for i, v in enumerate(row.get("metricValues", []))]
        )
    for total in data.get("totals", []):
        rows.append(
            ["TOTAL"] + [""] * (len(dims) - 1)
            + [fmt_metric(mets[i], v.get("value", ""))
               for i, v in enumerate(total.get("metricValues", []))]
        )
    return rows


def print_report(data: dict, a) -> None:
    if a.json:
        return _tables.dump(data)
    if not data.get("rows"):
        print("(no rows)")
        return
    _emit(report_rows(data), a)
    if data.get("rowCount", 0) > len(data.get("rows", [])):
        print(f"\n… {data['rowCount']:,} rows total (showing {len(data['rows'])}; raise --limit)",
              file=sys.stderr)


def cmd_ga_report(a):
    dims = [d.strip() for d in (a.dimensions or "").split(",") if d.strip()]
    mets = [m.strip() for m in a.metrics.split(",") if m.strip()]
    print_report(run_report(a, dims, mets), a)


def preset(dimensions: str, metrics: str, order: str | None = None, limit: int = 25):
    def run(a):
        a.dimensions = a.dimensions or dimensions
        a.metrics = a.metrics or metrics
        a.order_by = a.order_by or order
        a.limit = a.limit or limit
        cmd_ga_report(a)
    return run


def cmd_ga_compare(a):
    start, end = abs_date(a.start), abs_date(a.end)
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    prev_end = date.fromisoformat(start) - timedelta(days=1)
    ranges = [
        {"startDate": start, "endDate": end, "name": "current"},
        {"startDate": (prev_end - timedelta(days=days - 1)).isoformat(),
         "endDate": prev_end.isoformat(), "name": "previous"},
    ]
    metrics = [m.strip() for m in (a.metrics or OVERVIEW_METRICS).split(",")]
    data = run_report(a, [], metrics, date_ranges=ranges)
    if a.json:
        return _tables.dump(data)
    # with 2 dateRanges GA appends a synthetic "dateRange" dimension per row
    values: dict[str, list[str]] = {}
    for row in data.get("rows", []):
        values[row["dimensionValues"][0]["value"]] = [v.get("value", "0")
                                                      for v in row.get("metricValues", [])]
    cur, prev = values.get("current", []), values.get("previous", [])
    rows = [["METRIC", f"{ranges[0]['startDate']}..{ranges[0]['endDate']}",
             f"{ranges[1]['startDate']}..{ranges[1]['endDate']}", "DELTA"]]
    for i, m in enumerate(metrics):
        c, p = float(cur[i] or 0), float(prev[i] or 0)
        delta = f"{(c - p) / p * 100:+.1f}%" if p else ("n/a" if not c else "+∞")
        rows.append([m, fmt_metric(m, str(c)), fmt_metric(m, str(p)), delta])
    _emit(rows, a)


def cmd_ga_realtime(a):
    body = {
        "dimensions": [{"name": d.strip()} for d in (a.dimensions or "").split(",") if d.strip()],
        "metrics": [{"name": m.strip()} for m in a.metrics.split(",") if m.strip()],
        "limit": a.limit,
    }
    path = f"{DATA}/properties/{property_id(a.property)}:runRealtimeReport"
    print_report(call("POST", DATA_HOST, path, body=body), a)


def cmd_ga_properties(a):
    data = call("GET", ADMIN_HOST, f"{ADMIN}/accountSummaries", params={"pageSize": 200})
    if a.json:
        return _tables.dump(data)
    rows = [["PROPERTY_ID", "PROPERTY", "ACCOUNT"]]
    for acc in data.get("accountSummaries", []):
        for prop in acc.get("propertySummaries", []):
            rows.append([prop.get("property", "").split("/")[-1], prop.get("displayName", ""),
                         acc.get("displayName", "")])
    _emit(rows, a)


def cmd_ga_meta(a):
    path = f"{DATA}/properties/{property_id(a.property)}/metadata"
    data = call("GET", DATA_HOST, path)
    if a.json:
        return _tables.dump(data)
    needle = (a.search or "").lower()
    rows = [["KIND", "API NAME", "LABEL"]]
    for kind, key in (("dimension", "dimensions"), ("metric", "metrics")):
        for item in data.get(key, []):
            name, label = item.get("apiName", ""), item.get("uiName", "")
            haystack = f"{name} {label} {item.get('description', '')}".lower()
            if needle and needle not in haystack:
                continue
            rows.append([kind, name, _tables.trunc(label, 50)])
    _emit(rows, a)


# ---------------------------------------------------------------------------
# Search Console
# ---------------------------------------------------------------------------


def cmd_gsc_sites(a):
    data = call("GET", GSC_HOST, f"{GSC}/sites")
    if a.json:
        return _tables.dump(data)
    rows = [["SITE", "PERMISSION"]]
    for s in data.get("siteEntry", []):
        rows.append([s.get("siteUrl", ""), s.get("permissionLevel", "")])
    _emit(rows, a)


def gsc_query(a, dimensions: list[str]) -> dict:
    body: dict[str, Any] = {
        "startDate": abs_date(a.start),
        "endDate": abs_date(a.end),
        "dimensions": dimensions,
        "rowLimit": a.limit,
        "type": a.type,
    }
    filters = []
    for spec in a.filter or []:
        parts = spec.split(":", 2)
        if len(parts) != 3:
            raise RuntimeError(f"bad --filter {spec!r} (use dimension:operator:expression, "
                               "e.g. page:contains:/kampen/)")
        filters.append({"dimension": parts[0], "operator": parts[1], "expression": parts[2]})
    if filters:
        body["dimensionFilterGroups"] = [{"groupType": "and", "filters": filters}]
    path = f"{GSC}/sites/{site_path(a.site)}/searchAnalytics/query"
    return call("POST", GSC_HOST, path, body=body)


def cmd_gsc_query(a):
    dims = [d.strip() for d in (a.dimensions or "query").split(",") if d.strip()]
    data = gsc_query(a, dims)
    if a.json:
        return _tables.dump(data)
    rows = [[*[d.upper() for d in dims], "CLICKS", "IMPRESSIONS", "CTR", "POSITION"]]
    totals = [0.0, 0.0]
    for row in data.get("rows", []):
        totals[0] += row.get("clicks", 0)
        totals[1] += row.get("impressions", 0)
        rows.append([
            *[_tables.trunc(k, 70) for k in row.get("keys", [])],
            fmt_num(row.get("clicks", 0)),
            fmt_num(row.get("impressions", 0)),
            fmt_num(row.get("ctr", 0), "pct"),
            fmt_num(row.get("position", 0), "dec1"),
        ])
    if len(rows) == 1:
        print("(no rows — GSC data lags ~3 days; widen --start/--end)")
        return
    ctr = totals[0] / totals[1] if totals[1] else 0
    rows.append(["TOTAL"] + [""] * (len(dims) - 1)
                + [fmt_num(totals[0]), fmt_num(totals[1]), fmt_num(ctr, "pct"), ""])
    _emit(rows, a)
    print(f"\n{abs_date(a.start)}..{abs_date(a.end)} · GSC data lags ~3 days, retention 16 months",
          file=sys.stderr)


def gsc_preset(dimensions: str, limit: int = 25):
    def run(a):
        a.dimensions = a.dimensions or dimensions
        a.limit = a.limit or limit
        cmd_gsc_query(a)
    return run


def cmd_gsc_sitemaps(a):
    data = call("GET", GSC_HOST, f"{GSC}/sites/{site_path(a.site)}/sitemaps")
    if a.json:
        return _tables.dump(data)
    rows = [["SITEMAP", "LAST SUBMITTED", "LAST DOWNLOADED", "ERRORS", "WARNINGS", "SUBMITTED URLS"]]
    for s in data.get("sitemap", []):
        submitted = sum(int(c.get("submitted", 0)) for c in s.get("contents", []))
        rows.append([
            s.get("path", ""), s.get("lastSubmitted", "")[:10], s.get("lastDownloaded", "")[:10],
            s.get("errors", "0"), s.get("warnings", "0"), f"{submitted:,}",
        ])
    _emit(rows, a)


def cmd_gsc_inspect(a):
    body = {"inspectionUrl": a.url, "siteUrl": a.site or default_site(),
            "languageCode": a.language}
    data = call("POST", GSC_HOST, INSPECT, body=body)
    if a.json:
        return _tables.dump(data)
    result = data.get("inspectionResult", {})
    index = result.get("indexStatusResult", {})
    rows = [
        ["verdict", index.get("verdict", "")],
        ["coverage", index.get("coverageState", "")],
        ["indexing state", index.get("indexingState", "")],
        ["robots.txt", index.get("robotsTxtState", "")],
        ["last crawl", index.get("lastCrawlTime", "")],
        ["crawled as", index.get("crawledAs", "")],
        ["google canonical", _tables.trunc(index.get("googleCanonical", ""), 90)],
        ["user canonical", _tables.trunc(index.get("userCanonical", ""), 90)],
        ["sitemaps", ", ".join(index.get("sitemap", [])) or "-"],
    ]
    for key, label in (("mobileUsabilityResult", "mobile usability"),
                       ("richResultsResult", "rich results")):
        if result.get(key):
            rows.append([label, result[key].get("verdict", "")])
    _emit([["FIELD", "VALUE"]] + [r for r in rows if r[1]], a)
    if result.get("inspectionResultLink"):
        print(f"\n{result['inspectionResultLink']}")


# ---------------------------------------------------------------------------
# Escape hatch
# ---------------------------------------------------------------------------


def _resolve(url: str) -> tuple[str, str]:
    """Map a full URL or a shorthand path onto (host, path), refusing anything off the four hosts."""
    if url.startswith(("http://", "https://")):
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
                or parsed.port not in (None, 443)):
            raise RuntimeError(f"absolute URLs must be HTTPS on one of {', '.join(ALLOWED_HOSTS)}")
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        return parsed.hostname, path
    host = next((h for p, h in _PREFIX_HOST if url.startswith(p)), "")
    if not host:
        raise RuntimeError("unknown path prefix; use a full https URL or /v1beta/…, "
                           "/webmasters/…, /v1/urlInspection…")
    return host, url


def read_body(spec: str) -> Any:
    text = sys.stdin.read() if spec == "-" else open(spec, encoding="utf-8").read()
    return json.loads(text)


def cmd_api(a):
    host, path = _resolve(a.url)
    params = {}
    for kv in a.param or []:
        key, _, value = kv.partition("=")
        params[key] = value
    body = read_body(a.body) if a.body else None
    _tables.dump(call(a.method, host, path, params=params, body=body))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    g = argparse.ArgumentParser(add_help=False)
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="raw API JSON")
    g.add_argument("--csv", action="store_true", default=argparse.SUPPRESS,
                   help="print tables as CSV")

    p = argparse.ArgumentParser(
        prog="python -m lib.connectors.google_analytics",
        description="Read-only GA4 + Search Console connector.", parents=[g])
    sub = p.add_subparsers(dest="cmd", required=True,
                           parser_class=lambda **kw: argparse.ArgumentParser(parents=[g], **kw))

    def add(name: str, fn, help_: str):
        s = sub.add_parser(name, help=help_, description=help_)
        s.set_defaults(fn=fn)
        return s

    def ga_common(s, limit_default=None):
        s.add_argument("--property", help="GA4 property id or analytics.google.com URL "
                                          "(default: the connected property)")
        s.add_argument("--start", default="28daysAgo",
                       help="YYYY-MM-DD, today, yesterday or NdaysAgo")
        s.add_argument("--end", default="today")
        s.add_argument("--limit", type=int, default=limit_default)
        s.add_argument("--filter", action="append", metavar="EXPR",
                       help="dim==value | dim=~regex | dim=^prefix | dim=*contains (repeatable)")
        s.add_argument("--order-by", metavar="NAME[:desc]")
        return s

    def gsc_common(s, limit_default=None):
        s.add_argument("--site", help="Search Console property (default: the connected site)")
        s.add_argument("--start", default="28daysAgo")
        s.add_argument("--end", default="3daysAgo", help="GSC data lags ~3 days")
        s.add_argument("--limit", type=int, default=limit_default)
        s.add_argument("--filter", action="append", metavar="DIM:OP:EXPR",
                       help="e.g. page:contains:/pricing/ or query:equals:kamp (repeatable)")
        s.add_argument("--type", default="web",
                       choices=["web", "image", "video", "news", "discover", "googleNews"])
        return s

    ga_common(add("ga-report", cmd_ga_report, "custom GA4 report")).add_argument(
        "--dimensions", help="comma list, e.g. date,pagePath")
    sub.choices["ga-report"].add_argument("--metrics", required=True,
                                          help="comma list, e.g. sessions,activeUsers")
    sub.choices["ga-report"].set_defaults(limit=50)

    presets = {
        "ga-overview": ("traffic per day + totals", "date", OVERVIEW_METRICS, "date", 60),
        "ga-pages": ("most viewed pages", "pagePath", "screenPageViews,activeUsers",
                     "screenPageViews:desc", 25),
        "ga-landing": ("landing pages", "landingPage", "sessions,activeUsers,engagementRate",
                       "sessions:desc", 25),
        "ga-sources": ("traffic sources", "sessionSource,sessionMedium", "sessions,activeUsers",
                       "sessions:desc", 25),
        "ga-events": ("events", "eventName", "eventCount", "eventCount:desc", 25),
        "ga-devices": ("device categories", "deviceCategory", "sessions", "sessions:desc", 10),
        "ga-countries": ("countries", "country", "sessions", "sessions:desc", 15),
    }
    for name, (help_, dims, mets, order, limit) in presets.items():
        s = ga_common(add(name, preset(dims, mets, order, limit), help_))
        s.add_argument("--dimensions", help=f"override (default {dims})")
        s.add_argument("--metrics", help=f"override (default {mets})")

    s = ga_common(add("ga-compare", cmd_ga_compare,
                      "current period vs previous period of equal length"))
    s.add_argument("--metrics", help=f"comma list (default {OVERVIEW_METRICS})")
    s.set_defaults(limit=10, dimensions=None)

    s = add("ga-realtime", cmd_ga_realtime, "last 30 minutes")
    s.add_argument("--property")
    s.add_argument("--dimensions", help="e.g. unifiedScreenName,country")
    s.add_argument("--metrics", default="activeUsers")
    s.add_argument("--limit", type=int, default=25)

    add("ga-properties", cmd_ga_properties, "GA4 accounts/properties visible to the credential")

    s = add("ga-meta", cmd_ga_meta, "available GA4 dimension/metric api names")
    s.add_argument("--property")
    s.add_argument("--search", help="substring filter")

    add("gsc-sites", cmd_gsc_sites, "Search Console properties + permission level")
    gsc_common(add("gsc-query", cmd_gsc_query, "custom Search Console query")).add_argument(
        "--dimensions", default="query", help="query|page|country|device|date|searchAppearance")
    sub.choices["gsc-query"].set_defaults(limit=50)

    for name, (help_, dims, limit) in {
        "gsc-overview": ("clicks/impressions per day", "date", 90),
        "gsc-queries": ("top search queries", "query", 25),
        "gsc-pages": ("top landing pages from search", "page", 25),
        "gsc-countries": ("search traffic per country", "country", 15),
        "gsc-devices": ("search traffic per device", "device", 10),
    }.items():
        s = gsc_common(add(name, gsc_preset(dims, limit), help_))
        s.add_argument("--dimensions", help=f"override (default {dims})")

    s = add("gsc-sitemaps", cmd_gsc_sitemaps, "submitted sitemaps + errors")
    s.add_argument("--site")

    s = add("gsc-inspect", cmd_gsc_inspect,
            "URL inspection: indexing status, last crawl, canonical")
    s.add_argument("url")
    s.add_argument("--site")
    s.add_argument("--language", default="en", help="inspection language code (default en)")

    s = add("api", cmd_api, "authenticated raw GET / allowlisted read POST against the "
                            "analytics + searchconsole hosts")
    s.add_argument("method")
    s.add_argument("url")
    s.add_argument("--body", help="JSON request body: FILE or - for stdin")
    s.add_argument("--param", action="append", metavar="k=v")

    from . import _mint

    _mint.add_parser(add)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    global RAW
    a.json = getattr(a, "json", False)
    a.csv = getattr(a, "csv", False)
    RAW = a.csv
    try:
        a.fn(a)
    except (RuntimeError, api.MethodPolicyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:  # `| head` is a normal way to use this CLI
        import os

        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0
