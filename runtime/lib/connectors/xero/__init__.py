"""Xero Accounting connector — read-only grounding for billing/contact support.

Xero uses 1-based page-number pagination (page=1,2,3…) with a fixed 100-item ceiling, stopping
when a page returns fewer than 100 items. The generic lib.api offset style increments by item
count (0→100→200), which is incompatible. Force-code trigger (d) fired: non-standard pagination.

Every request also requires a ``Xero-tenant-id`` header (the organisation UUID), which is
per-connection and would otherwise need to be supplied as a raw ``--query`` flag on every lib.api
call. The connector threads it through automatically via ``--tenant-id``.

Read-only: only ever issues GETs. Writes to customer systems are explicitly out of scope.

CLI:
    python -m lib.connectors.xero tenants
    python -m lib.connectors.xero invoice --tenant-id <uuid> INV-0042
    python -m lib.connectors.xero contact --tenant-id <uuid> "Acme Corp"
    python -m lib.connectors.xero invoices --tenant-id <uuid> [--status OUTSTANDING]
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from lib import api

# ---------------------------------------------------------------------------
# Manifest registration
# ---------------------------------------------------------------------------

_MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")


def _load_manifest() -> api.Manifest:
    raw = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return api._manifest_from_dict(raw)


MANIFEST = api.register(_load_manifest())

BASE_URL = "https://api.xero.com/api.xro/2.0"


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="xero")


# ---------------------------------------------------------------------------
# Core pagination helper
# ---------------------------------------------------------------------------


def _xero_pages(
    c: api.Client,
    path: str,
    *,
    tenant_id: str,
    query: dict[str, Any] | None = None,
    items_key: str,
    max_pages: int = 1000,
) -> list[dict]:
    """Paginate a Xero list endpoint using 1-based page numbers.

    Xero returns at most 100 items per page (fixed ceiling). We stop when a page returns fewer
    than 100 items (last page) or when ``max_pages`` is reached. The ``Xero-tenant-id`` header is
    injected on every request — callers supply ``tenant_id`` once and the loop threads it through.
    """
    headers = {"Xero-tenant-id": tenant_id}
    base_q = dict(query or {})
    items: list[dict] = []
    page = 1
    while page <= max_pages:
        q = dict(base_q, page=page)
        body = c.get(path, query=q, headers=headers)
        page_items: list = body.get(items_key) or []
        items.extend(page_items)
        if len(page_items) < 100:
            break  # last page (Xero doesn't send an explicit has_more flag)
        page += 1
    return items


# ---------------------------------------------------------------------------
# High-level reads
# ---------------------------------------------------------------------------


def list_tenants() -> list[dict]:
    """Return the connected Xero organisations (tenants) for this credential.

    Uses the Xero Connections endpoint — does NOT require Xero-tenant-id.
    """
    c = api.client(
        api.Manifest(
            key="xero",
            base_url="https://api.xero.com",
            auth=api.Auth(strategy="bearer"),
        ),
        token_key="xero",
    )
    return c.get("connections") or []


def get_invoice(tenant_id: str, ref: str) -> dict | None:
    """Fetch one invoice by InvoiceNumber (INV-0042) or UUID. Returns None when not found."""
    c = _client()
    headers = {"Xero-tenant-id": tenant_id}
    # Xero lets us GET /Invoices/{InvoiceNumberOrID} directly.
    try:
        body = c.get(f"Invoices/{ref}", headers=headers)
        invoices = body.get("Invoices") or []
        return invoices[0] if invoices else None
    except api.ApiError as e:
        if e.status == 404:
            return None
        raise


def list_invoices(
    tenant_id: str,
    *,
    status: str | None = None,
    contact_id: str | None = None,
    max_pages: int = 10,
) -> list[dict]:
    """List invoices, optionally filtered by status and/or contact UUID."""
    c = _client()
    q: dict[str, Any] = {"order": "UpdatedDateUTC DESC"}
    if status:
        q["Statuses"] = status
    if contact_id:
        q["ContactIDs"] = contact_id
    return _xero_pages(c, "Invoices", tenant_id=tenant_id, query=q, items_key="Invoices", max_pages=max_pages)


def find_contact(tenant_id: str, name_or_id: str) -> dict | None:
    """Find a contact by name (partial match) or UUID.

    Tries a direct lookup first (works for UUIDs and exact names). On a 400 (not a valid id
    format) falls back to a ``searchTerm`` query. On a 404 (UUID not found) returns None without
    falling back — a UUID that 404s is definitively absent.
    """
    c = _client()
    headers = {"Xero-tenant-id": tenant_id}
    try:
        body = c.get(f"Contacts/{name_or_id}", headers=headers)
        contacts = body.get("Contacts") or []
        if contacts:
            return contacts[0]
    except api.ApiError as e:
        if e.status == 404:
            return None  # UUID present but contact definitively absent
        if e.status != 400:
            raise
    # 400 = the ref is not a valid UUID/id — treat as a name search.
    body = c.get("Contacts", query={"searchTerm": name_or_id}, headers=headers)
    contacts = body.get("Contacts") or []
    return contacts[0] if contacts else None


# ---------------------------------------------------------------------------
# Support summary
# ---------------------------------------------------------------------------


def contact_summary(tenant_id: str, name_or_id: str) -> dict:
    """Join contact + their outstanding invoices into a compact support dict."""
    contact = find_contact(tenant_id, name_or_id)
    if contact is None:
        return {"found": False, "ref": name_or_id}
    contact_id = contact.get("ContactID", "")
    invoices = list_invoices(tenant_id, contact_id=contact_id, status="OUTSTANDING", max_pages=3)
    return {
        "found": True,
        "contact": api.pick(contact, "ContactID,Name,EmailAddress,IsCustomer,IsSupplier,AccountsReceivableTaxType,HasAttachments"),
        "outstanding_invoices": [
            api.pick(inv, "InvoiceID,InvoiceNumber,Status,Total,AmountDue,AmountPaid,CurrencyCode,DueDate,UpdatedDateUTC")
            for inv in invoices
        ],
    }


def _fmt_money(amount: Any, currency: Any) -> str:
    cur = (str(currency) if currency else "").strip()
    try:
        return f"{float(amount):.2f} {cur}".strip()
    except (TypeError, ValueError):
        return str(amount)


def summary_to_markdown(s: dict) -> str:
    if not s.get("found"):
        return f"# Xero contact not found\nNo contact matched `{s.get('ref', '')}`."

    contact = s["contact"]
    lines = [f"# Xero: {contact.get('Name', '?')}"]
    if contact.get("EmailAddress"):
        lines.append(f"- Email: {contact['EmailAddress']}")
    flags = []
    if contact.get("IsCustomer"):
        flags.append("customer")
    if contact.get("IsSupplier"):
        flags.append("supplier")
    if flags:
        lines.append(f"- Type: {', '.join(flags)}")

    invoices = s.get("outstanding_invoices") or []
    lines.append(f"\n## Outstanding invoices ({len(invoices)})")
    if invoices:
        for inv in invoices:
            num = inv.get("InvoiceNumber") or inv.get("InvoiceID", "?")
            due = inv.get("AmountDue")
            currency = inv.get("CurrencyCode", "")
            due_date = (inv.get("DueDate") or "").replace("T00:00:00", "")
            lines.append(
                f"- `{num}`: {_fmt_money(due, currency)} due"
                + (f" by {due_date}" if due_date else "")
            )
    else:
        lines.append("- (none)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import json

    parser = argparse.ArgumentParser(prog="python -m lib.connectors.xero")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # tenants — no tenant-id needed
    sub.add_parser("tenants", help="list connected Xero organisations (find your tenant-id here)")

    # invoice
    inv_p = sub.add_parser("invoice", help="get one invoice by number or UUID")
    inv_p.add_argument("--tenant-id", required=True, help="Xero organisation UUID")
    inv_p.add_argument("ref", help="InvoiceNumber (INV-0042) or UUID")

    # invoices
    invs_p = sub.add_parser("invoices", help="list invoices (newest first)")
    invs_p.add_argument("--tenant-id", required=True, help="Xero organisation UUID")
    invs_p.add_argument("--status", default=None, help="e.g. OUTSTANDING, PAID, VOIDED, DRAFT")
    invs_p.add_argument("--max-pages", type=int, default=5, help="page cap (100/page)")

    # contact
    cont_p = sub.add_parser("contact", help="contact + outstanding invoices support summary")
    cont_p.add_argument("--tenant-id", required=True, help="Xero organisation UUID")
    cont_p.add_argument("ref", help="contact name (partial) or UUID")

    args = parser.parse_args(argv)

    if args.cmd == "tenants":
        tenants = list_tenants()
        if not tenants:
            print("No connected Xero organisations found.")
            return 0
        print("# Xero connected organisations\n")
        for t in tenants:
            print(f"- **{t.get('tenantName', '?')}** — `{t.get('tenantId', '?')}`  (type: {t.get('tenantType', '?')})")
        return 0

    if args.cmd == "invoice":
        inv = get_invoice(args.tenant_id, args.ref)
        if inv is None:
            print(f"# Xero invoice not found\nNo invoice matched `{args.ref}`.")
            return 0
        picked = api.pick(inv, "InvoiceID,InvoiceNumber,Type,Status,Contact.Name,Contact.EmailAddress,Total,AmountDue,AmountPaid,CurrencyCode,DueDate,UpdatedDateUTC,LineItems.*.Description,LineItems.*.Quantity,LineItems.*.UnitAmount")
        print(json.dumps(picked, indent=2, default=str))
        return 0

    if args.cmd == "invoices":
        invoices = list_invoices(
            args.tenant_id,
            status=args.status,
            max_pages=args.max_pages,
        )
        picked = [
            api.pick(inv, "InvoiceID,InvoiceNumber,Status,Contact.Name,Total,AmountDue,CurrencyCode,DueDate,UpdatedDateUTC")
            for inv in invoices
        ]
        print(json.dumps(picked, indent=2, default=str))
        return 0

    if args.cmd == "contact":
        s = contact_summary(args.tenant_id, args.ref)
        print(summary_to_markdown(s))
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
