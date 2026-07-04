"""Billit support connector for Belgian invoicing and Peppol reads.

Billit needs a PartyID header on account/order/file/Peppol reads. OAuth credentials stay host-side
behind the broker for the `billit` key; the private API-key fallback uses `billit_apikey`.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from lib import api

_BASE_URL = "https://api.billit.be"
_DEFAULT_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
_ORDER_PICK = (
    "Items.*.OrderID,"
    "Items.*.CompanyID,"
    "Items.*.OrderNumber,"
    "Items.*.OrderType,"
    "Items.*.OrderDirection,"
    "Items.*.OrderStatus,"
    "Items.*.OrderDate,"
    "Items.*.ExpiryDate,"
    "Items.*.LastModified,"
    "Items.*.TotalIncl,"
    "Items.*.ToPay,"
    "Items.*.Paid,"
    "Items.*.Overdue,"
    "Items.*.DaysOverdue,"
    "Items.*.CounterParty.Name,"
    "Items.*.CounterParty.VATNumber,"
    "Items.*.OrderPDF.FileID"
)
_ORDER_DETAIL_PICK = (
    "OrderID,CompanyID,OrderNumber,OrderType,OrderDirection,OrderStatus,OrderDate,ExpiryDate,"
    "LastModified,Created,TotalExcl,TotalIncl,ToPay,Paid,Overdue,DaysOverdue,"
    "Customer.Name,Customer.VATNumber,Supplier.Name,Supplier.VATNumber,"
    "OrderPDF.FileID,OrderPDF.FileName,Attachments.*.FileID,Attachments.*.FileName,"
    "OrderLines.*.Description,OrderLines.*.Quantity,OrderLines.*.UnitPriceExcl,"
    "OrderLines.*.VATPercentage,VatGroups"
)
_FILE_METADATA_PICK = "FileID,FileName,MimeType"
_INBOX_PICK = (
    "InboxItems.*.InboxItemID,"
    "InboxItems.*.SenderPeppolID,"
    "InboxItems.*.ReceiverPeppolID,"
    "InboxItems.*.ReceiverCompanyID,"
    "InboxItems.*.PeppolDocumentType,"
    "InboxItems.*.CreationDate,"
    "InboxItems.*.PeppolFileID"
)


api.register(
    api.Manifest(
        key="billit",
        base_url=_BASE_URL,
        auth=api.Auth(strategy="bearer"),
        pagination=api.Pagination(style="none"),
        default_headers=_DEFAULT_HEADERS,
    )
)
api.register(
    api.Manifest(
        key="billit_apikey",
        base_url=_BASE_URL,
        auth=api.Auth(strategy="api_key_header", name="ApiKey"),
        pagination=api.Pagination(style="none"),
        default_headers=_DEFAULT_HEADERS,
    )
)


def _client(connection: str) -> api.Client:
    key = connection.strip() or "billit"
    if key not in {"billit", "billit_apikey"}:
        raise RuntimeError("--connection must be billit or billit_apikey")
    return api.client(api.MANIFESTS[key], token_key=key)


def _headers(party_id: str, context_party_id: str = "") -> dict[str, str]:
    party_id = party_id.strip()
    if not party_id:
        raise RuntimeError("--party-id is required for Billit API reads")
    headers = {"PartyID": party_id}
    if context_party_id.strip():
        headers["ContextPartyID"] = context_party_id.strip()
    return headers


def _odata_filter(parts: list[str]) -> str:
    return " and ".join(part for part in parts if part)


def _orders_filter(args: argparse.Namespace) -> str:
    if args.filter:
        return args.filter
    parts = []
    if args.type:
        parts.append(f"OrderType eq '{args.type}'")
    if args.direction:
        parts.append(f"OrderDirection eq '{args.direction}'")
    if args.number:
        escaped = args.number.replace("'", "''")
        parts.append(f"OrderNumber eq '{escaped}'")
    if args.modified_since:
        parts.append(f"LastModified ge DateTime'{args.modified_since}'")
    return _odata_filter(parts)


def account(*, connection: str, party_id: str, context_party_id: str = "") -> dict[str, Any]:
    return _client(connection).get("/v1/account", headers=_headers(party_id, context_party_id))


def orders(
    *,
    connection: str,
    party_id: str,
    context_party_id: str = "",
    filter_expr: str = "",
    max_items: int = 25,
) -> dict[str, Any]:
    query = {}
    if filter_expr:
        query["$filter"] = filter_expr
    body = _client(connection).get("/v1/orders", query=query, headers=_headers(party_id, context_party_id))
    items = body.get("Items")
    if isinstance(items, list) and max_items > 0:
        body = dict(body)
        body["Items"] = items[:max_items]
        if len(items) > max_items:
            body["truncated"] = True
            body["shown"] = max_items
    return body


def order(*, connection: str, party_id: str, order_id: str, context_party_id: str = "") -> dict[str, Any]:
    return _client(connection).get(f"/v1/orders/{order_id}", headers=_headers(party_id, context_party_id))


def file(
    *,
    connection: str,
    party_id: str,
    file_id: str,
    context_party_id: str = "",
    metadata_only: bool = True,
) -> dict[str, Any]:
    body = _client(connection).get(f"/v1/files/{file_id}", headers=_headers(party_id, context_party_id))
    if metadata_only and isinstance(body, dict):
        body = dict(body)
        if "FileContent" in body:
            body["FileContent"] = "<base64 omitted; rerun with --include-content if needed>"
    return body


def peppol_inbox(*, connection: str, party_id: str, context_party_id: str = "") -> dict[str, Any]:
    return _client(connection).get("/v1/peppol/inbox", headers=_headers(party_id, context_party_id))


def raw_get(
    *,
    connection: str,
    party_id: str,
    path: str,
    query: dict[str, str],
    context_party_id: str = "",
) -> Any:
    return _client(connection).get(path, query=query, headers=_headers(party_id, context_party_id))


def _emit(obj: Any, pick: str = "") -> None:
    if pick:
        obj = api.pick(obj, pick)
    print(json.dumps(obj, indent=2, sort_keys=True))


def _query_pairs(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise RuntimeError(f"--query must be KEY=VALUE, got {raw!r}")
        key, val = raw.split("=", 1)
        out[key] = val
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m lib.connectors.billit")
    p.add_argument("--connection", default="billit", choices=["billit", "billit_apikey"])
    p.add_argument("--party-id", required=True, help="Billit company PartyID header")
    p.add_argument("--context-party-id", default="", help="Optional accountant ContextPartyID header")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("account")

    o = sub.add_parser("orders")
    o.add_argument("--direction", choices=["Income", "Cost"])
    o.add_argument("--type", choices=["Invoice", "CreditNote", "Offer", "Order"])
    o.add_argument("--number")
    o.add_argument("--modified-since", help="YYYY-MM-DD or Billit DateTime-compatible value")
    o.add_argument("--filter", help="Raw OData $filter expression")
    o.add_argument("--max-items", type=int, default=25)
    o.add_argument("--raw", action="store_true")

    one = sub.add_parser("order")
    one.add_argument("order_id")
    one.add_argument("--raw", action="store_true")

    f = sub.add_parser("file")
    f.add_argument("file_id")
    f.add_argument("--metadata-only", action="store_true", help="default; omit base64 FileContent")
    f.add_argument("--include-content", action="store_true")

    sub.add_parser("peppol-inbox")

    g = sub.add_parser("get")
    g.add_argument("path")
    g.add_argument("--query", action="append", default=[])
    g.add_argument("--pick", default="")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        common = {
            "connection": args.connection,
            "party_id": args.party_id,
            "context_party_id": args.context_party_id,
        }
        if args.cmd == "account":
            _emit(account(**common))
        elif args.cmd == "orders":
            body = orders(filter_expr=_orders_filter(args), max_items=args.max_items, **common)
            _emit(body, "" if args.raw else _ORDER_PICK)
        elif args.cmd == "order":
            body = order(order_id=args.order_id, **common)
            _emit(body, "" if args.raw else _ORDER_DETAIL_PICK)
        elif args.cmd == "file":
            body = file(file_id=args.file_id, metadata_only=not args.include_content, **common)
            _emit(body, "" if args.include_content else _FILE_METADATA_PICK)
        elif args.cmd == "peppol-inbox":
            _emit(peppol_inbox(**common), _INBOX_PICK)
        elif args.cmd == "get":
            _emit(raw_get(path=args.path, query=_query_pairs(args.query), **common), args.pick)
        else:
            raise RuntimeError(f"unknown command {args.cmd!r}")
    except (api.ApiError, api.MethodPolicyError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    return 0


__all__ = [
    "account",
    "orders",
    "order",
    "file",
    "peppol_inbox",
    "raw_get",
    "main",
]
