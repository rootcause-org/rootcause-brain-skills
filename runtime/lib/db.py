"""Read-only Postgres access for grounding.

A project usually has SEVERAL databases — Momentum Tools has powertools / ruby / elsa — each
injected as its own ``*_DSN`` env var. Pick one with the ``db`` argument, which accepts a short
name (``"powertools"``), the exact env-var name (``"MOMENTUM_POWERTOOLS_DSN"``), or a raw DSN.
With a single database configured (or ``PG_DSN`` set) ``db`` may be omitted. ``databases()``
lists what this run has.

Read-only by provisioning; this module adds a belt-and-suspenders ``READ ONLY``
transaction plus a ``statement_timeout`` so a stray write fails loudly and a runaway query can't
hang the run. ``psycopg`` is imported lazily, so the module — its DSN resolution, and the CLI's
``--list`` — loads even where the driver isn't installed.

CLI (token-efficient one-offs from bash):

    python -m lib.db --list
    python -m lib.db --db powertools "select count(*) from accounts"
    python -m lib.db --format table "select id, email from accounts limit 20"
"""

import os

DEFAULT_TIMEOUT_MS = 30_000

# The host's own operational store (registry + River + audit log) — never a grounding target.
# Excluded from discovery so the agent can't accidentally pick it.
_HOST_DSN_VARS = ("DATABASE_URL",)

# Cache of array-type OIDs per resolved DSN (see `_array_oids`). Process-local; OIDs are stable
# for a database, and a run's container is disposable, so a plain dict is enough.
_ARRAY_OIDS: dict[str, frozenset] = {}


def _parse_pg_array(text: str):
    """Parse a Postgres array output literal into a (possibly nested) Python list.

    psycopg parses arrays of KNOWN element types (``text[]``, ``int[]``, …) into lists itself; it
    leaves arrays of element types it has no loader for — chiefly **enum arrays** and other
    user-defined types — as the **raw literal string** (``"{parent}"``, ``"{parent,child}"``,
    ``"{}"``). Iterating that string by mistake (``list(role)`` → characters) is a silent footgun,
    so `query` routes such values through here.

    Handles the array grammar: quoted elements with backslash escapes (``{"a,b","x\\"y"}``),
    unquoted ``NULL`` → ``None`` (a quoted ``"NULL"`` stays the string), empty ``{}`` → ``[]``, and
    nesting (``{{1,2},{3}}``). Elements come back as ``str`` (the caller casts as needed). A value
    that isn't an array literal is returned unchanged.
    """
    s = text.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return text
    i = 0
    n = len(s)

    def parse_array():
        nonlocal i
        i += 1  # consume '{'
        out: list = []
        if i < n and s[i] == "}":
            i += 1
            return out
        while i < n:
            if s[i] == "{":
                out.append(parse_array())
            elif s[i] == '"':
                out.append(parse_quoted())
            else:
                out.append(parse_unquoted())
            if i < n and s[i] == ",":
                i += 1
                continue
            if i < n and s[i] == "}":
                i += 1
            break
        return out

    def parse_quoted():
        nonlocal i
        i += 1  # consume opening '"'
        buf: list = []
        while i < n:
            c = s[i]
            if c == "\\" and i + 1 < n:
                buf.append(s[i + 1])
                i += 2
            elif c == '"':
                i += 1
                break
            else:
                buf.append(c)
                i += 1
        return "".join(buf)

    def parse_unquoted():
        nonlocal i
        start = i
        while i < n and s[i] not in ",}":
            i += 1
        tok = s[start:i]
        return None if tok == "NULL" else tok

    return parse_array()


def _array_oids(conn, dsn: str) -> frozenset:
    """OIDs of all array types in this database (``pg_type.typcategory = 'A'``), cached per DSN.

    Lets `query` tell that a value psycopg returned as a *string* actually came from an array
    column (an unhandled element type, e.g. an enum array) and should be parsed — without touching
    real text columns that merely contain braces."""
    oids = _ARRAY_OIDS.get(dsn)
    if oids is None:
        with conn.cursor() as cur:
            cur.execute("SELECT oid FROM pg_type WHERE typcategory = 'A'")
            oids = frozenset(r[0] for r in cur.fetchall())
        _ARRAY_OIDS[dsn] = oids
    return oids


def databases() -> list[str]:
    """Names of the project DSN env vars available this run (``*_DSN``, host store excluded)."""
    return sorted(
        k for k, v in os.environ.items() if k.endswith("_DSN") and v and k not in _HOST_DSN_VARS
    )


def _resolve_dsn(db: str | None) -> str:
    """Resolve ``db`` to a DSN: raw DSN → exact env name → exact short name → substring fallback.

    Resolution prefers EXACT matches and only falls back to a substring match (with a warning) so a
    short name can't silently bind the wrong database: ``db="elsa"`` must not quietly resolve to
    ``MOMENTUM_ELSA_REPLICA_DSN`` when an exact ``elsa`` database exists. An ambiguous match (>1
    candidate at any tier) raises rather than guessing.
    """
    if db and "://" in db:
        return db
    if db:
        if os.environ.get(db):  # exact env-var name
            return os.environ[db]
        key = db.upper().replace("-", "_")
        avail = databases()

        # Exact short name: match the env var's trailing segment exactly — "powertools" →
        # MOMENTUM_POWERTOOLS_DSN, "elsa" → MOMENTUM_ELSA_DSN (NOT MOMENTUM_ELSA_REPLICA_DSN, whose
        # trailing segment is "replica"). This is the intended path and wins over any substring, so a
        # short name can't silently bind a longer, differently-named database.
        def _trailing(c: str) -> str:
            return c[: -len("_DSN")].rsplit("_", 1)[-1]  # MOMENTUM_POWERTOOLS_DSN → POWERTOOLS

        named = [c for c in avail if c == key or c == f"{key}_DSN" or _trailing(c) == key]
        if len(named) == 1:
            return os.environ[named[0]]
        if len(named) > 1:
            raise RuntimeError(f"db={db!r} is ambiguous (matches {named}); use an exact name from databases()")
        # Substring fallback — convenient but lossy, so warn: it can bind a name the caller didn't
        # mean (e.g. "elsa" → MOMENTUM_ELSA_REPLICA_DSN). Ambiguity here still raises.
        sub = [c for c in avail if key in c]
        if len(sub) == 1:
            import warnings

            warnings.warn(
                f"db={db!r} matched {sub[0]} by substring (no exact name matched); "
                f"pass an exact name from databases() to be unambiguous",
                stacklevel=2,
            )
            return os.environ[sub[0]]
        if len(sub) > 1:
            raise RuntimeError(f"db={db!r} is ambiguous (matches {sub}); use an exact name from databases()")
        raise RuntimeError(f"no database matches db={db!r}; available: {avail or 'none'}")
    if os.environ.get("PG_DSN"):
        return os.environ["PG_DSN"]
    avail = databases()
    if len(avail) == 1:
        return os.environ[avail[0]]
    if not avail:
        raise RuntimeError("no project database configured for this run (no *_DSN env var set)")
    raise RuntimeError(f"multiple databases available {avail}; pass db=... to pick one")


def query(
    sql: str,
    params: list | tuple | None = None,
    db: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> list[dict]:
    """Run a read-only SELECT and return rows as a list of dicts.

    Opens a fresh read-only connection per call (the container is disposable, so pooling buys
    nothing). Use ``%s`` placeholders with ``params`` — never string-format input into ``sql``.
    ``db`` selects the database (see module docstring); ``timeout_ms`` caps the statement.
    """
    import psycopg

    dsn = _resolve_dsn(db)
    with psycopg.connect(dsn, autocommit=False) as conn:
        # Read-only transaction: a write attempt errors instead of mutating customer data.
        conn.read_only = True
        with conn.cursor() as cur:
            if timeout_ms:
                cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            cur.execute(sql, params or [])
            if cur.description is None:
                return []
            cols = cur.description
            raw = cur.fetchall()
        # Enum/other unhandled array columns come back from psycopg as the raw literal string
        # ("{parent}"); parse those into lists so callers get a real list everywhere. Built-in
        # arrays already arrive as lists (not str) and so are untouched; the brace + array-OID
        # guards keep us off ordinary text columns. OID set is fetched once per DSN (cached).
        array_oids = _array_oids(conn, dsn) if raw else frozenset()
        out: list[dict] = []
        for row in raw:
            d = {}
            for col, val in zip(cols, row):
                if (
                    isinstance(val, str)
                    and val[:1] == "{"
                    and val[-1:] == "}"
                    and col.type_code in array_oids
                ):
                    val = _parse_pg_array(val)
                d[col.name] = val
            out.append(d)
        return out


def query_one(sql: str, params: list | tuple | None = None, db: str | None = None) -> dict | None:
    """Run a read-only SELECT and return the first row (or None)."""
    rows = query(sql, params, db=db)
    return rows[0] if rows else None


def columns(table: str, schema: str | None = None, db: str | None = None) -> list[dict]:
    """Column names + types for one table — schema introspection when the layout is unknown.

    ``schema=None`` (default) introspects the run's EFFECTIVE schema via ``current_schema()`` — the
    same resolution an unqualified table reference uses. On a tenant-scoped run that is the per-run
    ``scope_<id>`` schema of projected views (``public`` is revoked, so a hard-coded ``"public"``
    would see nothing); on a flat project it resolves to ``public`` exactly as before. Pass an
    explicit ``schema`` to override.
    """
    return query(
        "select column_name, data_type from information_schema.columns "
        "where table_schema = coalesce(%s::text, current_schema()) and table_name = %s "
        "order by ordinal_position",
        [schema, table],
        db=db,
    )


def tables_with_column(name_like: str, schema: str | None = None, db: str | None = None) -> list[dict]:
    """Find (table, column) pairs whose column name matches an ILIKE pattern, e.g. ``%email%``.

    The entry point for locating where data lives (an account email, a usage column) when the
    schema isn't pinned down — discover the identifier here, never take it from the ticket.
    ``schema=None`` (default) searches the run's EFFECTIVE schema (``current_schema()``) — the
    ``scope_<id>`` views on a scoped run, ``public`` on a flat project (see `columns`).
    """
    return query(
        "select table_name, column_name, data_type from information_schema.columns "
        "where table_schema = coalesce(%s::text, current_schema()) and column_name ilike %s "
        "order by table_name, column_name",
        [schema, name_like],
        db=db,
    )


def _parse_duration_ms(s: str) -> int:
    """Parse a duration like ``30s`` / ``2min`` / ``500ms`` / ``1m`` into milliseconds."""
    s = s.strip().lower()
    for suffix, mult in (("ms", 1), ("min", 60_000), ("s", 1000), ("m", 60_000), ("h", 3_600_000)):
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(float(s) * 1000)  # bare number = seconds


def _main(argv=None) -> int:
    import argparse

    from . import _output

    p = argparse.ArgumentParser(prog="python -m lib.db", description=__doc__.split("\n")[0])
    p.add_argument("sql", nargs="?", help="SQL to run (read-only transaction).")
    p.add_argument("--db", help="Database: short name, env-var name, or raw DSN. Omit if only one.")
    p.add_argument("--format", choices=("csv", "json", "table"), default="csv")
    p.add_argument("--timeout", default="30s", help="statement_timeout, e.g. 30s, 2min (default 30s).")
    p.add_argument("--list", action="store_true", help="List available databases and exit.")
    args = p.parse_args(argv)

    if args.list:
        for name in databases():
            print(name)
        return 0
    if not args.sql:
        p.error("provide SQL, or --list")
    rows = query(args.sql, db=args.db, timeout_ms=_parse_duration_ms(args.timeout))
    _output.emit(_output.render(rows, args.format), label="db")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
