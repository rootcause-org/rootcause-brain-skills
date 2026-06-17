"""Read-only Stripe access for billing grounding.

Uses the project's Stripe key (read scopes only), injected verbatim from the per-project ``.env``
(no remapping). The restriction is enforced by the key VALUE — a restricted ``rk_`` key
— not by the variable name, so the name is a free label. These helpers only ever read. The
underlying ``stripe`` SDK is the installed package — imported lazily so this module loads even
if a project has no Stripe configured.

Onboarding's documented standard is ``STRIPE_RESTRICTED_KEY`` (what internal/testsetup/seed.go
seeds), but live projects in the field seal ``STRIPE_API_KEY`` (e.g. momentum-tools — and the
brain skills read that name too). A single hard-coded name silently broke every Stripe call for
those projects, so we resolve the key from BOTH names, preferring the documented one. Keep new
projects on ``STRIPE_RESTRICTED_KEY`` to stay aligned with onboarding.
"""

import os

# Candidate env-var names in priority order: the documented onboarding standard first, then the
# name live projects actually seal. The first non-empty one wins.
_KEY_VARS = ("STRIPE_RESTRICTED_KEY", "STRIPE_API_KEY")


def _client():
    key = next((os.environ[v] for v in _KEY_VARS if os.environ.get(v)), None)
    if not key:
        raise RuntimeError(
            "no Stripe key set — expected one of "
            + " or ".join(_KEY_VARS)
            + " in this run's env (no Stripe configured for this project?)"
        )
    # Import the real SDK lazily and configure the module-level key. Lazy so `from lib import
    # stripe` never fails for a project without Stripe.
    import stripe as _sdk

    _sdk.api_key = key
    return _sdk


def customer(customer_id: str) -> dict:
    """Retrieve a customer object by id."""
    return _client().Customer.retrieve(customer_id)


def latest_invoice(customer_id: str) -> dict | None:
    """Return the customer's most recent invoice, or None."""
    invoices = _client().Invoice.list(customer=customer_id, limit=1)
    data = invoices.get("data", [])
    return data[0] if data else None


def usage_summary(customer_id: str, limit: int = 10) -> list[dict]:
    """Return the customer's recent invoices (newest first) for a usage/billing breakdown."""
    invoices = _client().Invoice.list(customer=customer_id, limit=limit)
    return list(invoices.get("data", []))
