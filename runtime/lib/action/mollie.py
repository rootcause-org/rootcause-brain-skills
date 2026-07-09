"""Mollie write helpers for hosted actions."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from lib import api
from lib import action
from lib.connectors import mollie as mollie_read


ACTION_HELPER_DOCS = {
    "provider": "mollie",
    "need": "Create Mollie payment links or refunds after read-only Mollie grounding",
    "connection": "mollie.write",
    "import": "from lib.action import mollie",
    "source_module": "lib.action.mollie",
    "manifest": [
        "Declare `connections: [mollie.write]`.",
        "Use a stable `idempotency_key` for every payment link or refund; derive it from the support case plus target payment/business object.",
        "Use string money values such as `10.00`, never floats; keep `currency` explicit.",
        "For refunds, include `payment_id`, `amount_value`, `currency`, `description`, and `idempotency_key`.",
        "For payment links, include `amount_value`, `currency`, `description`, and `idempotency_key`; add optional fields only when grounding says they apply.",
    ],
    "common_params": [
        "`payment_id`: Mollie payment ID (`tr_...`) for refunds.",
        "`amount_value` + `currency`: Mollie amount object fields, e.g. `20.00` + `EUR`.",
        "`description`: reviewer/customer-readable payment-link or refund reason.",
        "`idempotency_key`: deterministic retry key; required by both helpers.",
        "`profile_id` / `testmode`: only when the credential type and grounded Mollie evidence call for them.",
    ],
    "useful_for": [
        "create a one-off Mollie payment link for an approved manual collection or invoice amount",
        "refund part or all of an existing paid Mollie payment",
        "execute a payment-link or refund draft that was first checked with the read-only Mollie plan commands",
        "pass optional payment-link fields such as profile, redirect/webhook URLs, order lines, addresses, allowed methods, and test mode",
    ],
    "helpers": {
        "create_payment_refund": "Create a refund for an existing Mollie payment; returns `id`, `status`, and raw Mollie response.",
        "create_payment_link": "Create a Mollie payment link; returns `id`, `payment_link` checkout URL when Mollie provides one, and raw Mollie response.",
    },
    "patterns": [
        {
            "title": "Refund a payment after read-only planning",
            "code": """
from lib import action
from lib.action import mollie

@action.main
def run(p: action.Params) -> dict:
    if action.dry_run():
        return {"summary": f"Would refund **{p['currency']} {p['amount_value']}** on `{p['payment_id']}`."}

    refund = mollie.create_payment_refund(
        payment_id=p["payment_id"],
        amount_value=p["amount_value"],
        currency=p["currency"],
        description=p["description"],
        idempotency_key=f"refund:{p['payment_id']}:{p['amount_value']}:{p['currency']}",
    )
    return {"summary": f"Created Mollie refund **{refund.id}** ({refund.status}).", "refund_id": refund.id, "status": refund.status}
""",
        },
        {
            "title": "Create a payment link",
            "code": """
from lib import action
from lib.action import mollie

@action.main
def run(p: action.Params) -> dict:
    if action.dry_run():
        return {"summary": f"Would create a Mollie payment link for **{p['currency']} {p['amount_value']}**."}

    link = mollie.create_payment_link(
        amount_value=p["amount_value"],
        currency=p["currency"],
        description=p["description"],
        idempotency_key=f"payment-link:{p['reference']}",
        profile_id=p.get("profile_id", ""),
        expires_at=p.get("expires_at", ""),
    )
    return {"summary": f"Created Mollie payment link: {link.payment_link}", "payment_link_id": link.id, "payment_link": link.payment_link}
""",
        },
    ],
    "validation_failure": [
        "Both helpers reject blank `idempotency_key`; `create_payment_link` also rejects blank `description`.",
        "For refunds, ground/preflight with `python -m lib.connectors.mollie refund-plan <payment_id> --amount <value> --currency <currency> --description <text>` before proposing.",
        "For payment links, ground/preflight with `python -m lib.connectors.mollie payment-link-plan --amount <value> --currency <currency> --description <text>` plus optional profile/testmode/order-line fields only when needed.",
        "Mollie validation/API failures surface through `api.ApiError`/`action.ActionError` as reviewer-visible action failures; do not send an optimistic draft after a failed execution.",
    ],
    "do_not": [
        "Do not document or call non-existent write helpers for payment status checks; use the read-only Mollie connector or `lib.api` GET for reads.",
        "Do not POST to Mollie directly from the standard run loop; writes belong in hosted actions with `connections: [mollie.write]`.",
        "Do not pass floats for amounts; Mollie amount values must be strings.",
        "Do not omit or randomize idempotency keys; retries and double-clicks must not create duplicate money movement.",
        "Do not send `profile_id` or `testmode` blindly; OAuth organization tokens may need them, profile-bound API keys usually should not.",
    ],
}


@dataclass(frozen=True)
class MollieRefund:
    id: str
    status: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class MolliePaymentLink:
    id: str
    payment_link: str
    raw: dict[str, Any]


@lru_cache(maxsize=1)
def _client() -> api.Client:
    return action.client("mollie.write", manifest=mollie_read.MANIFEST)


def create_payment_refund(
    *,
    payment_id: str,
    amount_value: str,
    currency: str,
    description: str,
    idempotency_key: str,
    metadata: Any | None = None,
    testmode: bool | None = None,
) -> MollieRefund:
    if not idempotency_key.strip():
        raise action.ActionError("Mollie refunds require a stable idempotency_key")
    body: dict[str, Any] = {
        "amount": {"value": str(amount_value), "currency": str(currency)},
        "description": description,
    }
    if metadata is not None:
        body["metadata"] = metadata
    if testmode is not None:
        body["testmode"] = bool(testmode)
    raw = _client().post(
        f"/payments/{payment_id}/refunds",
        json=body,
        idempotency_key=idempotency_key,
    )
    return MollieRefund(id=str(raw.get("id", "")), status=str(raw.get("status", "")), raw=raw)


def create_payment_link(
    *,
    amount_value: str,
    currency: str,
    description: str,
    idempotency_key: str,
    profile_id: str = "",
    redirect_url: str = "",
    webhook_url: str = "",
    expires_at: str = "",
    reusable: bool = False,
    lines: list[dict[str, Any]] | None = None,
    billing_address: dict[str, Any] | None = None,
    shipping_address: dict[str, Any] | None = None,
    allowed_methods: list[str] | None = None,
    testmode: bool | None = None,
) -> MolliePaymentLink:
    """Create a one-off Mollie payment link from a human-confirmed hosted action."""
    if not idempotency_key.strip():
        raise action.ActionError("Mollie payment links require a stable idempotency_key")
    if not description.strip():
        raise action.ActionError("Mollie payment links require a description")
    body: dict[str, Any] = {
        "amount": {"value": str(amount_value), "currency": str(currency)},
        "description": description,
    }
    optional = {
        "profileId": profile_id,
        "redirectUrl": redirect_url,
        "webhookUrl": webhook_url,
        "expiresAt": expires_at,
    }
    for key, value in optional.items():
        if value:
            body[key] = value
    if reusable:
        body["reusable"] = True
    if lines:
        body["lines"] = lines
    if billing_address:
        body["billingAddress"] = billing_address
    if shipping_address:
        body["shippingAddress"] = shipping_address
    if allowed_methods:
        body["allowedMethods"] = allowed_methods
    if testmode is not None:
        body["testmode"] = bool(testmode)
    raw = _client().post(
        "/payment-links",
        json=body,
        idempotency_key=idempotency_key,
    )
    link = ""
    links = raw.get("_links")
    if isinstance(links, dict):
        payment_link = links.get("paymentLink")
        if isinstance(payment_link, dict):
            link = str(payment_link.get("href") or "")
    return MolliePaymentLink(id=str(raw.get("id", "")), payment_link=link, raw=raw)
