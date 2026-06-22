"""Live grounding-test kit + pytest plugin — verify a brain's grounding scripts against real,
read-only prod, cheaply and without flakiness.

A brain's `*_to_md` dumpers / lookups are tested offline with fake rows (L1): that validates *logic*
but never touches reality, so two failure classes ship undetected and only surface on a live run —
**schema drift** (a SELECT names a renamed/typo'd column) and **data-shape surprises** (enum array
returned as the raw `{a,b}` literal, integer-cents vs decimal-euros, unexpected NULLs). This kit adds
two live tiers that catch them, disjoint from L1:

- **L2 schema canary** — call each fetcher with `FAKE_UUID` (matches nothing). Postgres still parses +
  plans the query, so a bad column raises `UndefinedColumn` with **zero fixture data needed**.
- **L3 render-smoke** — run each dumper over a live `LIMIT k` sample; assert it renders without
  raising, starts with the right `# H1`, and contains **no error sentinel** (the dumpers wrap sections
  in try/except → `"… unavailable: {e}"`, which would otherwise hide a broken section from a coarse
  "did it raise?" check).

Four principles keep L2/L3 high-signal *and* low-maintenance: invariants never values (tolerates data
churn); dynamic subjects (each run finds its own fresh rows — **never hardcode a UUID**); sentinel-grep
is mandatory (else L3 tests nothing); skip-with-reason never silent (no DSN / no live rows ⇒ a *skip*
with a printed reason, never a fail; `--require-live` turns skips into errors for gated runs).

A brain enrols a module by exporting a module-level `LIVE_CHECK = LiveCheck(...)` manifest; the generic
`tests/test_live.py` parametrizes over `all_live_checks(SCRIPTS_DIR)`, so a new dumper self-enrols with
no test-file edit. Enable the plugin from the brain's top-level conftest: `pytest_plugins =
["lib.livecheck"]` (or rely on `scripts/brain_test.py`, which loads it with `-p lib.livecheck`).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

# Error-sentinel substrings a dumper emits when a section's try/except swallows a failure. L3 fails
# when the rendered output contains any of these — resilience that's correct for prod but would
# otherwise hide a broken section. Brains may pass their own via LiveCheck(sentinels=...).
SENTINELS: tuple[str, ...] = ("unavailable:", "dump failed")

# The sentinel id for the schema canary: a well-formed UUID that matches no row, so a fetcher's SELECT
# is parsed + planned (catching a bad column) while returning nothing — no fixture data needed.
FAKE_UUID = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class LiveCheck:
    """The declarative manifest a grounding module exports as `LIVE_CHECK`.

    `render` and `column_probes` close over the module's **own** fetchers — no query duplication.
    Fields with no default come first (dataclass rule); brains pass everything by keyword anyway.
    """

    model: str                                    # display id, e.g. "Leader"
    render: Callable[[dict], str]                 # subject row -> the markdown dump
    subjects_sql: str                             # SELECT id, tenant_id FROM <table> WHERE deleted_at
                                                  #   IS NULL ORDER BY created_at DESC LIMIT %(k)s
    db: str = "prod"                              # lib.db short name (which DSN)
    column_probes: Callable[[object], list[tuple[str, Callable[[], object]]]] | None = None
                                                  # tenant_id -> [(label, thunk)]; each thunk runs one
                                                  #   fetcher's SELECT with FAKE_UUID. None = no canary
                                                  #   (e.g. a SELECT * dump) → schema test skips.
    expect_prefix: str = "# "
    sentinels: tuple[str, ...] = SENTINELS
    k: int = 5
    # L3b extension point (targeted property checks: money ranges, enum-token sets, tenant-scope never
    # leaks). Declared so it's a stable seam; NOT run in v1 — grow it from real incidents.
    invariants: tuple[Callable[[dict], None], ...] = ()


def pick_tenant(lc: LiveCheck) -> object | None:
    """A real tenant id for the schema canary, sourced from the check's *own* `subjects_sql` (LIMIT 1).

    Generic by construction — `subjects_sql` already yields `tenant_id`, so no brain needs to name its
    tenant table here. Returns the row's `tenant_id` (correctly typed — avoids a uuid-vs-int cast
    false-positive when probing), or None when the table is empty (the canary then falls back to
    FAKE_UUID, which still validates every column since Postgres plans the query regardless of value).

    `RC_LIVE_TENANT` (set by `brain_test.py --tenant`) overrides the auto-pick to pin one tenant — a
    numeric value is coerced to int so an int `tenant_id` column stays well-typed; anything else passes
    through verbatim (e.g. a uuid string).
    """
    override = os.environ.get("RC_LIVE_TENANT")
    if override:
        return int(override) if override.lstrip("-").isdigit() else override

    from lib import db

    rows = db.query(lc.subjects_sql, {"k": 1}, db=lc.db)
    return rows[0].get("tenant_id") if rows else None


def all_live_checks(scripts_dir: str | Path) -> list[LiveCheck]:
    """Import every `*.py` in `scripts_dir` and collect each module-level `LIVE_CHECK`.

    The auto-enrolment seam: a new dumper that sets `LIVE_CHECK` is picked up with zero test-file
    edits. Inserts `scripts_dir` on `sys.path` so the modules and their siblings (`from ka import …`)
    resolve; `lib` comes from `runtime` already on PYTHONPATH. Dunder files are skipped; a helper
    module without a `LIVE_CHECK` is simply ignored.

    Each module is loaded under a name keyed by its absolute path, NOT its bare stem — so two skills
    that both ship a `dumper.py` don't collide in `sys.modules` (a bare-stem `import_module` would
    return the first-cached module for the second skill, silently testing the wrong code or dropping
    a skill's checks). Siblings still import by bare name via the `sys.path` entry above.
    """
    scripts_dir = Path(scripts_dir).resolve()
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    checks: list[LiveCheck] = []
    for py in sorted(scripts_dir.glob("*.py")):
        if py.name.startswith("__"):
            continue
        # Path-unique module name avoids cross-skill stem collisions in sys.modules.
        mod_name = f"_livecheck_{py.resolve().as_posix().replace('/', '_').replace('.', '_')}"
        spec = importlib.util.spec_from_file_location(mod_name, py)
        if spec is None or spec.loader is None:  # pragma: no cover - unreadable/odd file
            continue
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        lc = getattr(mod, "LIVE_CHECK", None)
        if isinstance(lc, LiveCheck):
            checks.append(lc)
    return checks


def assert_schema(lc: LiveCheck) -> None:
    """L2: every fetcher's SELECT is schema-valid against live prod (run with FAKE_UUID → 0 rows).

    Skips when the check declares no `column_probes` (e.g. a `SELECT *` dump has no columns to canary).
    Fails on the first probe that raises, surfacing the offending fetcher + DB error (the column name).
    """
    if lc.column_probes is None:
        pytest.skip(f"{lc.model}: no column_probes (SELECT * dump — nothing to canary)")
    # A real tenant keeps tenant-scoped probes well-typed; FAKE_UUID is fine when the table is empty
    # (schema validation doesn't depend on the value matching).
    tenant = pick_tenant(lc)
    if tenant is None:
        tenant = FAKE_UUID
    for label, thunk in lc.column_probes(tenant):
        try:
            thunk()
        except Exception as e:  # noqa: BLE001 — any DB/coding error is a real schema-canary failure
            raise AssertionError(
                f"{lc.model}: schema probe {label!r} raised {type(e).__name__}: {e}"
            ) from e


def assert_render_smoke(lc: LiveCheck) -> None:
    """L3: each dumper renders a live `LIMIT k` sample without raising, with the right `# H1`, no
    error sentinel.

    0 live subjects ⇒ skip with a reason (never a fail — the table may legitimately be empty).
    """
    from lib import db

    rows = db.query(lc.subjects_sql, {"k": lc.k}, db=lc.db)
    if not rows:
        pytest.skip(f"{lc.model}: no live subjects ({lc.subjects_sql.split('FROM', 1)[-1].strip()[:60]})")
    for row in rows:
        out = lc.render(row)
        assert isinstance(out, str) and out, f"{lc.model}: render returned empty output"
        assert out.startswith(lc.expect_prefix), (
            f"{lc.model}: render does not start with {lc.expect_prefix!r}: {out[:80]!r}"
        )
        for s in lc.sentinels:
            assert s not in out, (
                f"{lc.model}: error sentinel {s!r} in render output:\n  …{_excerpt(out, s)}…"
            )


def _excerpt(text: str, needle: str, width: int = 60) -> str:
    """A short window of `text` around the first `needle`, for a legible assertion message."""
    i = text.find(needle)
    start = max(0, i - width // 2)
    return text[start : i + len(needle) + width // 2].replace("\n", " ")


# ---- pytest plugin -----------------------------------------------------------------------------
#
# Enabled by a brain via `pytest_plugins = ["lib.livecheck"]` (top-level conftest) or by
# scripts/brain_test.py via `-p lib.livecheck`. Registers the `live` marker + `--require-live`,
# probes DSN reachability once and skip-marks live items when unreachable (skip-with-reason, never a
# silent green), and — under `--require-live` — fails the run if no live test actually ran.

# Process-local counter of live tests that genuinely ran (passed/failed, not skipped). A plain global
# is enough: one pytest session per process, no xdist here.
_LIVE_RAN = 0
# Count of collected live items, stashed on the config for the --require-live check at session end.
_LIVE_TOTAL_KEY = pytest.StashKey[int]()


def pytest_configure(config: pytest.Config) -> None:
    global _LIVE_RAN
    _LIVE_RAN = 0
    config.addinivalue_line(
        "markers", "live: read-only test against a real project DSN (opt-in; skipped without one)"
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--require-live",
        action="store_true",
        default=False,
        help="fail if no live test ran (no reachable DSN, or no LIVE_CHECK collected) — for gated/cron runs",
    )


def _item_lc(item: pytest.Item) -> LiveCheck | None:
    cs = getattr(item, "callspec", None)
    return cs.params.get("lc") if cs else None


def _probe(dbname: str) -> tuple[bool, str]:
    """Once-per-DSN reachability check: can we run `SELECT 1` against this `db`? Returns (ok, reason)."""
    try:
        from lib import db
    except Exception as e:  # noqa: BLE001
        return False, f"lib.db import failed: {e}"
    try:
        db.query("SELECT 1", db=dbname)
        return True, ""
    except Exception as e:  # noqa: BLE001 — no DSN / unreachable host / missing driver all land here
        return False, f"{type(e).__name__}: {e}"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    live_items = [it for it in items if it.get_closest_marker("live")]
    config.stash[_LIVE_TOTAL_KEY] = len(live_items)
    if not live_items:
        return
    # Probe each distinct DSN once, reusing the parametrized LiveCheck objects to know which dbs to hit.
    probed: dict[str, tuple[bool, str]] = {}
    for it in live_items:
        lc = _item_lc(it)
        dbname = lc.db if lc else None
        if dbname and dbname not in probed:
            probed[dbname] = _probe(dbname)
    for it in live_items:
        lc = _item_lc(it)
        dbname = lc.db if lc else None
        res = probed.get(dbname or "")
        if res and not res[0]:
            it.add_marker(pytest.mark.skip(reason=f"no live DSN for db={dbname!r} — {res[1]}"))


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    global _LIVE_RAN
    if report.when == "call" and "live" in report.keywords and not report.skipped:
        _LIVE_RAN += 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not session.config.getoption("--require-live"):
        return
    total = session.config.stash.get(_LIVE_TOTAL_KEY, 0)
    if _LIVE_RAN == 0 and (exitstatus in (0, pytest.ExitCode.NO_TESTS_COLLECTED)):
        # Required live coverage but none ran (no reachable DSN, or no LIVE_CHECK collected). Make the
        # gated run fail loudly rather than report a misleading green.
        session.exitstatus = 1
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_sep(
                "!",
                f"--require-live: required live coverage but 0/{total} live tests ran "
                "(no reachable DSN, or no LIVE_CHECK collected)",
                red=True,
            )
