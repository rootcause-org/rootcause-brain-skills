"""Meta (Facebook/Instagram) Ads connector — read-only marketing grounding.

Force-code triggers: a multi-field credential (system-user token + ad account id, see
``lib.connectors._fields``), Graph's `paging.next` continuation (an opaque absolute URL that must be
re-pinned to graph.facebook.com), and heavy field pre-selection — insights come back with a nested
``actions[]`` array that only becomes readable once flattened into ``action:<type>`` columns.

Read-only posture: GET only, host pinned to ``graph.facebook.com``. Writes (creating/pausing a
campaign, editing a budget) are action-plane work, never this connector.

CLI:
    python -m lib.connectors.meta_ads whoami
    python -m lib.connectors.meta_ads campaigns [--status ACTIVE]
    python -m lib.connectors.meta_ads adsets [--campaign ID] | ads [--campaign ID|--adset ID]
    python -m lib.connectors.meta_ads overview [--preset last_30d | --since D --until D] [--increment monthly]
    python -m lib.connectors.meta_ads insights --level campaign [--breakdown publisher_platform]
    python -m lib.connectors.meta_ads page [--page ID]
    python -m lib.connectors.meta_ads api GET /act_123/insights --param level=ad
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any
from urllib.parse import urlparse

from lib import api
from lib.connectors import _fields, _tables

KEY = "meta_ads"
FIELD_NAMES = ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID")

HOST = "graph.facebook.com"
API_VERSION = "v24.0"
BASE = f"https://{HOST}/{API_VERSION}"
MAX_PAGES = 20  # paging.next follow cap — a runaway cursor can't loop forever

METRICS = ["spend", "impressions", "reach", "frequency", "clicks",
           "inline_link_clicks", "ctr", "cpc", "cpm"]
INSIGHT_FIELDS = METRICS + ["actions"]
KEY_ACTIONS = ["link_click", "landing_page_view", "post_engagement", "lead"]
INCREMENTS = {"daily": "1", "weekly": "7", "monthly": "monthly", "all": "all_days"}
BREAKDOWNS = ["age", "gender", "country", "region", "publisher_platform",
              "platform_position", "device_platform", "impression_device"]
MONEY = {"spend", "cpc", "cpm"}
RATES = {"ctr"}
DEC = {"frequency"}

RAW = False  # --csv: unformatted numbers so a spreadsheet can compute on them


# ---------------------------------------------------------------------------
# Credentials + transport
# ---------------------------------------------------------------------------


def credentials() -> dict[str, str]:
    return _fields.fields(KEY, FIELD_NAMES)


def default_account() -> str:
    """The ad account from the credential, normalized to ``act_<id>``."""
    acc = _fields.require(credentials(), "META_AD_ACCOUNT_ID", key=KEY)
    return acc if acc.startswith("act_") else f"act_{acc}"


def _client(token: str | None = None) -> api.Client:
    """A lib.api client (retry/backoff/audit live there) presenting the token as a Bearer header.

    Graph also accepts ``?access_token=``; the header keeps the secret out of URLs entirely.
    """
    cred = token or _fields.require(credentials(), "META_ACCESS_TOKEN", key=KEY)
    manifest = api.Manifest(key=KEY, base_url=BASE, auth=api.Auth(strategy="bearer"))
    return api.Client(manifest=manifest, credential=cred)


def _pin(url: str) -> str:
    """Refuse any absolute URL that is not HTTPS on graph.facebook.com (incl. a paging.next)."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != HOST or parsed.port not in (None, 443):
        raise RuntimeError(f"refusing non-{HOST} URL: {url}")
    return url


def _call(client: api.Client, url: str, params: dict) -> Any:
    try:
        return client.get(url, query=params)
    except api.ApiError as exc:
        try:
            err = json.loads(exc.body).get("error") or {}
        except ValueError:
            err = {}
        if err:
            raise RuntimeError(f"Graph {err.get('code')}: {err.get('message')}") from exc
        raise RuntimeError(f"Graph HTTP {exc.status}: {exc.body[:400]}") from exc


def get(path: str, params: dict | None = None, *, token: str | None = None,
        paginate: bool = True) -> Any:
    """GET a Graph edge, transparently following ``paging.next`` up to ``MAX_PAGES``."""
    client = _client(token)
    url = _pin(path) if path.startswith(("http://", "https://")) else path
    query = dict(params or {})
    rows: list[dict] = []
    first: Any = None
    for _ in range(MAX_PAGES):
        data = _call(client, url, query)
        if first is None:
            first = data
        if not (paginate and isinstance(data, dict) and isinstance(data.get("data"), list)):
            return data
        rows += data["data"]
        nxt = (data.get("paging") or {}).get("next")
        if not nxt:
            break
        url, query = _pin(nxt), {}
    else:
        print(f"warning: stopped after {MAX_PAGES} pages", file=sys.stderr)
    return {**first, "data": rows}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def fmt(name: str, value: Any) -> str:
    if value in (None, ""):
        return ""
    if RAW:
        return str(value)
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if name in MONEY:
        return f"{num:,.2f}"
    if name in RATES:
        return f"{num:.2f}%"
    if name in DEC:
        return f"{num:.2f}"
    return f"{int(num):,}" if num.is_integer() else f"{num:,.2f}"


def budget(row: dict) -> str:
    """Meta returns budgets in minor units (cents); render them as whole currency."""
    for key, label in (("daily_budget", "/day"), ("lifetime_budget", " total")):
        if row.get(key) and int(row[key]) > 0:
            return f"{int(row[key]) / 100:,.2f}{label}"
    return ""


def _emit(rows: list[list[str]], a) -> None:
    _tables.emit(rows, getattr(a, "csv", False))


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------


def date_params(a) -> dict:
    if getattr(a, "since", None) or getattr(a, "until", None):
        if not (a.since and a.until):
            raise RuntimeError("--since and --until must be given together")
        for value in (a.since, a.until):
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise RuntimeError(f"bad date {value!r} (use YYYY-MM-DD, or --preset last_30d)") from exc
        return {"time_range": f'{{"since":"{a.since}","until":"{a.until}"}}'}
    return {"date_preset": getattr(a, "preset", None) or "last_30d"}


def insight_params(a, level: str, fields: list[str]) -> dict:
    params: dict[str, Any] = {"level": level, "fields": ",".join(fields), "limit": 500}
    params.update(date_params(a))
    inc = getattr(a, "increment", None)
    if inc and inc != "all":
        params["time_increment"] = INCREMENTS[inc]
    if getattr(a, "breakdown", None):
        params["breakdowns"] = a.breakdown
    filters = []
    for spec in getattr(a, "filter", None) or []:
        field, _, value = spec.partition("==")
        if not value:
            raise RuntimeError(f"bad --filter {spec!r} (use field==value, e.g. campaign.id==123)")
        op = "IN" if "," in value else "EQUAL"
        val = ('["' + '","'.join(value.split(",")) + '"]') if op == "IN" else f'"{value}"'
        filters.append(f'{{"field":"{field.strip()}","operator":"{op}","value":{val}}}')
    if filters:
        params["filtering"] = "[" + ",".join(filters) + "]"
    return params


def actions_map(row: dict) -> dict[str, str]:
    return {a["action_type"]: a.get("value", "") for a in row.get("actions") or []}


def insight_rows(rows: list[dict], columns: list[str], all_actions: bool) -> list[list[str]]:
    """Flatten `actions` into action:<type> columns; keep only KEY_ACTIONS unless --all-actions."""
    seen: list[str] = []
    for row in rows:
        for key in actions_map(row):
            if key not in seen and (all_actions or key in KEY_ACTIONS):
                seen.append(key)
    seen.sort(key=lambda k: (KEY_ACTIONS.index(k) if k in KEY_ACTIONS else 99, k))
    header = [c.upper() for c in columns] + [f"action:{k}" for k in seen]
    out = [header]
    for row in rows:
        acts = actions_map(row)
        out.append([fmt(c, row.get(c, "")) for c in columns]
                   + [fmt(k, acts.get(k, "")) for k in seen])
    return out


def fetch_insights(a, level: str, fields: list[str]) -> dict:
    return get(f"/{a.account}/insights", insight_params(a, level, fields))


def print_insights(a, level: str, fields: list[str], columns: list[str]) -> None:
    data = fetch_insights(a, level, fields)
    if a.json:
        _tables.dump(data)
        return
    rows = data.get("data", [])
    if not rows:
        print("(no rows — check the period; Meta insights lag ~1 day and go back 37 months)")
        return
    cols = [c for c in columns if any(c in r for r in rows)]
    _emit(insight_rows(rows, cols, getattr(a, "all_actions", False)), a)


def cmd_overview(a):
    cols = ["date_start", "date_stop"] + METRICS
    if a.increment == "all":
        cols = METRICS
    print_insights(a, "account", INSIGHT_FIELDS, cols)


def cmd_insights(a):
    fields = ([f.strip() for f in a.fields.split(",") if f.strip()] if a.fields
              else INSIGHT_FIELDS + [f"{a.level}_name"])
    cols = ["date_start", "date_stop"] + ([a.breakdown] if a.breakdown else [])
    cols += [f for f in fields if f != "actions"]
    print_insights(a, a.level, fields, cols)


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def cmd_campaigns(a):
    params = {"fields": "id,name,status,effective_status,objective,start_time,stop_time,"
                        "daily_budget,lifetime_budget", "limit": 200}
    if a.status:
        params["effective_status"] = f'["{a.status.upper()}"]'
    data = get(f"/{a.account}/campaigns", params)
    if a.json:
        return _tables.dump(data)
    rows = [["ID", "NAME", "STATUS", "OBJECTIVE", "START", "STOP", "BUDGET"]]
    for c in data.get("data", []):
        rows.append([c["id"], c.get("name", ""), c.get("effective_status", c.get("status", "")),
                     c.get("objective", "").replace("OUTCOME_", ""),
                     c.get("start_time", "")[:10], c.get("stop_time", "")[:10], budget(c)])
    _emit(rows, a)


def cmd_adsets(a):
    params = {"fields": "id,name,status,effective_status,campaign{name},optimization_goal,"
                        "start_time,end_time,daily_budget,lifetime_budget", "limit": 200}
    path = f"/{a.campaign}/adsets" if a.campaign else f"/{a.account}/adsets"
    data = get(path, params)
    if a.json:
        return _tables.dump(data)
    rows = [["ID", "NAME", "STATUS", "CAMPAIGN", "GOAL", "START", "END", "BUDGET"]]
    for s in data.get("data", []):
        rows.append([s["id"], s.get("name", ""), s.get("effective_status", ""),
                     (s.get("campaign") or {}).get("name", ""), s.get("optimization_goal", ""),
                     s.get("start_time", "")[:10], s.get("end_time", "")[:10], budget(s)])
    _emit(rows, a)


def cmd_ads(a):
    params = {"fields": "id,name,status,effective_status,campaign{name},adset{name},"
                        "creative{name,object_story_spec}", "limit": 200}
    path = (f"/{a.adset}/ads" if a.adset else
            f"/{a.campaign}/ads" if a.campaign else f"/{a.account}/ads")
    data = get(path, params)
    if a.json:
        return _tables.dump(data)
    rows = [["ID", "NAME", "STATUS", "CAMPAIGN", "ADSET", "CREATIVE", "LINK"]]
    for ad in data.get("data", []):
        creative = ad.get("creative") or {}
        spec = (creative.get("object_story_spec") or {}).get("link_data") or {}
        rows.append([ad["id"], ad.get("name", ""), ad.get("effective_status", ""),
                     (ad.get("campaign") or {}).get("name", ""),
                     (ad.get("adset") or {}).get("name", ""),
                     _tables.trunc(creative.get("name", ""), 45),
                     _tables.trunc(spec.get("link", ""), 70)])
    _emit(rows, a)


def cmd_whoami(a):
    """Identity/scope/account discovery — type, app, scopes, accounts and pages. Never the token."""
    token = _fields.require(credentials(), "META_ACCESS_TOKEN", key=KEY)
    me = get("/me", {"fields": "id,name"}, paginate=False)
    debug = get("/debug_token", {"input_token": token}, paginate=False).get("data", {})
    perms = get("/me/permissions").get("data", [])
    accounts = get("/me/adaccounts",
                   {"fields": "id,name,currency,timezone_name,account_status"}).get("data", [])
    pages = get("/me/accounts", {"fields": "id,name"}).get("data", [])
    if a.json:
        return _tables.dump({"me": me, "token": {k: v for k, v in debug.items() if "token" not in k},
                             "permissions": perms, "adaccounts": accounts, "pages": pages})
    rows = [["FIELD", "VALUE"],
            ["system user", f"{me.get('name', '')} ({me.get('id', '')})"],
            ["app", f"{debug.get('application', '')} ({debug.get('app_id', '')})"],
            ["token type", debug.get("type", "")],
            ["valid", str(debug.get("is_valid", ""))],
            ["expires", "never" if debug.get("expires_at") == 0 else str(debug.get("expires_at"))],
            ["scopes", ", ".join(p["permission"] for p in perms if p["status"] == "granted")],
            ["configured ad account", a.account]]
    for acc in accounts:
        rows.append([f"ad account {acc['id']}",
                     f"{acc.get('name', '')} · {acc.get('currency', '')} · "
                     f"{acc.get('timezone_name', '')} · status {acc.get('account_status', '')}"])
    for page in pages:
        rows.append([f"page {page.get('id', '')}", page.get("name", "")])
    _emit(rows, a)


# ---------------------------------------------------------------------------
# Facebook page (organic)
# ---------------------------------------------------------------------------


def resolve_page(page_id: str | None = None) -> str:
    """Return the page id to report on: the flag, else the only page the token can see."""
    if page_id:
        return page_id
    pages = get("/me/accounts", {"fields": "id,name"}).get("data", [])
    if len(pages) == 1:
        return pages[0]["id"]
    if not pages:
        raise RuntimeError("no Facebook page visible to this token (needs pages_read_engagement) "
                           "— pass --page ID")
    names = ", ".join(f"{p.get('id')} {p.get('name', '')}".strip() for p in pages)
    raise RuntimeError(f"several pages visible ({names}) — pass --page ID")


def page_token(page_id: str) -> str | None:
    """Page insights need the PAGE access token, not the system-user token."""
    for page in get("/me/accounts", {"fields": "id,name,access_token"}).get("data", []):
        if page.get("id") == page_id:
            return page.get("access_token")
    return None


def cmd_page(a):
    page_id = resolve_page(getattr(a, "page", None))
    info = get(f"/{page_id}", {"fields": "id,name,link,followers_count,fan_count"}, paginate=False)
    metrics = ["page_post_engagements", "page_follows", "page_views_total", "page_total_actions"]
    params = {"metric": ",".join(metrics), "period": "day", **date_params(a)}
    if "time_range" in params:  # page insights want since/until, not time_range
        params.pop("time_range")
        params.update({"since": a.since, "until": a.until})
    pt = page_token(page_id)
    stats: Any = {"data": []}
    note = ""
    if pt:
        try:
            stats = get(f"/{page_id}/insights", params, token=pt, paginate=False)
        except RuntimeError as exc:
            note, stats = str(exc), {"data": []}
    else:
        note = "no page access token via /me/accounts (needs pages_read_engagement on this page)"
    if a.json:
        return _tables.dump({"page": info, "insights": stats, "note": note})
    rows = [["FIELD", "VALUE"],
            ["page", f"{info.get('name', '')} ({info.get('id', '')})"],
            ["followers", fmt("followers", info.get("followers_count"))],
            ["fans (likes)", fmt("fans", info.get("fan_count"))]]
    for metric in stats.get("data", []):
        total = sum(float(v.get("value") or 0) for v in metric.get("values", []))
        rows.append([metric.get("name", ""), fmt(metric.get("name", ""), total)])
    _emit(rows, a)
    if len(rows) == 4:
        print(f"\nno organic page insights for this period{': ' + note if note else ''} — "
              "most Meta page metrics were retired in v24", file=sys.stderr)


# ---------------------------------------------------------------------------
# Escape hatch
# ---------------------------------------------------------------------------


def cmd_api(a):
    if a.method.upper() != "GET":
        raise RuntimeError("this connector is read-only — GET only; a write goes through an action")
    url = a.url
    if url.startswith(("http://", "https://")):
        _pin(url)
    elif not url.startswith("/"):
        url = "/" + url
    params = {}
    for kv in a.param or []:
        key, _, value = kv.partition("=")
        params[key] = value
    _tables.dump(get(url, params, paginate=not a.no_paging))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    g = argparse.ArgumentParser(add_help=False)
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="raw Graph JSON")
    g.add_argument("--csv", action="store_true", default=argparse.SUPPRESS, help="tables as CSV")
    g.add_argument("--account", default=argparse.SUPPRESS,
                   help="ad account act_… (default: the connected account)")

    p = argparse.ArgumentParser(
        prog="python -m lib.connectors.meta_ads",
        description="Read-only Meta Ads connector (GET only, graph.facebook.com).", parents=[g])
    sub = p.add_subparsers(dest="cmd", required=True,
                           parser_class=lambda **kw: argparse.ArgumentParser(parents=[g], **kw))

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_, description=help_)
        s.set_defaults(fn=fn)
        return s

    def dates(s):
        s.add_argument("--since", metavar="YYYY-MM-DD")
        s.add_argument("--until", metavar="YYYY-MM-DD")
        s.add_argument("--preset", metavar="DATE_PRESET",
                       help="Graph date_preset: today, yesterday, last_7d, last_30d, last_90d, "
                            "this_month, last_month, this_year, maximum (default last_30d)")
        s.add_argument("--increment", choices=list(INCREMENTS), default="daily")
        s.add_argument("--all-actions", action="store_true",
                       help=f"all action:* columns (default only {', '.join(KEY_ACTIONS)})")
        return s

    add("whoami", cmd_whoami, "token type, app, scopes, visible ad accounts and pages")

    s = add("campaigns", cmd_campaigns, "campaigns with status, objective, dates, budget")
    s.add_argument("--status", help="ACTIVE, PAUSED, ARCHIVED, … (effective_status)")

    s = add("adsets", cmd_adsets, "ad sets")
    s.add_argument("--campaign", metavar="ID")

    s = add("ads", cmd_ads, "ads with creative name and destination link")
    s.add_argument("--campaign", metavar="ID")
    s.add_argument("--adset", metavar="ID")

    dates(add("overview", cmd_overview, "account-level spend/reach/clicks per period"))

    s = dates(add("insights", cmd_insights, "insights at campaign/adset/ad level, with breakdowns"))
    s.add_argument("--level", default="campaign", choices=["account", "campaign", "adset", "ad"])
    s.add_argument("--breakdown", choices=BREAKDOWNS)
    s.add_argument("--fields", help=f"comma list (default {','.join(INSIGHT_FIELDS)},<level>_name)")
    s.add_argument("--filter", action="append", metavar="FIELD==VALUE",
                   help="e.g. campaign.id==120249060606790215 (repeatable, AND-ed)")
    s.set_defaults(increment="all")

    s = dates(add("page", cmd_page, "Facebook page: followers + organic insights"))
    s.add_argument("--page", metavar="ID", help="page id (default: the only visible page)")

    s = add("api", cmd_api, "authenticated raw GET against graph.facebook.com")
    s.add_argument("method")
    s.add_argument("url")
    s.add_argument("--param", action="append", metavar="k=v")
    s.add_argument("--no-paging", action="store_true", help="do not follow paging.next")
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    global RAW
    a.json = getattr(a, "json", False)
    a.csv = getattr(a, "csv", False)
    RAW = a.csv
    try:
        account = getattr(a, "account", None) or default_account()
        a.account = account if account.startswith("act_") else f"act_{account}"
        a.fn(a)
    except (RuntimeError, api.MethodPolicyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:  # `| head` is a normal way to use this CLI
        import os

        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0
