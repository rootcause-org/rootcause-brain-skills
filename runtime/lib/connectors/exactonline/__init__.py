"""Exact Online read-only accounting connector for Dutch-hosted support workflows.

Exact exposes OData v2 REST endpoints. ``lib.api`` owns OAuth bearer placement, retries, error
normalization, and ``d.__next`` pagination; this module adds safe OData filters, current-division
discovery, compact field selection, and the account -> invoices/receivables support join.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from lib import api

_MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")
MANIFEST = api.register(api._parse_manifest_file(_MANIFEST_PATH))

_IDENTITY_SELECT = "CurrentDivision,FullName,UserID"
_DIVISION_SELECT = "Code,Description,Current"
_ACCOUNT_SELECT = "ID,Code,Name,Email,Phone,VATNumber,Status,IsSales,IsSupplier,Blocked"
_INVOICE_SELECT = (
    "InvoiceID,InvoiceNumber,InvoiceDate,DueDate,Status,StatusDescription,InvoiceTo,"
    "InvoiceToName,AmountFC,Currency,YourRef"
)
_RECEIVABLE_SELECT = (
    "ID,Account,AccountName,InvoiceNumber,InvoiceDate,DueDate,AmountFC,"
    "Currency,Status,IsFullyPaid,YourRef"
)


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="exactonline")


def odata_literal(value: str) -> str:
    """Quote an OData string literal; doubled apostrophes cannot escape into the expression."""
    return "'" + str(value).replace("'", "''") + "'"


def _odata_guid(value: str) -> str:
    try:
        normalized = str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError) as exc:
        raise RuntimeError(f"invalid Exact Online UUID: {value!r}") from exc
    return f"guid'{normalized}'"


def _division_code(value: int | str) -> int:
    try:
        division = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid Exact Online division: {value!r}") from exc
    if division <= 0:
        raise RuntimeError("Exact Online division must be a positive integer")
    return division


def _odata_results(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    d = body.get("d")
    if not isinstance(d, dict):
        return []
    rows = d.get("results")
    return rows if isinstance(rows, list) else []


def current_identity() -> dict[str, Any]:
    """Return the signed-in user and their current administration."""
    body = _client().get("current/Me", query={"$select": _IDENTITY_SELECT})
    rows = _odata_results(body)
    if not rows:
        raise RuntimeError("Exact Online current/Me returned no identity")
    return api.pick(rows[0], _IDENTITY_SELECT)


def current_division(division: int | str | None = None) -> int:
    """Validate an explicit division or discover the signed-in user's current one."""
    if division not in (None, ""):
        return _division_code(division)
    value = current_identity().get("CurrentDivision")
    if value in (None, ""):
        raise RuntimeError("Exact Online identity has no CurrentDivision")
    return _division_code(value)


def _collection(
    path: str,
    *,
    query: dict[str, Any],
    select: str,
    max_items: int,
) -> dict[str, Any]:
    result = _client().collect(path, query=query, max_items=max_items)
    return {
        "items": [api.pick(item, select) for item in result["items"]],
        "incomplete": result["incomplete"],
        "reason": result["reason"],
    }


def divisions(*, division: int | str | None = None, max_items: int = 100) -> dict[str, Any]:
    """List administrations accessible from the current administration context."""
    div = current_division(division)
    return _collection(
        f"{div}/system/Divisions",
        query={"$select": _DIVISION_SELECT, "$orderby": "Description asc"},
        select=_DIVISION_SELECT,
        max_items=max_items,
    )


def _account_filter(term: str, field: str) -> str:
    normalized = term.strip()
    literal = odata_literal(normalized)
    if field == "id":
        return f"ID eq {_odata_guid(term)}"
    if field == "email":
        return f"Email eq {literal}"
    if field == "code":
        if not normalized.isdigit() or len(normalized) > 18:
            raise RuntimeError("Exact Online account code must be a numeric string of at most 18 digits")
        # Exact stores Account.Code as a fixed-width 18-character string padded on the left.
        return f"Code eq {odata_literal(normalized.rjust(18))}"
    if field == "name":
        return f"substringof({literal},Name)"
    if field == "any":
        code_literal = (
            odata_literal(normalized.rjust(18))
            if normalized.isdigit() and len(normalized) <= 18
            else literal
        )
        return (
            f"(Email eq {literal} or Code eq {code_literal} or Name eq {literal} "
            f"or substringof({literal},Name))"
        )
    raise RuntimeError(f"unsupported Exact Online account search field: {field!r}")


def accounts(
    term: str,
    *,
    field: str = "any",
    division: int | str | None = None,
    max_items: int = 20,
) -> dict[str, Any]:
    """Search customer/supplier accounts with an OData-safe literal."""
    term = term.strip()
    if not term:
        raise RuntimeError("account search term is required")
    div = current_division(division)
    return _collection(
        f"{div}/crm/Accounts",
        query={
            "$filter": _account_filter(term, field),
            "$select": _ACCOUNT_SELECT,
            "$orderby": "Name asc",
        },
        select=_ACCOUNT_SELECT,
        max_items=max_items,
    )


def sales_invoices(
    *,
    division: int | str | None = None,
    account_id: str = "",
    invoice_number: int | str | None = None,
    max_items: int = 50,
) -> dict[str, Any]:
    """List newest sales invoices, optionally restricted to an account or invoice number."""
    div = current_division(division)
    filters = []
    if account_id:
        filters.append(f"InvoiceTo eq {_odata_guid(account_id)}")
    if invoice_number not in (None, ""):
        try:
            number = int(invoice_number)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid invoice number: {invoice_number!r}") from exc
        filters.append(f"InvoiceNumber eq {number}")
    query = {"$select": _INVOICE_SELECT, "$orderby": "InvoiceDate desc"}
    if filters:
        query["$filter"] = " and ".join(filters)
    return _collection(
        f"{div}/salesinvoice/SalesInvoices",
        query=query,
        select=_INVOICE_SELECT,
        max_items=max_items,
    )


def receivables(
    *,
    division: int | str | None = None,
    account_id: str = "",
    open_only: bool = True,
    max_items: int = 50,
) -> dict[str, Any]:
    """List receivables; open items (Exact status 20) are the default support view."""
    div = current_division(division)
    filters = []
    if account_id:
        filters.append(f"Account eq {_odata_guid(account_id)}")
    if open_only:
        filters.append("Status eq 20")
    query = {"$select": _RECEIVABLE_SELECT, "$orderby": "DueDate asc"}
    if filters:
        query["$filter"] = " and ".join(filters)
    return _collection(
        f"{div}/cashflow/Receivables",
        query=query,
        select=_RECEIVABLE_SELECT,
        max_items=max_items,
    )


def _best_account_match(term: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    needle = term.strip().casefold()
    exact = [
        row
        for row in rows
        if any(str(row.get(key, "")).strip().casefold() == needle for key in ("ID", "Code", "Name", "Email"))
    ]
    if len(exact) == 1:
        return exact[0]
    return rows[0] if len(rows) == 1 else None


def account_summary(
    term: str,
    *,
    division: int | str | None = None,
    max_invoices: int = 20,
    max_receivables: int = 20,
) -> dict[str, Any]:
    """Join one account with its newest sales invoices and open receivables."""
    div = current_division(division)
    try:
        uuid.UUID(term.strip())
        field = "id"
    except ValueError:
        field = "email" if "@" in term else "any"
    matches = accounts(term, field=field, division=div, max_items=10)
    account = _best_account_match(term, matches["items"])
    if account is None:
        return {
            "found": False,
            "ambiguous": bool(matches["items"]),
            "matches": matches["items"],
            "incomplete": matches["incomplete"],
            "reason": matches["reason"],
        }

    account_id = str(account["ID"])
    invoices = sales_invoices(division=div, account_id=account_id, max_items=max_invoices)
    outstanding = receivables(division=div, account_id=account_id, max_items=max_receivables)
    incomplete = any((matches["incomplete"], invoices["incomplete"], outstanding["incomplete"]))
    reasons = [r for r in (matches["reason"], invoices["reason"], outstanding["reason"]) if r]
    return {
        "found": True,
        "division": div,
        "account": account,
        "sales_invoices": invoices["items"],
        "open_receivables": outstanding["items"],
        "incomplete": incomplete,
        "reason": "; ".join(reasons),
    }


def _emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _add_division(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--division", help="numeric administration code; defaults to current/Me")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.exactonline")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("me", help="show current identity and division")

    division_parser = sub.add_parser("divisions", help="list accessible administrations")
    _add_division(division_parser)
    division_parser.add_argument("--max-items", type=int, default=100)

    account_parser = sub.add_parser("accounts", help="search accounts by email, code, name, or UUID")
    account_parser.add_argument("term")
    account_parser.add_argument("--field", choices=["any", "email", "code", "name", "id"], default="any")
    account_parser.add_argument("--max-items", type=int, default=20)
    _add_division(account_parser)

    invoice_parser = sub.add_parser("sales-invoices", help="list newest sales invoices")
    invoice_parser.add_argument("--account-id", default="")
    invoice_parser.add_argument("--invoice-number")
    invoice_parser.add_argument("--max-items", type=int, default=50)
    _add_division(invoice_parser)

    receivable_parser = sub.add_parser("receivables", help="list open receivables")
    receivable_parser.add_argument("--account-id", default="")
    receivable_parser.add_argument("--all", action="store_true", help="include non-open receivables")
    receivable_parser.add_argument("--max-items", type=int, default=50)
    _add_division(receivable_parser)

    summary_parser = sub.add_parser("account-summary", help="join an account to invoices and open receivables")
    summary_parser.add_argument("term", help="account UUID, email, code, or exact/partial name")
    summary_parser.add_argument("--max-invoices", type=int, default=20)
    summary_parser.add_argument("--max-receivables", type=int, default=20)
    _add_division(summary_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "me":
            value = current_identity()
        elif args.command == "divisions":
            value = divisions(division=args.division, max_items=args.max_items)
        elif args.command == "accounts":
            value = accounts(args.term, field=args.field, division=args.division, max_items=args.max_items)
        elif args.command == "sales-invoices":
            value = sales_invoices(
                division=args.division,
                account_id=args.account_id,
                invoice_number=args.invoice_number,
                max_items=args.max_items,
            )
        elif args.command == "receivables":
            value = receivables(
                division=args.division,
                account_id=args.account_id,
                open_only=not args.all,
                max_items=args.max_items,
            )
        elif args.command == "account-summary":
            value = account_summary(
                args.term,
                division=args.division,
                max_invoices=args.max_invoices,
                max_receivables=args.max_receivables,
            )
        else:
            raise RuntimeError(f"unknown command: {args.command}")
    except (api.ApiError, api.MethodPolicyError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    _emit(value)
    return 0


__all__ = [
    "MANIFEST",
    "account_summary",
    "accounts",
    "current_division",
    "current_identity",
    "divisions",
    "main",
    "odata_literal",
    "receivables",
    "sales_invoices",
]
