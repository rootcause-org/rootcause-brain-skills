"""New Relic NerdGraph support connector.

Force-code trigger (c): NerdGraph is a GraphQL API — every call is a POST with a JSON ``query``
body. ``lib.api`` is GET/REST only, so the generic client cannot drive it. This connector issues
the POST directly using the ``requests`` session managed by lib.api's Client (so auth injection,
retry, and backoff are still owned by lib.api — we never re-implement those).

All queries are read-only GraphQL QUERIES (never mutations). Credential is a New Relic User API
Key injected as ``RC_CONN_NEWRELIC`` and placed in the ``Api-Key`` header.

CLI:
    python -m lib.connectors.newrelic entities [--query FILTER] [--pick a,b] [--eu]
    python -m lib.connectors.newrelic nrql ACCOUNT_ID NRQL_QUERY [--pick a,b] [--eu]
    python -m lib.connectors.newrelic violations ACCOUNT_ID [--pick a,b] [--eu]
    python -m lib.connectors.newrelic incidents ACCOUNT_ID [--pick a,b] [--eu]
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from lib import _http_audit, api, oauth

# ---------------------------------------------------------------------------
# Manifest (registers the connector so `python -m lib.api get newrelic …` works for
# single-item introspection, and the manifest is available to the YAML loader).
# ---------------------------------------------------------------------------

MANIFEST = api.register(
    api.Manifest(
        key="newrelic",
        base_url="https://api.newrelic.com/graphql",
        auth=api.Auth(strategy="api_key_header", name="Api-Key"),
        pagination=api.Pagination(style="none"),
        rate_limit_remaining_header="",
        default_headers={"Content-Type": "application/json"},
    )
)

EU_BASE_URL = "https://api.eu.newrelic.com/graphql"


# ---------------------------------------------------------------------------
# Core GraphQL POST helper
# ---------------------------------------------------------------------------


def _gql(query: str, variables: dict | None = None, *, eu: bool = False) -> dict:
    """POST one GraphQL query to NerdGraph; return the parsed ``data`` dict.

    Auth comes from RC_CONN_NEWRELIC (lib.oauth.token raises loudly when absent). Retry/backoff
    for 429/5xx is handled by a minimal inline loop mirroring lib.api's strategy, since NerdGraph
    needs GraphQL-specific response handling around the POST.

    Raises ``api.ApiError`` on HTTP error or on a GraphQL ``errors`` payload (NerdGraph returns
    200 even on partial errors, so we surface the first error message clearly).
    """
    token = oauth.token("newrelic")
    url = EU_BASE_URL if eu else MANIFEST.base_url
    headers = {
        "Api-Key": token,
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {"query": query}
    if variables:
        body["variables"] = variables

    attempt = 0
    reason = "initial"
    rng = __import__("random").Random()
    while True:
        resp = _http_audit.request(
            "POST",
            url,
            json_body=body,
            headers=headers,
            timeout=(api.DEFAULT_CONNECT_TIMEOUT, api.DEFAULT_READ_TIMEOUT),
            attempt=attempt + 1,
            reason=reason,
            endpoint_template="/graphql",
            known_secrets=(token,),
        )
        if resp.status_code == 429 and attempt < api.DEFAULT_MAX_RETRIES:
            delay = api.parse_retry_after(resp.headers.get("Retry-After"))
            if delay is None:
                delay = api._full_jitter(attempt, api.DEFAULT_BACKOFF_BASE, api.DEFAULT_BACKOFF_CAP, rng)
            __import__("time").sleep(min(delay, api.MAX_RETRY_AFTER))
            reason = "retry_status_429"
            attempt += 1
            continue
        if resp.status_code in (500, 502, 503, 504) and attempt < api.DEFAULT_MAX_RETRIES:
            delay = api._full_jitter(attempt, api.DEFAULT_BACKOFF_BASE, api.DEFAULT_BACKOFF_CAP, rng)
            __import__("time").sleep(delay)
            reason = f"retry_status_{resp.status_code}"
            attempt += 1
            continue
        if not (200 <= resp.status_code < 300):
            raise api.ApiError(resp.status_code, resp.text, url=url)
        try:
            parsed = resp.json()
        except ValueError:
            raise api.ApiError(resp.status_code, f"non-JSON response: {resp.text[:200]}", url=url)
        # NerdGraph returns HTTP 200 even when the query has errors; surface them loudly.
        gql_errors = parsed.get("errors")
        if gql_errors:
            msg = gql_errors[0].get("message", str(gql_errors[0]))
            raise api.ApiError(200, f"NerdGraph error: {msg}", url=url)
        return parsed.get("data") or {}


# ---------------------------------------------------------------------------
# Entity search (with cursor-based pagination inside the GraphQL query)
# ---------------------------------------------------------------------------

_ENTITY_FIELDS = "name guid entityType alertSeverity reporting domain type"

_ENTITY_QUERY_TEMPLATE = """
{{
  actor {{
    entitySearch(query: {filter!r}) {{
      results(cursor: {cursor!r}) {{
        entities {{
          {fields}
        }}
        nextCursor
      }}
    }}
  }}
}}
"""

_ENTITY_QUERY_FIRST = """
{{
  actor {{
    entitySearch(query: {filter!r}) {{
      results {{
        entities {{
          {fields}
        }}
        nextCursor
      }}
    }}
  }}
}}
"""


def query_entities(
    entity_filter: str = "",
    *,
    eu: bool = False,
    max_pages: int = 100,
) -> dict:
    """Fetch all entities matching ``entity_filter`` (a NerdGraph entity search query string).

    NerdGraph entity search paginates with a ``nextCursor`` field INSIDE the GraphQL response
    body (not an HTTP header or query-string param). The cursor must be inlined into the next
    GraphQL query as the ``results(cursor: "…")`` argument — a non-standard shape that none of
    lib.api's built-in styles can express. This function owns the cursor loop.

    Returns ``{"items": [...], "incomplete": bool, "reason": str}``.
    """
    if not entity_filter:
        entity_filter = "reporting IS true"
    items: list[dict] = []
    incomplete = False
    reason = ""
    cursor: str | None = None
    pages = 0

    try:
        while pages < max_pages:
            if cursor is None:
                gql = _ENTITY_QUERY_FIRST.format(filter=entity_filter, fields=_ENTITY_FIELDS)
            else:
                gql = _ENTITY_QUERY_TEMPLATE.format(
                    filter=entity_filter, cursor=cursor, fields=_ENTITY_FIELDS
                )
            data = _gql(gql, eu=eu)
            results = (
                data.get("actor", {})
                .get("entitySearch", {})
                .get("results", {})
            )
            batch = results.get("entities") or []
            items.extend(batch)
            pages += 1
            cursor = results.get("nextCursor")
            if not cursor:
                break
        else:
            incomplete = True
            reason = f"reached max_pages={max_pages}"
    except api.ApiError as e:
        incomplete = True
        reason = f"page fetch failed after {len(items)} item(s): {e}"

    return {"items": items, "incomplete": incomplete, "reason": reason}


# ---------------------------------------------------------------------------
# NRQL query (single call — NRQL results are not paged by NerdGraph)
# ---------------------------------------------------------------------------

_NRQL_QUERY = """
{{
  actor {{
    account(id: {account_id:d}) {{
      nrql(query: {nrql!r}) {{
        results
      }}
    }}
  }}
}}
"""


def run_nrql(account_id: int, nrql: str, *, eu: bool = False) -> list:
    """Run a NRQL query and return the results list.

    NRQL queries return a flat list of result rows; no cursor-based paging. Common queries:
        SELECT count(*) FROM TransactionError SINCE 1 HOUR AGO FACET error.class LIMIT 20
        SELECT average(duration) FROM Transaction SINCE 30 MINUTES AGO TIMESERIES
    """
    data = _gql(_NRQL_QUERY.format(account_id=account_id, nrql=nrql), eu=eu)
    return (
        data.get("actor", {})
        .get("account", {})
        .get("nrql", {})
        .get("results", [])
    )


# ---------------------------------------------------------------------------
# Alert violations (open incidents on monitored entities)
# ---------------------------------------------------------------------------

_VIOLATIONS_QUERY = """
{{
  actor {{
    account(id: {account_id:d}) {{
      alerts {{
        nrqlConditionsPages(cursor: {cursor!r}) {{
          nrqlConditions {{
            id
            name
            enabled
            policyId
            violationTimeLimitSeconds
            signal {{
              aggregationWindow
              evaluationOffset
            }}
          }}
          nextCursor
        }}
      }}
    }}
  }}
}}
"""

# Open violations via the alerts domain on the account
_OPEN_VIOLATIONS_QUERY = """
{{
  actor {{
    account(id: {account_id:d}) {{
      alerts {{
        violations(isMuted: false) {{
          violations {{
            label
            duration
            severity
            status
            openedAt
            closedAt
            entity {{
              name
              type
            }}
            condition {{
              name
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


def query_violations(account_id: int, *, eu: bool = False) -> dict:
    """Fetch open alert violations for the account.

    Returns ``{"items": [...], "incomplete": bool, "reason": str}``.
    NerdGraph alert violations are not cursor-paged in the open violations query; the full list is
    returned in a single response (bounded by NerdGraph's own per-call limits ~200 items).
    """
    try:
        data = _gql(_OPEN_VIOLATIONS_QUERY.format(account_id=account_id), eu=eu)
    except api.ApiError as e:
        return {"items": [], "incomplete": True, "reason": str(e)}
    violations = (
        data.get("actor", {})
        .get("account", {})
        .get("alerts", {})
        .get("violations", {})
        .get("violations", [])
    ) or []
    return {"items": violations, "incomplete": False, "reason": ""}


# ---------------------------------------------------------------------------
# Alert incidents (NerdGraph alertsIncidents)
# ---------------------------------------------------------------------------

_INCIDENTS_QUERY = """
{{
  actor {{
    account(id: {account_id:d}) {{
      alerts {{
        incidents {{
          incidents {{
            incidentId
            title
            priority
            state
            createdAt
            closedAt
            sources {{
              policyId
              conditionName
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


def query_incidents(account_id: int, *, eu: bool = False) -> dict:
    """Fetch recent alert incidents (active + recently closed) for the account.

    Returns ``{"items": [...], "incomplete": bool, "reason": str}``.
    """
    try:
        data = _gql(_INCIDENTS_QUERY.format(account_id=account_id), eu=eu)
    except api.ApiError as e:
        return {"items": [], "incomplete": True, "reason": str(e)}
    incidents = (
        data.get("actor", {})
        .get("account", {})
        .get("alerts", {})
        .get("incidents", {})
        .get("incidents", [])
    ) or []
    return {"items": incidents, "incomplete": False, "reason": ""}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_result(result: dict, pick_paths: str) -> None:
    if pick_paths:
        result = dict(result)
        result["items"] = [api.pick(it, pick_paths) for it in result["items"]]
    print(json.dumps(result, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the New Relic NerdGraph connector."""
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.newrelic",
        description="Read New Relic observability data via NerdGraph (GraphQL). Read-only.",
    )
    parser.add_argument("--eu", action="store_true", help="use EU NerdGraph endpoint")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # entities
    ent = sub.add_parser("entities", help="search entities (APM apps, hosts, services, …)")
    ent.add_argument(
        "--query",
        default="reporting IS true",
        help="NerdGraph entity search filter string, e.g. \"name like 'my-app' and type = 'APPLICATION'\"",
    )
    ent.add_argument("--pick", default="", help="comma-separated dotted paths to select")

    # nrql
    nrql_p = sub.add_parser("nrql", help="run a NRQL analytics query")
    nrql_p.add_argument("account_id", type=int, help="New Relic account ID (numeric)")
    nrql_p.add_argument("nrql", help="NRQL query string, e.g. \"SELECT count(*) FROM Transaction SINCE 1 HOUR AGO\"")
    nrql_p.add_argument("--pick", default="", help="comma-separated dotted paths to select from each result row")

    # violations
    viol = sub.add_parser("violations", help="list open alert violations")
    viol.add_argument("account_id", type=int, help="New Relic account ID")
    viol.add_argument("--pick", default="", help="comma-separated dotted paths to select")

    # incidents
    inc = sub.add_parser("incidents", help="list recent alert incidents")
    inc.add_argument("account_id", type=int, help="New Relic account ID")
    inc.add_argument("--pick", default="", help="comma-separated dotted paths to select")

    args = parser.parse_args(argv)
    eu = args.eu

    if args.cmd == "entities":
        result = query_entities(args.query, eu=eu)
        _print_result(result, args.pick)

    elif args.cmd == "nrql":
        rows = run_nrql(args.account_id, args.nrql, eu=eu)
        if args.pick:
            rows = [api.pick(r, args.pick) for r in rows]
        print(json.dumps(rows, indent=2, default=str))

    elif args.cmd == "violations":
        result = query_violations(args.account_id, eu=eu)
        _print_result(result, args.pick)

    elif args.cmd == "incidents":
        result = query_incidents(args.account_id, eu=eu)
        _print_result(result, args.pick)

    else:
        parser.error(f"unknown command: {args.cmd!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
