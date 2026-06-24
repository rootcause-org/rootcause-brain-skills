"""Pure-logic tests for the grounding lib — DSN resolution, rendering, time windows, Insights
polling. No real DB/AWS: psycopg/boto3 are imported lazily, so these run on a bare host with

    cd runtime && uv run --no-project python -m unittest discover -s tests

The live paths (real query / real Insights call) are covered by the host's Go workspace smoke
tests; here we lock down the logic that decides *what* gets run.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import _output, cloudwatch, db  # noqa: E402
from lib import stripe as lib_stripe  # noqa: E402


class DSNResolution(unittest.TestCase):
    def setUp(self):
        self._clean = {
            k: v for k, v in os.environ.items() if k.endswith("_DSN") or k in ("PG_DSN", "DATABASE_URL")
        }
        for k in self._clean:
            del os.environ[k]

    def tearDown(self):
        for k in list(os.environ):
            if k.endswith("_DSN") or k in ("PG_DSN", "DATABASE_URL"):
                del os.environ[k]
        os.environ.update(self._clean)

    def test_databases_excludes_host_store(self):
        os.environ["DATABASE_URL"] = "postgres://host/ops"
        os.environ["MOMENTUM_POWERTOOLS_DSN"] = "postgres://pt"
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        self.assertEqual(db.databases(), ["MOMENTUM_POWERTOOLS_DSN", "MOMENTUM_RUBY_DSN"])

    def test_raw_dsn_passthrough(self):
        self.assertEqual(db._resolve_dsn("postgres://x/y"), "postgres://x/y")

    def test_exact_env_name(self):
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        self.assertEqual(db._resolve_dsn("MOMENTUM_RUBY_DSN"), "postgres://ruby")

    def test_short_name_suffix_match(self):
        os.environ["MOMENTUM_POWERTOOLS_DSN"] = "postgres://pt"
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        self.assertEqual(db._resolve_dsn("powertools"), "postgres://pt")
        self.assertEqual(db._resolve_dsn("ruby"), "postgres://ruby")

    def test_ambiguous_short_name_raises(self):
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://a"
        os.environ["OTHER_RUBY_DSN"] = "postgres://b"
        with self.assertRaises(RuntimeError):
            db._resolve_dsn("ruby")

    def test_exact_short_name_wins_over_substring(self):
        # "elsa" must bind the EXACT elsa database, never the longer ELSA_REPLICA by substring.
        os.environ["MOMENTUM_ELSA_DSN"] = "postgres://elsa"
        os.environ["MOMENTUM_ELSA_REPLICA_DSN"] = "postgres://replica"
        self.assertEqual(db._resolve_dsn("elsa"), "postgres://elsa")

    def test_substring_fallback_warns(self):
        # No exact match for "elsa"; only a substring (ELSA_REPLICA) — resolve but warn loudly so a
        # silently-wrong DB binding can't happen unnoticed.
        os.environ["MOMENTUM_ELSA_REPLICA_DSN"] = "postgres://replica"
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertEqual(db._resolve_dsn("elsa"), "postgres://replica")
        self.assertTrue(any("substring" in str(w.message) for w in caught))

    def test_ambiguous_substring_raises(self):
        # "elsa" substring-matches two DSNs and no exact name — raise rather than guess.
        os.environ["MOMENTUM_ELSA_REPLICA_DSN"] = "postgres://a"
        os.environ["OTHER_ELSA_SHARD_DSN"] = "postgres://b"
        with self.assertRaises(RuntimeError):
            db._resolve_dsn("elsa")

    def test_unknown_name_raises(self):
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        with self.assertRaises(RuntimeError):
            db._resolve_dsn("elsa")

    def test_default_pg_dsn(self):
        os.environ["PG_DSN"] = "postgres://default"
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        self.assertEqual(db._resolve_dsn(None), "postgres://default")

    def test_default_single_database(self):
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        self.assertEqual(db._resolve_dsn(None), "postgres://ruby")

    def test_default_none_configured_raises(self):
        with self.assertRaises(RuntimeError):
            db._resolve_dsn(None)

    def test_default_multiple_requires_pick(self):
        os.environ["A_DSN"] = "postgres://a"
        os.environ["B_DSN"] = "postgres://b"
        with self.assertRaises(RuntimeError):
            db._resolve_dsn(None)


class Introspection(unittest.TestCase):
    """columns / tables_with_column build the right information_schema query and pass db= through."""

    def test_columns_delegates(self):
        with mock.patch.object(db, "query", return_value=[]) as q:
            db.columns("users", db="ruby")
        sql, params = q.call_args.args[0], q.call_args.args[1]
        self.assertIn("information_schema.columns", sql)
        self.assertIn("table_name = %s", sql)
        # Default schema is NULL → coalesced to current_schema() so a scoped run sees its scope_<id>
        # views (public is revoked there); a flat run still resolves to public.
        self.assertIn("current_schema()", sql)
        self.assertEqual(params, [None, "users"])
        self.assertEqual(q.call_args.kwargs["db"], "ruby")

    def test_columns_explicit_schema_overrides(self):
        with mock.patch.object(db, "query", return_value=[]) as q:
            db.columns("users", schema="public", db="ruby")
        self.assertEqual(q.call_args.args[1], ["public", "users"])

    def test_tables_with_column_delegates(self):
        with mock.patch.object(db, "query", return_value=[]) as q:
            db.tables_with_column("%email%", schema="app", db="powertools")
        sql, params = q.call_args.args[0], q.call_args.args[1]
        self.assertIn("column_name ilike %s", sql)
        self.assertEqual(params, ["app", "%email%"])
        self.assertEqual(q.call_args.kwargs["db"], "powertools")

    def test_tables_with_column_default_schema_is_current(self):
        with mock.patch.object(db, "query", return_value=[]) as q:
            db.tables_with_column("%email%", db="powertools")
        sql, params = q.call_args.args[0], q.call_args.args[1]
        self.assertIn("current_schema()", sql)
        self.assertEqual(params, [None, "%email%"])


class DurationParsing(unittest.TestCase):
    def test_units(self):
        self.assertEqual(db._parse_duration_ms("500ms"), 500)
        self.assertEqual(db._parse_duration_ms("30s"), 30_000)
        self.assertEqual(db._parse_duration_ms("2min"), 120_000)
        self.assertEqual(db._parse_duration_ms("1m"), 60_000)
        self.assertEqual(db._parse_duration_ms("1h"), 3_600_000)
        self.assertEqual(db._parse_duration_ms("5"), 5_000)  # bare = seconds


class Rendering(unittest.TestCase):
    rows = [{"id": 1, "email": "a@b.com", "meta": {"k": "v"}}, {"id": 2, "email": "c@d.com", "meta": None}]

    def test_csv(self):
        out = _output.render(self.rows, "csv")
        self.assertIn("id,email,meta", out)
        self.assertIn('"{""k"": ""v""}"', out)  # JSON-encoded dict cell, CSV-quoted

    def test_json_roundtrip(self):
        import json

        self.assertEqual(json.loads(_output.render(self.rows, "json")), self.rows)

    def test_table_has_header_and_separator(self):
        out = _output.render(self.rows, "table").splitlines()
        self.assertTrue(out[0].startswith("id"))
        self.assertIn("--", out[1])

    def test_empty(self):
        self.assertEqual(_output.render([], "csv"), "")

    def test_spill(self):
        with mock.patch.object(_output, "SPILL_BYTES", 10):
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                _output.emit("x" * 100, label="t")
            self.assertIn("spilled to", buf.getvalue())


class CloudWatchTimeRange(unittest.TestCase):
    def test_hours_lookback(self):
        with mock.patch.object(cloudwatch.time, "time", return_value=1_000_000):
            s, e = cloudwatch._time_range(hours=1, start=None, end=None)
            self.assertEqual(e, 1_000_000)
            self.assertEqual(s, 1_000_000 - 3600)

    def test_explicit_iso_window(self):
        s, e = cloudwatch._time_range(hours=24, start="2026-01-10 00:00:00", end="2026-01-10 01:00:00")
        self.assertEqual(e - s, 3600)


class InsightsPolling(unittest.TestCase):
    def _fake_client(self, statuses, results):
        c = mock.Mock()
        c.start_query.return_value = {"queryId": "q1"}
        c.get_query_results.side_effect = [{"status": st, "results": results} for st in statuses]
        return c

    def test_polls_until_complete_and_drops_ptr(self):
        rows = [[{"field": "@message", "value": "hi"}, {"field": "@ptr", "value": "xyz"}]]
        client = self._fake_client(["Running", "Complete"], rows)
        with mock.patch.object(cloudwatch, "_client", return_value=client), mock.patch.object(
            cloudwatch.time, "sleep"
        ), mock.patch.object(cloudwatch.time, "time", return_value=0):
            out = cloudwatch.insights("fields @message", "/app", hours=1)
        self.assertEqual(out, [{"@message": "hi"}])

    def test_failed_status_raises(self):
        client = self._fake_client(["Failed"], [])
        with mock.patch.object(cloudwatch, "_client", return_value=client), mock.patch.object(
            cloudwatch.time, "sleep"
        ):
            with self.assertRaises(RuntimeError):
                cloudwatch.insights("q", "/app")


class CloudWatchPatternEscaping(unittest.TestCase):
    """search() must not let attacker-influenceable text break out of the /.../ regex literal."""

    def test_slash_is_escaped(self):
        # A bare "/" would close the literal early; it must be backslash-escaped.
        self.assertEqual(cloudwatch._escape_pattern("a/b"), "a\\/b")

    def test_backslash_is_escaped_first(self):
        # The escape char itself is doubled, and that happens before slashes so we don't double-escape.
        self.assertEqual(cloudwatch._escape_pattern("a\\b"), "a\\\\b")
        self.assertEqual(cloudwatch._escape_pattern("a\\/b"), "a\\\\\\/b")

    def test_control_chars_rejected(self):
        for bad in ("line1\nline2", "tab\there", "nul\x00here", "del\x7f"):
            with self.assertRaises(ValueError):
                cloudwatch._escape_pattern(bad)

    def test_search_interpolates_escaped_pattern(self):
        captured = {}

        def fake_insights(q, *a, **k):
            captured["q"] = q
            return []

        with mock.patch.object(cloudwatch, "insights", side_effect=fake_insights):
            cloudwatch.search("/app", "thread/id")
        # The slash from the pattern is escaped inside the filter clause, not a raw literal-closer.
        self.assertIn("filter @message like /thread\\/id/", captured["q"])

    def test_search_rejects_newline_pattern(self):
        with self.assertRaises(ValueError):
            cloudwatch.search("/app", "evil\n| stats count(*)")


class StripeKeyResolution(unittest.TestCase):
    """The lib must resolve the key from EITHER name a project may seal into its .env: the
    documented STRIPE_RESTRICTED_KEY or the in-the-field STRIPE_API_KEY (preferring the former)."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("STRIPE_RESTRICTED_KEY", "STRIPE_API_KEY")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _resolve_key(self):
        """Run _client() with a stubbed SDK and return the api_key it resolved."""
        fake_sdk = mock.Mock()
        with mock.patch.dict(sys.modules, {"stripe": fake_sdk}):
            sdk = lib_stripe._client()
        self.assertIs(sdk, fake_sdk)
        return fake_sdk.api_key

    def test_missing_key_raises_naming_both_vars(self):
        with self.assertRaises(RuntimeError) as cm:
            lib_stripe._client()
        msg = str(cm.exception)
        self.assertIn("STRIPE_RESTRICTED_KEY", msg)
        self.assertIn("STRIPE_API_KEY", msg)

    def test_restricted_key_initializes_client(self):
        # The documented onboarding name resolves.
        os.environ["STRIPE_RESTRICTED_KEY"] = "rk_test_dummy"
        self.assertEqual(self._resolve_key(), "rk_test_dummy")

    def test_api_key_fallback_initializes_client(self):
        # The in-the-field name (what momentum-tools seals) also resolves — this is the P2 fix.
        os.environ["STRIPE_API_KEY"] = "rk_live_field"
        self.assertEqual(self._resolve_key(), "rk_live_field")

    def test_restricted_key_wins_when_both_present(self):
        # When both are sealed, the documented standard takes precedence (stable, predictable).
        os.environ["STRIPE_RESTRICTED_KEY"] = "rk_restricted"
        os.environ["STRIPE_API_KEY"] = "rk_api"
        self.assertEqual(self._resolve_key(), "rk_restricted")


class PgArrayParsing(unittest.TestCase):
    """`_parse_pg_array` turns the raw literal psycopg leaves for unhandled (enum) array columns
    into a real list — the fix for `list("{parent}")` mangling roles into single characters."""

    def test_array_literals(self):
        cases = [
            ("{}", []),
            ("{parent}", ["parent"]),
            ("{parent,child}", ["parent", "child"]),
            ('{"parent"}', ["parent"]),
            ('{"a,b","x\\"y"}', ["a,b", 'x"y']),  # quoted comma + escaped quote
            ("{NULL,foo}", [None, "foo"]),  # unquoted NULL -> None
            ('{"NULL"}', ["NULL"]),  # quoted NULL stays a string
            ("{{1,2},{3,4}}", [["1", "2"], ["3", "4"]]),  # nested
        ]
        for literal, expected in cases:
            self.assertEqual(db._parse_pg_array(literal), expected, msg=literal)

    def test_non_array_returned_unchanged(self):
        # A real text value that merely contains braces is never an array literal here (the caller
        # also gates on the column's array OID), and round-trips untouched if passed in.
        self.assertEqual(db._parse_pg_array("not an array"), "not an array")


if __name__ == "__main__":
    unittest.main()
