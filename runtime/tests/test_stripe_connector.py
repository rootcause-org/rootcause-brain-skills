"""vcrpy-cassette tests for the Stripe support connector — the reference progressive-disclosure
connector over lib.api. Replays hand-built cassettes (Stripe's documented example payloads) so the
multi-call join + markdown rendering are exercised with NO network and NO live key.

Recording: if RC_CONN_STRIPE (a restricted rk_test_ key) is in env, vcr records new cassettes with
secrets scrubbed at record time (Authorization header -> DUMMY). Otherwise it replays. A CI grep
(`test_no_token_prefixes_in_cassettes`) fails the build if any token prefix ever leaks into a
committed cassette.

    cd runtime && uv run --with . --with pytest --with vcrpy --no-project pytest tests/test_stripe_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import vcr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib.connectors import stripe as stripe_conn  # noqa: E402

CASSETTES = Path(__file__).resolve().parent / "cassettes"

# Scrub every credential-bearing field at record time. filter_headers swaps Authorization; the
# query-param filters cover APIs that pass a key in the URL (Stripe doesn't, but the floor is cheap).
_vcr = vcr.VCR(
    cassette_library_dir=str(CASSETTES),
    record_mode="none" if not os.environ.get("RC_CONN_STRIPE") else "once",
    filter_headers=[("Authorization", "DUMMY")],
    filter_query_parameters=["api_key", "access_token"],
    match_on=["method", "scheme", "host", "port", "path", "query"],
)

# Any real Stripe secret/key prefix that must never appear in a committed cassette.
_TOKEN_PREFIXES = ("sk_live_", "sk_test_", "rk_live_", "rk_test_", "Bearer sk_", "Bearer rk_")


class StripeConnectorReplay(unittest.TestCase):
    def setUp(self):
        # Replay needs SOME credential set so lib.api builds a client; the cassette's auth is scrubbed
        # anyway, so a dummy is correct for replay and harmless for record (real key from env wins).
        self._saved = os.environ.get("RC_CONN_STRIPE")
        if not self._saved:
            os.environ["RC_CONN_STRIPE"] = "rk_test_dummy_for_replay"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_STRIPE", None)

    def test_customer_by_id_summary(self):
        with _vcr.use_cassette("stripe_customer_by_id.yaml"):
            s = stripe_conn.support_summary("cus_NffrFeUfNV2Hib")
        self.assertTrue(s["found"])
        self.assertEqual(s["customer"]["email"], "jenny.rosen@example.com")
        self.assertEqual(s["subscription"]["status"], "active")
        self.assertEqual(s["subscription"]["plan.nickname"], "Standard Monthly")
        self.assertEqual(s["latest_invoice"]["status"], "open")
        self.assertEqual(s["last_failed_charge"]["failure_code"], "card_declined")

    def test_customer_by_id_markdown(self):
        with _vcr.use_cassette("stripe_customer_by_id.yaml"):
            md = stripe_conn.summary_to_markdown(stripe_conn.support_summary("cus_NffrFeUfNV2Hib"))
        self.assertIn("# Stripe: jenny.rosen@example.com", md)
        self.assertIn("Status: **active**", md)
        self.assertIn("Standard Monthly (15.00 USD/month)", md)
        self.assertIn("## Latest invoice", md)
        self.assertIn("AB12345-0001: **open**", md)
        self.assertIn("## Last failed payment", md)
        self.assertIn("Your card was declined.", md)

    def test_customer_by_email_resolves_and_renders_sparse(self):
        with _vcr.use_cassette("stripe_customer_by_email.yaml"):
            md = stripe_conn.summary_to_markdown(stripe_conn.support_summary("jenny.rosen@example.com"))
        self.assertIn("# Stripe: jenny.rosen@example.com", md)
        self.assertIn("(no subscription)", md)
        self.assertIn("(no invoices)", md)


class CassetteSecretHygiene(unittest.TestCase):
    """CI guard: no real token prefix may ever land in a committed cassette (scrub-at-record proof)."""

    def test_no_token_prefixes_in_cassettes(self):
        offenders = []
        for path in CASSETTES.glob("*.yaml"):
            text = path.read_text(encoding="utf-8")
            for pref in _TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref}")
        self.assertEqual(offenders, [], f"token-like material leaked into cassettes: {offenders}")

    def test_money_helper(self):
        self.assertEqual(stripe_conn._money(1500, "usd"), "15.00 USD")
        self.assertEqual(stripe_conn._money(None, "usd"), "None")


if __name__ == "__main__":
    unittest.main()
