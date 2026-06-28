"""GoCardless support connector — grounding reads for payments, mandates, customers, subscriptions.

Force-code trigger (d): GoCardless list responses embed items under a resource-type key that varies
per endpoint ("payments", "mandates", "customers", "subscriptions", "refunds", "events", …) while
the pagination envelope is always at ``meta.cursors.after``. The generic ``items_field`` in the
manifest cannot be fixed to a single value across all endpoints. This script extracts items
dynamically by deriving the envelope key from the endpoint path.

Auth: bearer (access token or OAuth token injected as ``RC_CONN_GOCARDLESS``).
Pagination: cursor style — server returns ``meta.cursors.after``; absent means last page.
Required header: ``GoCardless-Version: 2015-07-06`` (declared in manifest default_headers).

Read-only: only GETs are issued. We never write to customer GoCardless accounts.

CLI:
    python -m lib.connectors.gocardless list payments [--query k=v] [--max-pages N]
    python -m lib.connectors.gocardless list mandates [--query k=v] [--max-pages N]
    python -m lib.connectors.gocardless list customers [--query k=v] [--max-pages N]
    python -m lib.connectors.gocardless list subscriptions [--query k=v] [--max-pages N]
    python -m lib.connectors.gocardless list refunds [--query k=v] [--max-pages N]
    python -m lib.connectors.gocardless list events [--query k=v] [--max-pages N]
    python -m lib.connectors.gocardless get payment <id>
    python -m lib.connectors.gocardless get mandate <id>
    python -m lib.connectors.gocardless get customer <id>
    python -m lib.connectors.gocardless get subscription <id>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from lib import api

# ---------------------------------------------------------------------------
# Manifest registration — authoritative row for both lib.api CLI and this script.
# ---------------------------------------------------------------------------

# Load from the co-located manifest.yaml so the canonical source is the YAML, not duplicated here.
_MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.yaml"
_MANIFEST = api._parse_manifest_file(_MANIFEST_PATH)
api.register(_MANIFEST)
MANIFEST = _MANIFEST

# Support-relevant fields per resource type. Dotted paths fed to api.pick().
_PICK_FIELDS: dict[str, str] = {
    "payments": (
        "id,status,amount,currency,description,created_at,charge_date,"
        "failure_reason,failure_reason_description,can_retry,"
        "links.mandate,links.subscription,links.customer"
    ),
    "mandates": (
        "id,status,scheme,created_at,next_possible_charge_date,"
        "reference,payments_require_approval,"
        "links.customer,links.customer_bank_account"
    ),
    "customers": (
        "id,email,given_name,family_name,company_name,phone_number,"
        "created_at,language,metadata"
    ),
    "subscriptions": (
        "id,status,amount,currency,name,interval,interval_unit,"
        "start_date,end_date,payment_reference,count,created_at,"
        "links.mandate,links.customer"
    ),
    "refunds": (
        "id,amount,currency,created_at,reference,status,"
        "links.payment,links.mandate"
    ),
    "events": (
        "id,created_at,action,resource_type,"
        "links.payment,links.mandate,links.subscription,links.customer,"
        "details.cause,details.description,details.scheme,details.reason_code"
    ),
}

# Resources that go directly under the path (plural → resource envelope key mapping).
# GoCardless wraps responses as {"<resource_key>": [...], "meta": {...}} — the key is the
# plural resource name used as both the URL segment and the envelope key.
_KNOWN_RESOURCES = frozenset({
    "payments", "mandates", "customers", "subscriptions",
    "refunds", "events", "customer_bank_accounts", "payment_requests",
    "creditors", "payout_items", "payouts", "redirect_flows",
    "customer_notifications",
})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="gocardless")


def _resource_key(path: str) -> str:
    """Derive the GoCardless envelope items key from the endpoint path.

    GoCardless embeds list items under the plural resource name, e.g.
    ``/payments`` → ``"payments"``, ``/mandates/MD123`` → ``"mandates"`` (single resource).
    Takes the first path segment (strips leading slash) as the canonical key.
    """
    segment = path.lstrip("/").split("/")[0]
    return segment


def _items_from_body(body: Any, resource_key: str) -> list:
    """Extract the items list from a GoCardless list response envelope.

    GoCardless wraps items as ``{"<resource_key>": [...], "meta": {"cursors": {…}}}``.
    Tries the declared resource_key first; if absent or not a list, falls back to the first
    list-valued key that is not the pagination meta envelope.
    """
    if isinstance(body, dict):
        candidate = body.get(resource_key)
        if isinstance(candidate, list):
            return list(candidate)
        # Fallback: first list-valued key that is not the pagination wrapper.
        _META_KEYS = frozenset({"meta"})
        for k, v in body.items():
            if k not in _META_KEYS and isinstance(v, list):
                return list(v)
    if isinstance(body, list):
        return list(body)
    return []


def _next_cursor(body: Any) -> str | None:
    """Extract the next-page cursor from ``meta.cursors.after``; None when last page.

    GoCardless sets ``meta.cursors.after`` to an opaque string when more pages follow,
    and to ``null`` / omits it when exhausted. No separate ``has_more`` boolean.
    """
    if not isinstance(body, dict):
        return None
    meta = body.get("meta")
    if not isinstance(meta, dict):
        return None
    cursors = meta.get("cursors")
    if not isinstance(cursors, dict):
        return None
    after = cursors.get("after")
    return str(after) if after else None


def list_resource(
    path: str,
    *,
    query: dict[str, Any] | None = None,
    max_pages: int = 20,
    page_size: int = 200,
) -> dict:
    """Auto-page a GoCardless list endpoint, extracting items from the variable envelope key.

    Returns ``{"items": [...], "incomplete": bool, "reason": str}`` — the same shape as
    ``api.Client.collect`` so callers are interchangeable.

    GoCardless uses ``after`` as the cursor query param (sent on subsequent pages), derived from
    ``meta.cursors.after`` in each response. No ``has_more``; cursor absence stops the loop.
    """
    c = _client()
    resource_key = _resource_key(path)
    q = dict(query or {}, limit=page_size)
    items: list = []
    pages_fetched = 0
    incomplete = False
    reason = ""

    try:
        while pages_fetched < max_pages:
            page = c.fetch_page(path, query=q)
            page_items = _items_from_body(page.body, resource_key)
            items.extend(page_items)
            pages_fetched += 1
            cursor = _next_cursor(page.body)
            if cursor is None:
                break
            q = dict(q, after=cursor)
        else:
            incomplete = True
            reason = f"reached max_pages={max_pages}"
    except api.ApiError as e:
        incomplete = True
        reason = f"page fetch failed after {len(items)} item(s): {e}"

    return {"items": items, "incomplete": incomplete, "reason": reason}


def get_resource(path: str) -> Any:
    """GET a single resource by path (e.g. ``/payments/PM123``). Raises ApiError on error."""
    return _client().get(path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_LIST_RESOURCES = ["payments", "mandates", "customers", "subscriptions", "refunds", "events",
                   "customer_bank_accounts", "payouts", "payment_requests"]

_GET_RESOURCES = ["payment", "mandate", "customer", "subscription", "refund", "event"]

# Singular → plural mapping for GET paths.
_SINGULAR_TO_PLURAL: dict[str, str] = {
    "payment": "payments",
    "mandate": "mandates",
    "customer": "customers",
    "subscription": "subscriptions",
    "refund": "refunds",
    "event": "events",
    "customer_bank_account": "customer_bank_accounts",
    "payout": "payouts",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.gocardless",
        description="Read-only GoCardless grounding: payments, mandates, customers, subscriptions.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # -- list subcommand --
    ls = sub.add_parser("list", help="list a resource (auto-paginated, pre-selected fields)")
    ls.add_argument(
        "resource",
        choices=_LIST_RESOURCES,
        help="resource type to list",
    )
    ls.add_argument("--query", action="append", default=[], metavar="K=V",
                    help="query param (repeatable, e.g. --query customer_id=CU123)")
    ls.add_argument("--max-pages", type=int, default=10,
                    help="hard page cap (default 10)")
    ls.add_argument("--page-size", type=int, default=200,
                    help="items per page (default 200, GoCardless max)")
    ls.add_argument("--no-pick", action="store_true",
                    help="return full objects instead of pre-selected support fields")

    # -- get subcommand --
    gt = sub.add_parser("get", help="fetch one resource by id")
    gt.add_argument("resource",
                    choices=_GET_RESOURCES,
                    help="resource type")
    gt.add_argument("id", help="resource id (e.g. PM123, MD456, CU789)")
    gt.add_argument("--no-pick", action="store_true",
                    help="return full object instead of pre-selected support fields")

    args = parser.parse_args(argv)

    if args.cmd == "list":
        path = f"/{args.resource}"
        query = dict(kv.split("=", 1) for kv in args.query if "=" in kv)
        result = list_resource(
            path, query=query, max_pages=args.max_pages, page_size=args.page_size,
        )
        if not args.no_pick and args.resource in _PICK_FIELDS:
            result["items"] = [api.pick(it, _PICK_FIELDS[args.resource]) for it in result["items"]]
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.cmd == "get":
        plural = _SINGULAR_TO_PLURAL.get(args.resource, args.resource + "s")
        path = f"/{plural}/{args.id}"
        body = get_resource(path)
        pick_key = plural  # match _PICK_FIELDS keys
        if not args.no_pick and pick_key in _PICK_FIELDS:
            # Single-resource GET: GoCardless wraps as {"payments": {...}} (singular object, not list).
            envelope = body.get(pick_key) if isinstance(body, dict) else None
            target = envelope if isinstance(envelope, dict) else body
            body = api.pick(target, _PICK_FIELDS[pick_key])
        print(json.dumps(body, indent=2, default=str))
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
