"""Mollie write helpers for hosted actions."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from lib import api
from lib import action
from lib.connectors import mollie as mollie_read


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
