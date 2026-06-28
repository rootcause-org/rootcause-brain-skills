"""Salesforce support connector — reads Cases, Contacts, Accounts via SOQL.

Force-code triggers that required a script (not just manifest.yaml):
(a) Field pre-selection: Salesforce Case/Contact objects are enormous (100+ fields); support needs
    5–8 fields. Pre-selecting in SOQL is idiomatic and required — ``SELECT *`` is not valid SOQL.
(d) Non-standard pagination: SOQL results carry ``nextRecordsUrl`` (a JSON path like
    ``/services/data/v59.0/query/01gxx...``), fetched verbatim as the next GET. lib.api's cursor
    style sends a query param; its link style follows HTTP headers. Neither matches.
(e) SOQL search DSL: the query endpoint requires properly quoted SOQL strings. A thin wrapper with
    pre-built queries prevents common footguns (missing quotes around strings, wrong WHERE syntax).

Auth: OAuth 2.0 bearer token injected as ``RC_CONN_SALESFORCE``. The instance subdomain is
org-specific; the connector resolves it from ``RC_CONN_SALESFORCE_INSTANCE`` (preferred, no
round-trip) or falls back to the token's ``/services/oauth2/userinfo`` endpoint.

This connector imports ``lib.api`` — it never re-implements retry/backoff/rate-limiting.

CLI:
    python -m lib.connectors.salesforce cases --email user@example.com
    python -m lib.connectors.salesforce cases --account "Acme Corp"
    python -m lib.connectors.salesforce contact user@example.com
"""

from __future__ import annotations

import argparse
import os
from typing import Any
from lib import api, oauth

# Salesforce REST API version. Increment here when support for newer features is needed;
# connectors in a self-owned system may keep this pinned for stability.
_API_VERSION = "v59.0"

# SOQL field sets pre-selected for support use — every field listed here must exist in a standard
# Salesforce org without custom configuration.
_CASE_FIELDS = (
    "Id,CaseNumber,Subject,Description,Status,Priority,Origin,"
    "CreatedDate,LastModifiedDate,"
    "Contact.Name,Contact.Email,"
    "Account.Name,OwnerId"
)

_CONTACT_FIELDS = (
    "Id,Name,Email,Phone,Title,Department,"
    "Account.Name,Account.Id,"
    "CreatedDate,LastModifiedDate"
)

_ACCOUNT_FIELDS = (
    "Id,Name,Type,Industry,BillingCity,BillingCountry,"
    "Website,Phone,OwnerId,"
    "CreatedDate,LastModifiedDate"
)

# Injected via lib.api's manifest/YAML loader. The base_url is overridden at call time because
# each org has a unique instance URL; the manifest row documents the login host as a placeholder.
_MANIFEST = api.Manifest(
    key="salesforce",
    base_url="",  # set per-call via the resolved instance URL
    auth=api.Auth(strategy="bearer"),
    pagination=api.Pagination(style="none"),  # connector drives pagination manually
    rate_limit_remaining_header="",
)
api.register(_MANIFEST)


def _instance_url() -> str:
    """Resolve the org-specific Salesforce instance URL.

    Prefers ``RC_CONN_SALESFORCE_INSTANCE`` (operator-supplied, no extra round-trip). Falls back
    to the token's ``/services/oauth2/userinfo`` identity endpoint on login.salesforce.com to get
    the ``urls.sobjects`` or ``instance_url`` from the identity response.

    The value is cached per-process (a run is one process, so one resolution is enough).
    """
    if _instance_url._cached:  # type: ignore[attr-defined]
        return _instance_url._cached  # type: ignore[attr-defined]
    env = os.environ.get("RC_CONN_SALESFORCE_INSTANCE", "").rstrip("/")
    if env:
        _instance_url._cached = env  # type: ignore[attr-defined]
        return env
    # Fall back: call the userinfo/identity endpoint on login.salesforce.com.
    # The credential from RC_CONN_SALESFORCE is already a valid bearer token.
    cred = oauth.token("salesforce")
    probe = api.Client(
        manifest=api.Manifest(
            key="salesforce",
            base_url="https://login.salesforce.com",
            auth=api.Auth(strategy="bearer"),
        ),
        credential=cred,
    )
    info = probe.get("/services/oauth2/userinfo")
    # The identity response includes `profile` and various URLs; `instance_url` is the cleanest.
    instance = info.get("profile", "").split("/")[2] if info.get("profile") else ""
    if instance:
        base = f"https://{instance}"
    else:
        # Fallback: some token responses include urls.sobjects; strip the path.
        sobjects = (info.get("urls") or {}).get("sobjects", "")
        base = sobjects.split("/services/")[0] if "/services/" in sobjects else ""
    if not base:
        raise RuntimeError(
            "Could not resolve Salesforce instance URL. "
            "Set RC_CONN_SALESFORCE_INSTANCE=https://<your-instance>.salesforce.com"
        )
    _instance_url._cached = base  # type: ignore[attr-defined]
    return base


_instance_url._cached = ""  # type: ignore[attr-defined]


def _client(instance: str) -> api.Client:
    """Build a lib.api Client pointed at the org's instance URL.

    SOQL paging is the ``body_url`` style: each page carries ``nextRecordsUrl`` (a path under the
    instance) which lib.api follows verbatim, ``_join``ing the relative path onto ``base_url``. The
    records live at ``records``. The framework drives the while-has-more loop — no hand-rolled loop.
    """
    manifest = api.Manifest(
        key="salesforce",
        base_url=instance,
        auth=api.Auth(strategy="bearer"),
        pagination=api.Pagination(
            style="body_url", next_url_field="nextRecordsUrl", items_field="records"
        ),
        rate_limit_remaining_header="",
    )
    return api.client(manifest, token_key="salesforce")


def _soql_query(soql: str, *, instance: str | None = None, max_records: int = 500) -> list[dict]:
    """Execute a SOQL query, collecting records across nextRecordsUrl pages up to ``max_records``."""
    base = instance or _instance_url()
    c = _client(base)
    path = f"/services/data/{_API_VERSION}/query"
    return c.collect(path, query={"q": soql}, max_items=max_records)["items"]


# ---------------------------------------------------------------------------
# Public query helpers
# ---------------------------------------------------------------------------


def query_cases(
    *,
    email: str | None = None,
    account: str | None = None,
    status: str | None = None,
    limit: int = 50,
    instance: str | None = None,
) -> list[dict]:
    """Return support Cases filtered by contact email or account name/id.

    At least one of ``email`` or ``account`` is required. ``status`` is an optional additional
    filter (e.g. ``"Open"``). Results are sorted newest first.
    """
    if not email and not account:
        raise RuntimeError("at least one of email or account is required")

    clauses: list[str] = []
    if email:
        safe = email.replace("'", "\\'")
        clauses.append(f"Contact.Email = '{safe}'")
    if account:
        acc = account.replace("'", "\\'")
        if account.startswith("001") and len(account) in (15, 18):
            # Looks like a Salesforce Account ID (15/18-char); match on AccountId directly.
            clauses.append(f"AccountId = '{acc}'")
        else:
            clauses.append(f"Account.Name = '{acc}'")
    if status:
        safe_status = status.replace("'", "\\'")
        clauses.append(f"Status = '{safe_status}'")

    where = " AND ".join(clauses)
    soql = (
        f"SELECT {_CASE_FIELDS} FROM Case "
        f"WHERE {where} "
        f"ORDER BY CreatedDate DESC "
        f"LIMIT {limit}"
    )
    return _soql_query(soql, instance=instance, max_records=limit)


def query_contact(email: str, *, instance: str | None = None) -> dict | None:
    """Fetch the first Contact matching ``email``. Returns None if not found."""
    safe = email.replace("'", "\\'")
    soql = (
        f"SELECT {_CONTACT_FIELDS} FROM Contact "
        f"WHERE Email = '{safe}' "
        f"ORDER BY CreatedDate DESC "
        f"LIMIT 1"
    )
    rows = _soql_query(soql, instance=instance, max_records=1)
    return rows[0] if rows else None


def query_account(name_or_id: str, *, instance: str | None = None) -> dict | None:
    """Fetch an Account by name or Salesforce ID. Returns None if not found."""
    safe = name_or_id.replace("'", "\\'")
    if name_or_id.startswith("001") and len(name_or_id) in (15, 18):
        where = f"Id = '{safe}'"
    else:
        where = f"Name = '{safe}'"
    soql = (
        f"SELECT {_ACCOUNT_FIELDS} FROM Account "
        f"WHERE {where} "
        f"LIMIT 1"
    )
    rows = _soql_query(soql, instance=instance, max_records=1)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _sf_val(obj: Any, path: str) -> Any:
    """Dotted-path lookup into a Salesforce response dict (handles nested relationship objects)."""
    found, val = api._dget(obj, path.split("."))
    return val if found else None


def _case_to_md(case: dict) -> str:
    """Render one Salesforce Case as a concise markdown block."""
    num = _sf_val(case, "CaseNumber") or _sf_val(case, "Id")
    subject = _sf_val(case, "Subject") or "(no subject)"
    status = _sf_val(case, "Status") or "?"
    priority = _sf_val(case, "Priority") or "Normal"
    origin = _sf_val(case, "Origin") or ""
    created = (_sf_val(case, "CreatedDate") or "")[:10]
    modified = (_sf_val(case, "LastModifiedDate") or "")[:10]
    contact_name = _sf_val(case, "Contact.Name") or ""
    contact_email = _sf_val(case, "Contact.Email") or ""
    account_name = _sf_val(case, "Account.Name") or ""
    description = (_sf_val(case, "Description") or "")[:300]

    lines = [f"### Case {num}: {subject}"]
    lines.append(f"- Status: **{status}** | Priority: {priority}" + (f" | Origin: {origin}" if origin else ""))
    lines.append(f"- Created: {created} | Modified: {modified}")
    if contact_name or contact_email:
        lines.append(f"- Contact: {contact_name}" + (f" <{contact_email}>" if contact_email else ""))
    if account_name:
        lines.append(f"- Account: {account_name}")
    if description:
        lines.append(f"- Description: {description}" + ("…" if len(_sf_val(case, "Description") or "") > 300 else ""))
    return "\n".join(lines)


def cases_to_markdown(cases: list[dict], *, title: str = "Salesforce Cases") -> str:
    """Render a list of Cases as markdown."""
    if not cases:
        return f"# {title}\n\nNo cases found."
    header = f"# {title} ({len(cases)} found)"
    return header + "\n\n" + "\n\n".join(_case_to_md(c) for c in cases)


def contact_to_markdown(contact: dict | None, email: str = "") -> str:
    """Render a Contact as markdown."""
    if contact is None:
        return "# Salesforce Contact\n\nNo contact found" + (f" for `{email}`." if email else ".")
    name = _sf_val(contact, "Name") or "(unknown)"
    lines = [f"# Salesforce Contact: {name}"]
    for label, path in [
        ("Email", "Email"), ("Phone", "Phone"), ("Title", "Title"),
        ("Department", "Department"), ("Account", "Account.Name"),
    ]:
        val = _sf_val(contact, path)
        if val:
            lines.append(f"- {label}: {val}")
    lines.append(f"- Created: {(_sf_val(contact, 'CreatedDate') or '')[:10]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.salesforce")
    sub = parser.add_subparsers(dest="cmd", required=True)

    cases_p = sub.add_parser("cases", help="list support cases for a contact or account")
    grp = cases_p.add_mutually_exclusive_group()
    grp.add_argument("--email", help="filter cases by contact email")
    grp.add_argument("--account", help="filter cases by account name or Salesforce Account ID")
    cases_p.add_argument("--status", default="", help="optional status filter (e.g. Open)")
    cases_p.add_argument("--limit", type=int, default=20, help="max cases to return (default 20)")
    cases_p.add_argument("--instance", default="", help="override instance URL")

    contact_p = sub.add_parser("contact", help="look up a Salesforce contact by email")
    contact_p.add_argument("email", help="contact email address")
    contact_p.add_argument("--instance", default="", help="override instance URL")

    args = parser.parse_args(argv)
    instance = getattr(args, "instance", "") or None

    if args.cmd == "cases":
        if not args.email and not args.account:
            parser.error("cases requires --email or --account")
        cases = query_cases(
            email=args.email or None,
            account=args.account or None,
            status=args.status or None,
            limit=args.limit,
            instance=instance,
        )
        label = args.email or args.account or "query"
        print(cases_to_markdown(cases, title=f"Cases for {label}"))
        return 0

    if args.cmd == "contact":
        contact = query_contact(args.email, instance=instance)
        print(contact_to_markdown(contact, email=args.email))
        return 0

    parser.error("unknown command")
    return 2
