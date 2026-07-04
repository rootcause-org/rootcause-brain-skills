"""Mollie write helpers for hosted actions."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from lib import action
from lib.connectors import mollie as mollie_read


@dataclass(frozen=True)
class MollieRefund:
    id: str
    status: str
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
