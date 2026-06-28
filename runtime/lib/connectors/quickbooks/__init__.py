"""QuickBooks Online support connector — reads Customers, Invoices, Payments, CompanyInfo.

Force-code triggers that required a script (not just manifest.yaml):
(a) Field pre-selection: Customer/Invoice objects carry 50+ fields; support needs 5–8.
    Pre-selecting in the QB SQL query is idiomatic and keeps context small.
(d) Non-standard pagination: QB embeds pagination *inside* the SQL query string
    (``STARTPOSITION N MAXRESULTS M`` are SQL clauses, not separate query params).
    lib.api's offset style sends ``offset_param`` as a standalone query param — it can't
    inject values into an embedded SQL string.
(e) QB query DSL: the ``/query`` endpoint requires SQL-like strings; a thin wrapper with
    pre-built queries prevents footguns (unquoted string values, missing MAXRESULTS, wrong
    field names per entity).

Auth: OAuth 2.0 bearer token injected as ``RC_CONN_QUICKBOOKS``.
RealmId: the company identifier varies per QB company; resolved from ``RC_CONN_QUICKBOOKS_REALM_ID``.

This connector imports ``lib.api`` — it never re-implements retry/backoff/rate-limiting.

CLI:
    python -m lib.connectors.quickbooks customer "Acme Corp"
    python -m lib.connectors.quickbooks customer --email acme@example.com
    python -m lib.connectors.quickbooks invoices --customer-id 42
    python -m lib.connectors.quickbooks invoice 1001
    python -m lib.connectors.quickbooks company
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from lib import api

# Pinned API version segment — part of the base path, not a header.
_API_VERSION = "v3"

# QB SQL field sets pre-selected for support grounding. QB SQL uses entity-prefixed field names
# only in JOINs; for single-entity SELECT the plain field names are correct.
_CUSTOMER_FIELDS = (
    "Id, DisplayName, PrimaryEmailAddr, PrimaryPhone, Balance, "
    "BillAddr, Active, CompanyName, GivenName, FamilyName, "
    "MetaData"
)

_INVOICE_FIELDS = (
    "Id, DocNumber, TxnDate, DueDate, TotalAmt, Balance, "
    "CustomerRef, EmailStatus, PrintStatus, "
    "BillEmail, Line, MetaData"
)

_PAYMENT_FIELDS = (
    "Id, TxnDate, TotalAmt, UnappliedAmt, CustomerRef, "
    "DepositToAccountRef, PaymentMethodRef, MetaData"
)

_COMPANY_FIELDS = (
    "CompanyName, LegalName, CompanyAddr, FiscalYearStartMonth, "
    "Country, Email, PrimaryPhone, WebAddr"
)

# The manifest row for this connector. base_url is set per-call using the resolved realmId;
# the YAML declares the static host as documentation — the connector overrides it at runtime.
_MANIFEST = api.Manifest(
    key="quickbooks",
    base_url="https://quickbooks.api.intuit.com",  # host only; path set per-call
    auth=api.Auth(strategy="bearer"),
    pagination=api.Pagination(style="none"),  # connector drives SQL-embedded pagination manually
    rate_limit_remaining_header="",
    default_headers={"Accept": "application/json"},
)
api.register(_MANIFEST)


def _realm_id() -> str:
    """Resolve the QB company realmId from ``RC_CONN_QUICKBOOKS_REALM_ID``.

    The realmId is the numeric company identifier in QB URLs (.../app/...?companyId=<realmId>).
    Must be set alongside ``RC_CONN_QUICKBOOKS`` — unlike Salesforce we can't derive it from
    the token because QB tokens are not bound to a single company (a token may access multiple
    companies; the operator picks the target company by setting this env var).
    """
    realm = os.environ.get("RC_CONN_QUICKBOOKS_REALM_ID", "").strip()
    if not realm:
        raise RuntimeError(
            "RC_CONN_QUICKBOOKS_REALM_ID is not set. "
            "Set it to the numeric company ID from your QuickBooks Online dashboard URL "
            "(.../app/...?companyId=<realmId>)."
        )
    return realm


def _client(realm: str | None = None) -> api.Client:
    """Build a lib.api Client for the given QB company."""
    rid = realm or _realm_id()
    # Build a per-call manifest with the fully resolved base URL so _join works correctly.
    manifest = api.Manifest(
        key="quickbooks",
        base_url=f"https://quickbooks.api.intuit.com/{_API_VERSION}/company/{rid}",
        auth=api.Auth(strategy="bearer"),
        pagination=api.Pagination(style="none"),
        rate_limit_remaining_header="",
        default_headers={"Accept": "application/json"},
    )
    return api.client(manifest, token_key="quickbooks")


def _qb_query(
    sql: str,
    entity: str,
    *,
    realm: str | None = None,
    page_size: int = 100,
    max_records: int = 500,
) -> list[dict]:
    """Execute a QB SQL query and page through results up to ``max_records``.

    QB embeds pagination in the SQL: ``STARTPOSITION N MAXRESULTS M``. Each response carries
    ``QueryResponse.<Entity>`` (the item list) and ``QueryResponse.totalCount``.
    The connector drives the STARTPOSITION offset here — nowhere else.

    ``sql`` must NOT include STARTPOSITION/MAXRESULTS (we append them).
    ``entity`` is the QB entity name (``Customer``, ``Invoice``, etc.) used to extract items
    from the response envelope.
    """
    c = _client(realm)
    records: list[dict] = []
    start = 1  # QB uses 1-based offset
    while len(records) < max_records:
        page_sql = f"{sql} STARTPOSITION {start} MAXRESULTS {page_size}"
        body = c.get("query", query={"query": page_sql, "minorversion": "65"})
        qr = body.get("QueryResponse") or {}
        page_items: list[dict] = list(qr.get(entity) or [])
        records.extend(page_items)
        # QB returns fewer items than MAXRESULTS when exhausted (or omits the entity key entirely).
        if len(page_items) < page_size:
            break
        start += len(page_items)
    return records[:max_records]


# ---------------------------------------------------------------------------
# Public query helpers
# ---------------------------------------------------------------------------


def query_customer(
    display_name: str | None = None,
    *,
    email: str | None = None,
    customer_id: str | None = None,
    realm: str | None = None,
) -> list[dict]:
    """Return Customers matching display_name (LIKE), email, or QB customer id.

    At least one of ``display_name``, ``email``, or ``customer_id`` is required.
    """
    if not display_name and not email and not customer_id:
        raise RuntimeError("at least one of display_name, email, or customer_id is required")

    if customer_id:
        # Direct read endpoint — faster than a query; no SQL needed.
        c = _client(realm)
        body = c.get(f"customer/{customer_id}")
        cust = body.get("Customer")
        return [cust] if cust else []

    clauses: list[str] = []
    if display_name:
        safe = display_name.replace("'", "\\'")
        clauses.append(f"DisplayName LIKE '%{safe}%'")
    if email:
        safe_email = email.replace("'", "\\'")
        clauses.append(f"PrimaryEmailAddr = '{safe_email}'")

    where = " AND ".join(clauses)
    sql = f"SELECT {_CUSTOMER_FIELDS} FROM Customer WHERE {where}"
    return _qb_query(sql, "Customer", realm=realm, max_records=50)


def query_invoices(
    *,
    customer_id: str | None = None,
    invoice_id: str | None = None,
    realm: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return Invoices by QB invoice id or filtered by QB customer id (newest first)."""
    if invoice_id:
        c = _client(realm)
        body = c.get(f"invoice/{invoice_id}")
        inv = body.get("Invoice")
        return [inv] if inv else []

    if customer_id:
        safe = customer_id.replace("'", "\\'")
        sql = (
            f"SELECT {_INVOICE_FIELDS} FROM Invoice "
            f"WHERE CustomerRef = '{safe}' "
            f"ORDERBY MetaData.LastUpdatedTime DESC"
        )
    else:
        sql = (
            f"SELECT {_INVOICE_FIELDS} FROM Invoice "
            f"ORDERBY MetaData.LastUpdatedTime DESC"
        )
    return _qb_query(sql, "Invoice", realm=realm, max_records=limit)


def query_payments(
    *,
    customer_id: str | None = None,
    realm: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return Payments for a customer (newest first)."""
    if customer_id:
        safe = customer_id.replace("'", "\\'")
        sql = (
            f"SELECT {_PAYMENT_FIELDS} FROM Payment "
            f"WHERE CustomerRef = '{safe}' "
            f"ORDERBY MetaData.LastUpdatedTime DESC"
        )
    else:
        sql = f"SELECT {_PAYMENT_FIELDS} FROM Payment ORDERBY MetaData.LastUpdatedTime DESC"
    return _qb_query(sql, "Payment", realm=realm, max_records=limit)


def query_company_info(*, realm: str | None = None) -> dict | None:
    """Return the QB company info for the configured realmId."""
    rid = realm or _realm_id()
    c = _client(rid)
    body = c.get(f"companyinfo/{rid}")
    return body.get("CompanyInfo")


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _val(obj: Any, path: str) -> Any:
    """Dotted-path lookup that tolerates nested QB response dicts."""
    found, v = api._dget(obj, path.split("."))
    return v if found else None


def _customer_to_md(c: dict) -> str:
    name = _val(c, "DisplayName") or "(unknown)"
    email = _val(c, "PrimaryEmailAddr.Address") or ""
    phone = _val(c, "PrimaryPhone.FreeFormNumber") or ""
    company = _val(c, "CompanyName") or ""
    balance = _val(c, "Balance")
    active = _val(c, "Active")
    cid = _val(c, "Id") or ""

    lines = [f"### Customer {cid}: {name}"]
    if company and company != name:
        lines.append(f"- Company: {company}")
    if email:
        lines.append(f"- Email: {email}")
    if phone:
        lines.append(f"- Phone: {phone}")
    if balance is not None:
        lines.append(f"- Balance owing: {balance:.2f}")
    if active is False:
        lines.append("- **Inactive**")
    return "\n".join(lines)


def _invoice_to_md(inv: dict) -> str:
    iid = _val(inv, "Id") or ""
    doc = _val(inv, "DocNumber") or iid
    date = (_val(inv, "TxnDate") or "")[:10]
    due = (_val(inv, "DueDate") or "")[:10]
    total = _val(inv, "TotalAmt")
    balance = _val(inv, "Balance")
    cust = _val(inv, "CustomerRef.name") or _val(inv, "CustomerRef.value") or ""
    email_status = _val(inv, "EmailStatus") or ""

    lines = [f"### Invoice {doc}"]
    lines.append(f"- Date: {date}" + (f" | Due: {due}" if due else ""))
    if cust:
        lines.append(f"- Customer: {cust}")
    if total is not None:
        paid = (total - balance) if balance is not None else None
        lines.append(
            f"- Total: {total:.2f}"
            + (f" | Paid: {paid:.2f} | Balance: {balance:.2f}" if paid is not None else "")
        )
    if email_status:
        lines.append(f"- Email status: {email_status}")
    return "\n".join(lines)


def customers_to_markdown(customers: list[dict], *, title: str = "QuickBooks Customers") -> str:
    if not customers:
        return f"# {title}\n\nNo customers found."
    header = f"# {title} ({len(customers)} found)"
    return header + "\n\n" + "\n\n".join(_customer_to_md(c) for c in customers)


def invoices_to_markdown(invoices: list[dict], *, title: str = "QuickBooks Invoices") -> str:
    if not invoices:
        return f"# {title}\n\nNo invoices found."
    header = f"# {title} ({len(invoices)} found)"
    return header + "\n\n" + "\n\n".join(_invoice_to_md(inv) for inv in invoices)


def company_to_markdown(info: dict | None) -> str:
    if info is None:
        return "# QuickBooks Company\n\nNo company info found."
    name = _val(info, "CompanyName") or ""
    legal = _val(info, "LegalName") or ""
    country = _val(info, "Country") or ""
    fiscal = _val(info, "FiscalYearStartMonth") or ""
    addr = _val(info, "CompanyAddr") or {}
    city = addr.get("City", "") if isinstance(addr, dict) else ""

    lines = ["# QuickBooks Company Info"]
    if name:
        lines.append(f"- Name: {name}")
    if legal and legal != name:
        lines.append(f"- Legal name: {legal}")
    if city:
        lines.append(f"- City: {city}")
    if country:
        lines.append(f"- Country: {country}")
    if fiscal:
        lines.append(f"- Fiscal year starts: {fiscal}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.quickbooks",
        description="Read QuickBooks Online customers, invoices, payments, and company info.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # customer subcommand
    cust_p = sub.add_parser("customer", help="look up customers by name, email, or QB id")
    cust_p.add_argument("name", nargs="?", default="", help="display name (partial match)")
    cust_p.add_argument("--email", default="", help="filter by primary email")
    cust_p.add_argument("--customer-id", default="", dest="customer_id", help="QB customer id")
    cust_p.add_argument("--realm", default="", help="override QB realmId")

    # invoices subcommand
    inv_p = sub.add_parser("invoices", help="list invoices (optionally for a customer)")
    inv_p.add_argument("--customer-id", default="", dest="customer_id", help="QB customer id")
    inv_p.add_argument("--limit", type=int, default=20, help="max invoices to return")
    inv_p.add_argument("--realm", default="", help="override QB realmId")

    # invoice subcommand (single)
    inv1_p = sub.add_parser("invoice", help="show a single invoice by QB invoice id")
    inv1_p.add_argument("id", help="QB invoice id")
    inv1_p.add_argument("--realm", default="", help="override QB realmId")

    # company subcommand
    co_p = sub.add_parser("company", help="show company info")
    co_p.add_argument("--realm", default="", help="override QB realmId")

    args = parser.parse_args(argv)
    realm = (getattr(args, "realm", "") or None)

    if args.cmd == "customer":
        if not args.name and not args.email and not args.customer_id:
            parser.error("customer requires a name argument, --email, or --customer-id")
        customers = query_customer(
            args.name or None,
            email=args.email or None,
            customer_id=args.customer_id or None,
            realm=realm,
        )
        print(customers_to_markdown(customers))
        return 0

    if args.cmd == "invoices":
        invoices = query_invoices(
            customer_id=args.customer_id or None,
            realm=realm,
            limit=args.limit,
        )
        print(invoices_to_markdown(invoices))
        return 0

    if args.cmd == "invoice":
        invoices = query_invoices(invoice_id=args.id, realm=realm)
        print(invoices_to_markdown(invoices, title=f"QuickBooks Invoice {args.id}"))
        return 0

    if args.cmd == "company":
        info = query_company_info(realm=realm)
        print(company_to_markdown(info))
        return 0

    parser.error("unknown command")
    return 2
