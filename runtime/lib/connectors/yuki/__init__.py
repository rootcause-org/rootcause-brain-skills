"""Yuki Accounting connector — read-only SOAP grounding for supplier/invoice support.

Yuki exposes accounting data through SOAP services under ``api.yukiworks.be/ws``. A web-service
access key is exchanged for a short-lived session ID, then read methods are called on service-specific
``.asmx`` endpoints. The connector is a script because the useful support workflows are multi-call
SOAP reads and status inference, not simple REST paths.

Runtime posture: read-only. Upload helpers only prepare action payloads; they never send writes from
the run loop.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

import yaml

from lib import api, oauth

BASE_URL = "https://api.yukiworks.be/ws"
NAMESPACE = "http://www.theyukicompany.com/"
SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"

SERVICES = {
    "accounting": "Accounting.asmx",
    "accounting_info": "AccountingInfo.asmx",
    "archive": "Archive.asmx",
    "purchase": "Purchase.asmx",
}

SORT_ORDERS = {"ContactAsc", "ContactDesc", "AmountAsc", "AmountDesc", "DateAsc", "DateDesc"}
DOCUMENT_SORT_ORDERS = {
    "CreatedDesc",
    "CreatedAsc",
    "ModifiedDesc",
    "ModifiedAsc",
    "DocumentDateDesc",
    "DocumentDateAsc",
    "ContactNameAsc",
    "ContactNameDesc",
}
READ_SOAP_OPERATIONS = {
    "Authenticate",
    "SetCurrentDomain",
    "Domains",
    "Administrations",
    "AdministrationID",
    "GetTransactionDetails",
    "GetTransactions",
    "OutstandingCreditorItems",
    "OutstandingDebtorItems",
    "CheckOutstandingItem",
    "CheckOutstandingItemAdmin",
    "SearchDocuments",
    "FindDocument",
}

_MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")


def _load_manifest() -> api.Manifest:
    raw = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return api._manifest_from_dict(raw)


MANIFEST = api.register(_load_manifest())


class YukiError(RuntimeError):
    """Provider or connector error with secrets stripped at the source."""


@dataclass(frozen=True)
class UploadDocumentPayload:
    """Action-ready description for a Yuki archive upload.

    The payload intentionally contains no session ID. Action code should authenticate with action
    credentials, add ``sessionID``, then send this through the Archive service.
    """

    service: str
    operation: str
    message: dict[str, Any]
    filename: str
    content_type: str
    size_bytes: int


class Client:
    def __init__(self, api_key: str | None = None, *, base_url: str = BASE_URL):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session_id: str | None = None

    def connect(self, *, domain_id: str | None = None) -> "Client":
        self.authenticate()
        if domain_id:
            self.set_current_domain(domain_id)
        return self

    def authenticate(self) -> str:
        key = self.api_key or oauth.token("yuki")
        result = self._call("accounting_info", "Authenticate", {"accessKey": key}, result_name="AuthenticateResult")
        if not isinstance(result, str) or not result.strip():
            raise YukiError("Yuki authentication failed: no session ID returned")
        self.session_id = result.strip()
        return self.session_id

    def domains(self) -> list[dict[str, Any]]:
        result = self._session_call("accounting_info", "Domains", {}, result_name="DomainsResult")
        return _records(result, preferred=("domain", "administration", "item"))

    def administrations(self, *, domain_id: str | None = None) -> list[dict[str, Any]]:
        if domain_id:
            self.set_current_domain(domain_id)
        result = self._session_call("accounting_info", "Administrations", {}, result_name="AdministrationsResult")
        return _records(result, preferred=("administration", "item"))

    def administration_id(self, administration_name: str, *, domain_id: str | None = None) -> str:
        if domain_id:
            self.set_current_domain(domain_id)
        result = self._session_call(
            "accounting_info",
            "AdministrationID",
            {"administrationName": administration_name},
            result_name="AdministrationIDResult",
        )
        if not isinstance(result, str) or not result.strip():
            raise YukiError(f"Yuki administration not found: {administration_name!r}")
        return result.strip()

    def set_current_domain(self, domain_id: str) -> None:
        self._session_call("accounting_info", "SetCurrentDomain", {"domainID": domain_id}, result_name=None)

    def transaction_details(
        self,
        administration_id: str,
        *,
        start_date: str,
        end_date: str,
        gl_account_code: str = "",
        financial_mode: int = 0,
    ) -> list[dict[str, Any]]:
        result = self._session_call(
            "accounting_info",
            "GetTransactionDetails",
            {
                "administrationID": administration_id,
                "GLAccountCode": gl_account_code,
                "StartDate": _date_arg(start_date),
                "EndDate": _date_arg(end_date),
                "financialMode": financial_mode,
            },
            result_name="GetTransactionDetailsResult",
        )
        return _records(result, preferred=("transaction_info",))

    def outstanding_creditor_items(
        self,
        administration_id: str,
        *,
        include_bank_transactions: bool = True,
        sort_order: str = "DateAsc",
    ) -> list[dict[str, Any]]:
        return self._outstanding_items(
            "OutstandingCreditorItems",
            "OutstandingCreditorItemsResult",
            administration_id,
            include_bank_transactions=include_bank_transactions,
            sort_order=sort_order,
        )

    def outstanding_debtor_items(
        self,
        administration_id: str,
        *,
        include_bank_transactions: bool = True,
        sort_order: str = "DateAsc",
    ) -> list[dict[str, Any]]:
        return self._outstanding_items(
            "OutstandingDebtorItems",
            "OutstandingDebtorItemsResult",
            administration_id,
            include_bank_transactions=include_bank_transactions,
            sort_order=sort_order,
        )

    def check_outstanding_item(self, reference: str, *, administration_id: str | None = None) -> list[dict[str, Any]]:
        if administration_id:
            operation = "CheckOutstandingItemAdmin"
            result_name = "CheckOutstandingItemAdminResult"
            message = {"administrationID": administration_id, "Reference": reference}
        else:
            operation = "CheckOutstandingItem"
            result_name = "CheckOutstandingItemResult"
            message = {"Reference": reference}
        result = self._session_call("accounting", operation, message, result_name=result_name)
        return _records(result, preferred=("item", "outstanding_item"))

    def search_documents(
        self,
        *,
        text: str,
        search_option: str = "All",
        folder_id: int = -1,
        tab_id: int = -1,
        sort_order: str = "DocumentDateDesc",
        start_date: str = "2000-01-01",
        end_date: str | None = None,
        number_of_records: int = 50,
        start_record: int = 0,
    ) -> list[dict[str, Any]]:
        if sort_order not in DOCUMENT_SORT_ORDERS:
            raise YukiError(f"unsupported Yuki document sort order: {sort_order}")
        result = self._session_call(
            "archive",
            "SearchDocuments",
            {
                "searchOption": search_option,
                "searchText": text,
                "folderID": folder_id,
                "tabID": tab_id,
                "sortOrder": sort_order,
                "startDate": _date_arg(start_date),
                "endDate": _date_arg(end_date or dt.date.today().isoformat()),
                "numberOfRecords": number_of_records,
                "startRecord": start_record,
            },
            result_name="SearchDocumentsResult",
        )
        return _records(result, preferred=("document", "item"))

    def find_document(self, document_id: str) -> dict[str, Any] | None:
        result = self._session_call("archive", "FindDocument", {"documentID": document_id}, result_name="FindDocumentResult")
        docs = _records(result, preferred=("document", "item"))
        return docs[0] if docs else _as_dict(result)

    def supplier_invoice_status(
        self,
        administration_id: str,
        *,
        supplier: str | None = None,
        reference: str | None = None,
        amount: float | None = None,
    ) -> dict[str, Any]:
        outstanding = self.outstanding_creditor_items(administration_id)
        matches = filter_outstanding_items(outstanding, supplier=supplier, reference=reference, amount=amount)
        return invoice_status_from_matches(matches, supplier=supplier, reference=reference, amount=amount)

    def _outstanding_items(
        self,
        operation: str,
        result_name: str,
        administration_id: str,
        *,
        include_bank_transactions: bool,
        sort_order: str,
    ) -> list[dict[str, Any]]:
        if sort_order not in SORT_ORDERS:
            raise YukiError(f"unsupported Yuki outstanding sort order: {sort_order}")
        result = self._session_call(
            "accounting",
            operation,
            {
                "administrationID": administration_id,
                "includeBankTransactions": include_bank_transactions,
                "sortOrder": sort_order,
            },
            result_name=result_name,
        )
        return [_normalize_outstanding_item(row) for row in _records(result, preferred=("item",))]

    def _session_call(
        self,
        service: str,
        operation: str,
        message: dict[str, Any],
        *,
        result_name: str | None,
    ) -> Any:
        if not self.session_id:
            self.authenticate()
        return self._call(service, operation, {"sessionID": self.session_id, **message}, result_name=result_name)

    def _call(self, service: str, operation: str, message: dict[str, Any], *, result_name: str | None) -> Any:
        if operation not in READ_SOAP_OPERATIONS:
            raise YukiError(f"Yuki SOAP operation {operation!r} is not available in read-only grounding")
        endpoint = f"{self.base_url}/{SERVICES[service]}"
        envelope = _soap_envelope(operation, message)
        client = api.Client(
            manifest=api.Manifest(
                key="yuki",
                base_url="",
                auth=api.Auth(strategy="none"),
                allowed_post_paths=("/ws/*.asmx",),
            ),
            credential="",
        )
        response = client._send(
            "POST",
            endpoint,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f"{NAMESPACE}{operation}",
            },
            data=envelope.encode("utf-8"),
        )
        return _parse_soap(response.text, operation=operation, result_name=result_name)


def client(*, domain_id: str | None = None) -> Client:
    return Client().connect(domain_id=domain_id)


def filter_outstanding_items(
    items: Iterable[dict[str, Any]],
    *,
    supplier: str | None = None,
    reference: str | None = None,
    amount: float | None = None,
) -> list[dict[str, Any]]:
    supplier_norm = _norm(supplier)
    reference_norm = _norm(reference)
    out = []
    for item in items:
        haystack = " ".join(
            str(item.get(k, ""))
            for k in ("contact", "description", "description_clean", "reference", "document_reference", "invoice_number")
        )
        if supplier_norm and supplier_norm not in _norm(haystack):
            continue
        if reference_norm and reference_norm not in _norm(haystack):
            continue
        if amount is not None and not _amount_close(item.get("open_amount") or item.get("amount"), amount):
            continue
        out.append(item)
    return out


def invoice_status_from_matches(
    matches: list[dict[str, Any]],
    *,
    supplier: str | None = None,
    reference: str | None = None,
    amount: float | None = None,
) -> dict[str, Any]:
    query = {k: v for k, v in {"supplier": supplier, "reference": reference, "amount": amount}.items() if v not in (None, "")}
    if matches:
        return {
            "status": "open_or_unpaid",
            "query": query,
            "evidence": matches,
            "summary": "Matching outstanding creditor item(s) still exist in Yuki.",
        }
    return {
        "status": "not_currently_outstanding",
        "query": query,
        "evidence": [],
        "summary": "No matching outstanding creditor item found. Usually paid/settled or not booked as an open creditor item.",
    }


def prepare_upload_document(
    path: str | Path,
    *,
    administration_id: str,
    folder: int,
) -> UploadDocumentPayload:
    p = Path(path)
    data = p.read_bytes()
    return UploadDocumentPayload(
        service="archive",
        operation="UploadDocument",
        message={
            "fileName": p.name,
            "data": base64.b64encode(data).decode("ascii"),
            "folder": folder,
            "administrationID": administration_id,
        },
        filename=p.name,
        content_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream",
        size_bytes=len(data),
    )


def prepare_upload_document_with_data(
    path: str | Path,
    *,
    administration_id: str,
    folder: int,
    currency: str,
    amount: str | float,
    payment_method: int,
    cost_category: str = "",
    project: str = "",
    remarks: str = "",
) -> UploadDocumentPayload:
    payload = prepare_upload_document(path, administration_id=administration_id, folder=folder)
    message = {
        **payload.message,
        "currency": currency,
        "amount": str(amount),
        "costCategory": cost_category,
        "paymentMethod": payment_method,
        "project": project,
        "remarks": remarks,
    }
    return UploadDocumentPayload(
        service="archive",
        operation="UploadDocumentWithData",
        message=message,
        filename=payload.filename,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
    )


def action_message(payload: UploadDocumentPayload, *, session_id: str) -> dict[str, Any]:
    return {"sessionID": session_id, **payload.message}


def _soap_envelope(operation: str, message: dict[str, Any]) -> str:
    body = "".join(_xml_value(k, v) for k, v in message.items() if v is not None)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{SOAP_ENV}">'
        "<soap:Body>"
        f'<{operation} xmlns="{NAMESPACE}">{body}</{operation}>'
        "</soap:Body>"
        "</soap:Envelope>"
    )


def _xml_value(name: str, value: Any) -> str:
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, dict):
        inner = "".join(_xml_value(k, v) for k, v in value.items() if v is not None)
        return f"<{name}>{inner}</{name}>"
    elif isinstance(value, list):
        return "".join(_xml_value(name, v) for v in value)
    else:
        text = str(value)
    return f"<{name}>{html.escape(text, quote=False)}</{name}>"


def _parse_soap(xml_text: str, *, operation: str, result_name: str | None) -> Any:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise YukiError(f"Yuki returned invalid XML for {operation}: {exc}") from exc
    fault = _find_first(root, "Fault")
    if fault is not None:
        detail = _text(_find_first(fault, "faultstring")) or _text(fault) or "SOAP fault"
        raise YukiError(f"Yuki {operation} failed: {detail}")
    if result_name is None:
        return {}
    result = _find_first(root, result_name)
    if result is None:
        return {}
    return _element_to_obj(result)


def _element_to_obj(element: ET.Element) -> Any:
    children = list(element)
    if not children:
        return _text(element)
    grouped: dict[str, list[Any]] = {}
    for child in children:
        key = _snake(_local(child.tag))
        grouped.setdefault(key, []).append(_element_to_obj(child))
    return {key: values[0] if len(values) == 1 else values for key, values in grouped.items()}


def _find_first(element: ET.Element, local_name: str) -> ET.Element | None:
    for el in element.iter():
        if _local(el.tag) == local_name:
            return el
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _snake(name: str) -> str:
    if name.isupper():
        return name.lower()
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1)
    return s2.replace("__", "_").lower()


def _text(element: ET.Element | None) -> str:
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def _records(obj: Any, *, preferred: tuple[str, ...]) -> list[dict[str, Any]]:
    found = _find_records(obj, preferred)
    return [_as_dict(v) for v in found]


def _find_records(obj: Any, preferred: tuple[str, ...]) -> list[Any]:
    if isinstance(obj, list):
        return obj
    if not isinstance(obj, dict):
        return []
    for name in preferred:
        value = obj.get(name)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value]
    for value in obj.values():
        found = _find_records(value, preferred)
        if found:
            return found
    return []


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    return {"value": value}


def _normalize_outstanding_item(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    desc = html.unescape(str(out.get("description", "") or "")).strip()
    contact = str(out.get("contact", "") or "").strip()
    out["description_clean"] = _clean_description(desc, contact)
    for key in ("open_amount", "original_amount", "amount"):
        if key in out:
            out[key] = _decimalish(out[key])
    return out


def _clean_description(desc: str, contact: str = "") -> str:
    cleaned = desc
    cleaned = re.sub(r"\| Klantreferentie: .*", "", cleaned)
    cleaned = re.sub(r"\| Netto bedrag: .*", "", cleaned)
    cleaned = re.sub(r"\| Vreemde valuta: .*", "", cleaned)
    cleaned = re.sub(r"^Factuur van\s+", "", cleaned)
    cleaned = cleaned.strip(" |")
    if cleaned in {"", "Kaartbetaling", "Domiciliering"} and contact:
        return contact
    return cleaned


def _decimalish(value: Any) -> Any:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return value


def _amount_close(value: Any, expected: float) -> bool:
    try:
        return abs(float(value) - float(expected)) < 0.01
    except (TypeError, ValueError):
        return False


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _date_arg(value: str | dt.date | dt.datetime) -> str:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _limit(items: list[dict[str, Any]], max_items: int) -> list[dict[str, Any]]:
    return items[:max(0, max_items)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.yuki")
    parser.add_argument("--domain-id", dest="global_domain_id", default=None, help="set current Yuki domain before the command")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("domains", help="list accessible Yuki domains")

    admins = sub.add_parser("administrations", help="list administrations in current/domain-id domain")
    admins.add_argument("--domain-id", dest="admin_domain_id", default=None, help="domain UUID; overrides the global --domain-id")

    admin_id = sub.add_parser("administration-id", help="resolve an administration UUID by name")
    admin_id.add_argument("--name", required=True, help='administration name, e.g. "KampAdmin BV"')
    admin_id.add_argument("--domain-id", dest="admin_id_domain_id", default=None, help="domain UUID; overrides the global --domain-id")

    outstanding = sub.add_parser("outstanding-purchases", help="list open supplier invoices/payables")
    outstanding.add_argument("--administration-id", required=True)
    outstanding.add_argument("--supplier", default=None)
    outstanding.add_argument("--reference", default=None)
    outstanding.add_argument("--sort-order", default="DateAsc", choices=sorted(SORT_ORDERS))
    outstanding.add_argument("--exclude-bank-transactions", action="store_true")
    outstanding.add_argument("--max-items", type=int, default=100)

    status = sub.add_parser("invoice-status", help="infer paid/open status from outstanding creditor items")
    status.add_argument("--administration-id", required=True)
    status.add_argument("--supplier", default=None)
    status.add_argument("--reference", default=None)
    status.add_argument("--amount", type=float, default=None)

    search = sub.add_parser("search-documents", help="search Yuki archive documents")
    search.add_argument("--text", required=True)
    search.add_argument("--option", default="All", choices=["All", "Creator", "Contact", "Subject", "Tag", "Type"])
    search.add_argument("--folder-id", type=int, default=-1, help="-1 searches all folders")
    search.add_argument("--tab-id", type=int, default=-1, help="-1 searches all tabs")
    search.add_argument("--sort-order", default="DocumentDateDesc", choices=sorted(DOCUMENT_SORT_ORDERS))
    search.add_argument("--max-items", type=int, default=50)

    tx = sub.add_parser("transaction-details", help="list transaction details for an administration/date range")
    tx.add_argument("--administration-id", required=True)
    tx.add_argument("--start-date", required=True)
    tx.add_argument("--end-date", required=True)
    tx.add_argument("--gl-account-code", default="")
    tx.add_argument("--max-items", type=int, default=100)

    upload = sub.add_parser("upload-payload", help="prepare, but do not send, an action upload payload")
    upload.add_argument("--file", required=True)
    upload.add_argument("--administration-id", required=True)
    upload.add_argument("--folder", type=int, required=True)
    upload.add_argument("--with-data", action="store_true")
    upload.add_argument("--currency", default="EUR")
    upload.add_argument("--amount", default="0")
    upload.add_argument("--payment-method", type=int, default=0)
    upload.add_argument("--cost-category", default="")
    upload.add_argument("--project", default="")
    upload.add_argument("--remarks", default="")
    upload.add_argument("--include-base64", action="store_true", help="emit file bytes; intended for action tests only")

    args = parser.parse_args(argv)

    if args.cmd == "upload-payload":
        if args.with_data:
            payload = prepare_upload_document_with_data(
                args.file,
                administration_id=args.administration_id,
                folder=args.folder,
                currency=args.currency,
                amount=args.amount,
                payment_method=args.payment_method,
                cost_category=args.cost_category,
                project=args.project,
                remarks=args.remarks,
            )
        else:
            payload = prepare_upload_document(args.file, administration_id=args.administration_id, folder=args.folder)
        out = {
            "service": payload.service,
            "operation": payload.operation,
            "filename": payload.filename,
            "content_type": payload.content_type,
            "size_bytes": payload.size_bytes,
            "message": payload.message if args.include_base64 else {**payload.message, "data": "<base64 omitted>"},
        }
        _print_json(out)
        return 0

    yuki = Client().connect(domain_id=args.global_domain_id)
    if args.cmd == "domains":
        _print_json(yuki.domains())
        return 0
    if args.cmd == "administrations":
        _print_json(yuki.administrations(domain_id=args.admin_domain_id or args.global_domain_id))
        return 0
    if args.cmd == "administration-id":
        _print_json({"name": args.name, "administration_id": yuki.administration_id(args.name, domain_id=args.admin_id_domain_id or args.global_domain_id)})
        return 0
    if args.cmd == "outstanding-purchases":
        items = yuki.outstanding_creditor_items(
            args.administration_id,
            include_bank_transactions=not args.exclude_bank_transactions,
            sort_order=args.sort_order,
        )
        items = filter_outstanding_items(items, supplier=args.supplier, reference=args.reference)
        _print_json(_limit(items, args.max_items))
        return 0
    if args.cmd == "invoice-status":
        _print_json(
            yuki.supplier_invoice_status(
                args.administration_id,
                supplier=args.supplier,
                reference=args.reference,
                amount=args.amount,
            )
        )
        return 0
    if args.cmd == "search-documents":
        docs = yuki.search_documents(
            text=args.text,
            search_option=args.option,
            folder_id=args.folder_id,
            tab_id=args.tab_id,
            sort_order=args.sort_order,
            number_of_records=args.max_items,
        )
        _print_json(_limit(docs, args.max_items))
        return 0
    if args.cmd == "transaction-details":
        rows = yuki.transaction_details(
            args.administration_id,
            start_date=args.start_date,
            end_date=args.end_date,
            gl_account_code=args.gl_account_code,
        )
        _print_json(_limit(rows, args.max_items))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
