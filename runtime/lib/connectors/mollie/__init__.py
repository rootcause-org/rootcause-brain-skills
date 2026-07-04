"""Mollie support connector: read payments/refunds and prepare refund evidence.

Force-code trigger: Mollie v2 paginates with HAL ``_links.next.href`` while list items sit under a
resource-specific ``_embedded.<resource>`` key. The connector extracts that variable envelope and keeps
the common support reads compact. It remains read-only; refund POSTs live under ``lib.action.mollie``.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lib import api

_MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.yaml"
_MANIFEST = api._parse_manifest_file(_MANIFEST_PATH)
api.register(_MANIFEST)
MANIFEST = _MANIFEST

_LIST_RESOURCES = {
    "payments": "/payments",
    "refunds": "/refunds",
    "balances": "/balances",
    "settlements": "/settlements",
    "customers": "/customers",
    "profiles": "/profiles",
    "methods": "/methods",
}

_GET_RESOURCES = {
    "payment": "/payments/{id}",
    "refund": "/payments/{payment_id}/refunds/{id}",
    "balance": "/balances/{id}",
    "settlement": "/settlements/{id}",
    "customer": "/customers/{id}",
    "profile": "/profiles/{id}",
}

_PICK_FIELDS = {
    "payments": (
        "id,mode,createdAt,paidAt,expiresAt,status,isCancelable,amount.value,amount.currency,"
        "amountRefunded.value,amountRefunded.currency,amountRemaining.value,amountRemaining.currency,"
        "description,method,profileId,customerId,mandateId,subscriptionId,metadata"
    ),
    "refunds": (
        "id,paymentId,mode,createdAt,status,amount.value,amount.currency,description,metadata,"
        "_links.payment.href"
    ),
    "balances": "id,mode,createdAt,status,transferFrequency,transferThreshold,value.value,value.currency",
    "settlements": "id,reference,createdAt,settledAt,status,amount.value,amount.currency,periods",
    "customers": "id,mode,createdAt,name,email,locale,metadata",
    "profiles": "id,mode,createdAt,name,website,status,review.status,categoryCode",
    "methods": "id,description,status,minimumAmount,maximumAmount",
}


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="mollie")


def _parse_query(items: list[str]) -> dict[str, str]:
    query: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--query must be K=V, got {item!r}")
        key, value = item.split("=", 1)
        query[key] = value
    return query


def _items_from_body(body: Any, resource: str) -> list:
    if not isinstance(body, dict):
        return list(body) if isinstance(body, list) else []
    embedded = body.get("_embedded")
    if isinstance(embedded, dict):
        candidate = embedded.get(resource)
        if isinstance(candidate, list):
            return list(candidate)
        for value in embedded.values():
            if isinstance(value, list):
                return list(value)
    return []


def _next_url(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    links = body.get("_links")
    if not isinstance(links, dict):
        return None
    nxt = links.get("next")
    if isinstance(nxt, dict) and nxt.get("href"):
        return str(nxt["href"])
    return None


def _guard_next_url(c: api.Client, next_url: str | None) -> str | None:
    if not next_url:
        return None
    nxt = urlsplit(next_url)
    if not nxt.scheme and not nxt.netloc:
        return next_url
    origin = urlsplit(c.manifest.base_url)
    if nxt.scheme == origin.scheme and nxt.netloc == origin.netloc:
        return next_url
    raise api.ApiError(0, "pagination next URL escaped Mollie API origin", url=next_url)


def list_resource(
    resource: str,
    *,
    query: dict[str, Any] | None = None,
    max_pages: int = 10,
) -> dict[str, Any]:
    path = _LIST_RESOURCES[resource]
    c = _client()
    current: str | None = path
    current_query = dict(query or {})
    items: list[Any] = []
    pages = 0
    incomplete = False
    reason = ""
    try:
        while current and pages < max_pages:
            body = c.get(current, query=current_query if pages == 0 else None)
            items.extend(_items_from_body(body, resource))
            current = _guard_next_url(c, _next_url(body))
            current_query = {}
            pages += 1
        if current:
            incomplete = True
            reason = f"reached max_pages={max_pages}"
    except api.ApiError as e:
        incomplete = True
        reason = f"page fetch failed after {len(items)} item(s): {e}"
    return {"items": items, "incomplete": incomplete, "reason": reason}


def get_resource(resource: str, **ids: str) -> Any:
    return _client().get(_GET_RESOURCES[resource].format(**ids))


def payment(payment_id: str, *, embed: str = "", testmode: str = "") -> dict[str, Any]:
    query = {}
    if embed:
        query["embed"] = embed
    if testmode:
        query["testmode"] = testmode
    return _client().get(f"/payments/{payment_id}", query=query)


def payment_refunds(payment_id: str, *, query: dict[str, Any] | None = None, max_pages: int = 10) -> dict[str, Any]:
    c = _client()
    current: str | None = f"/payments/{payment_id}/refunds"
    current_query = dict(query or {})
    items: list[Any] = []
    pages = 0
    incomplete = False
    reason = ""
    try:
        while current and pages < max_pages:
            body = c.get(current, query=current_query if pages == 0 else None)
            items.extend(_items_from_body(body, "refunds"))
            current = _guard_next_url(c, _next_url(body))
            current_query = {}
            pages += 1
        if current:
            incomplete = True
            reason = f"reached max_pages={max_pages}"
    except api.ApiError as e:
        incomplete = True
        reason = f"refund page fetch failed after {len(items)} item(s): {e}"
    return {"items": items, "incomplete": incomplete, "reason": reason}


def refund_plan(
    payment_id: str,
    *,
    amount: str = "",
    currency: str = "",
    description: str = "",
    testmode: str = "",
) -> dict[str, Any]:
    query = {"embed": "refunds,chargebacks"}
    if testmode:
        query["testmode"] = testmode
    raw_payment = _client().get(f"/payments/{payment_id}", query=query)
    refund_result = payment_refunds(payment_id, query={"testmode": testmode} if testmode else {})
    refunds = refund_result["items"]
    picked_payment = api.pick(raw_payment, _PICK_FIELDS["payments"])
    requested = {"amount": amount, "currency": currency, "description": description}
    available = _available_amount(raw_payment)
    checks = _refund_checks(
        raw_payment,
        amount=amount,
        currency=currency,
        description=description,
        available=available,
        refund_result=refund_result,
    )
    return {
        "payment": picked_payment,
        "existing_refunds": [api.pick(r, _PICK_FIELDS["refunds"]) for r in refunds],
        "requested_refund": requested,
        "available_amount": available,
        "checks": checks,
        "action_capability": "mollie.write",
    }


def _available_amount(payment_obj: dict[str, Any]) -> dict[str, str]:
    remaining = payment_obj.get("amountRemaining")
    if isinstance(remaining, dict) and remaining.get("value") and remaining.get("currency"):
        return {"value": str(remaining["value"]), "currency": str(remaining["currency"])}
    amount = _amount_decimal(payment_obj.get("amount"))
    refunded = _amount_decimal(payment_obj.get("amountRefunded"))
    currency = _amount_currency(payment_obj.get("amount")) or _amount_currency(payment_obj.get("amountRefunded"))
    if amount is None:
        return {"value": "", "currency": currency}
    refunded = refunded or Decimal("0")
    return {"value": str(max(amount - refunded, Decimal("0"))), "currency": currency}


def _refund_checks(
    payment_obj: dict[str, Any],
    *,
    amount: str,
    currency: str,
    description: str,
    available: dict[str, str],
    refund_result: dict[str, Any],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    status = str(payment_obj.get("status", ""))
    checks.append({"name": "payment_status", "ok": status == "paid", "observed": status})
    checks.append({
        "name": "refund_history_complete",
        "ok": not bool(refund_result.get("incomplete")),
        "observed": refund_result.get("reason", ""),
    })
    payment_currency = _amount_currency(payment_obj.get("amount"))
    if currency:
        checks.append({"name": "currency_matches", "ok": currency == payment_currency, "observed": payment_currency})
    if amount:
        requested = _decimal(amount)
        remaining = _decimal(available.get("value", ""))
        checks.append({
            "name": "amount_positive",
            "ok": requested is not None and requested > 0,
            "observed": amount,
        })
        checks.append({
            "name": "amount_within_remaining",
            "ok": requested is not None and remaining is not None and requested <= remaining,
            "observed": available,
        })
    if description:
        checks.append({"name": "description_length", "ok": len(description) <= 255, "observed": len(description)})
    return checks


def _amount_decimal(value: Any) -> Decimal | None:
    if isinstance(value, dict):
        return _decimal(str(value.get("value", "")))
    return None


def _amount_currency(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("currency", ""))
    return ""


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _compact(resource: str, obj: Any) -> Any:
    fields = _PICK_FIELDS.get(resource)
    if fields and isinstance(obj, dict):
        return api.pick(obj, fields)
    return obj


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.mollie",
        description="Read-only Mollie grounding for payments, refunds, balances, and settlements.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", help="list a resource with HAL pagination")
    ls.add_argument("resource", choices=sorted(_LIST_RESOURCES))
    ls.add_argument("--query", action="append", default=[], metavar="K=V")
    ls.add_argument("--max-pages", type=int, default=10)
    ls.add_argument("--no-pick", action="store_true")

    gt = sub.add_parser("get", help="fetch one resource")
    gt.add_argument("resource", choices=sorted(_GET_RESOURCES))
    gt.add_argument("id")
    gt.add_argument("--payment-id", default="", help="parent payment id for refund get")
    gt.add_argument("--no-pick", action="store_true")

    pay = sub.add_parser("payment", help="fetch one payment, optionally embedding refunds/chargebacks")
    pay.add_argument("payment_id")
    pay.add_argument("--embed", default="")
    pay.add_argument("--testmode", default="")
    pay.add_argument("--no-pick", action="store_true")

    refs = sub.add_parser("payment-refunds", help="list refunds for one payment")
    refs.add_argument("payment_id")
    refs.add_argument("--query", action="append", default=[], metavar="K=V")
    refs.add_argument("--max-pages", type=int, default=10)
    refs.add_argument("--no-pick", action="store_true")

    plan = sub.add_parser("refund-plan", help="read-only evidence for a proposed payment refund")
    plan.add_argument("payment_id")
    plan.add_argument("--amount", default="")
    plan.add_argument("--currency", default="")
    plan.add_argument("--description", default="")
    plan.add_argument("--testmode", default="")

    args = parser.parse_args(argv)

    if args.cmd == "list":
        result = list_resource(args.resource, query=_parse_query(args.query), max_pages=args.max_pages)
        if not args.no_pick:
            result["items"] = [_compact(args.resource, item) for item in result["items"]]
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.cmd == "get":
        if args.resource == "refund":
            if not args.payment_id:
                raise SystemExit("get refund requires --payment-id")
            body = get_resource("refund", payment_id=args.payment_id, id=args.id)
            pick_resource = "refunds"
        else:
            body = get_resource(args.resource, id=args.id)
            pick_resource = args.resource + "s"
        print(json.dumps(body if args.no_pick else _compact(pick_resource, body), indent=2, default=str))
        return 0

    if args.cmd == "payment":
        body = payment(args.payment_id, embed=args.embed, testmode=args.testmode)
        print(json.dumps(body if args.no_pick else _compact("payments", body), indent=2, default=str))
        return 0

    if args.cmd == "payment-refunds":
        result = payment_refunds(args.payment_id, query=_parse_query(args.query), max_pages=args.max_pages)
        if not args.no_pick:
            result["items"] = [_compact("refunds", item) for item in result["items"]]
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.cmd == "refund-plan":
        print(json.dumps(refund_plan(
            args.payment_id,
            amount=args.amount,
            currency=args.currency,
            description=args.description,
            testmode=args.testmode,
        ), indent=2, default=str))
        return 0

    parser.error("unknown command")
    return 2
