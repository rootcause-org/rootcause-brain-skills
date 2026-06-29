"""Read-only Postgres access for grounding.

A project usually has SEVERAL databases — Momentum Tools has powertools / ruby / elsa — each
injected as its own ``*_DSN`` env var. Pick one with the ``db`` argument, which accepts a short
name (``"powertools"``), the exact env-var name (``"MOMENTUM_POWERTOOLS_DSN"``), or a raw DSN.
With a single database configured (or ``PG_DSN`` set) ``db`` may be omitted. ``databases()``
lists what this run has. ``--list`` (or a bad ``db=``) shows each database's purpose — the
descriptions come from project metadata in the ``RC_DB_DESCRIPTIONS`` env var (a JSON object keyed
by the exact DSN env-var name), so the agent learns which DB is which without trial-and-error.

On a data-scoped project, `query` AUTO-HEALS: if you SELECT a column the project hides (standard
single-table shape), it's dropped, the trimmed query runs, and a warning names what was dropped — so
one extra field doesn't fail the whole query. A column that ISN'T hidden (a typo) still raises, with a
scoping-aware hint. The hidden-column map comes from the ``RC_DB_EXCLUDED_COLUMNS`` env var.

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
import re
import warnings

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


def _short_name(env: str) -> str:
    """Short name for a DSN env var = its trailing segment, lowercased.

    ``MOMENTUM_POWERTOOLS_DSN`` → ``powertools``; ``MOMENTUM_ELSA_REPLICA_DSN`` → ``replica``. The
    single source for both the user-facing listing and `_resolve_dsn`'s exact short-name match
    (which compares case-consistently, uppercased)."""
    return env[: -len("_DSN")].rsplit("_", 1)[-1].lower()


def _descriptions() -> dict[str, str]:
    """Parse ``RC_DB_DESCRIPTIONS`` (JSON: exact DSN env-var name → one-sentence purpose).

    Best-effort metadata, host-filtered to this run's DSNs; absent/blank/malformed → ``{}`` (never
    raise — a bad description must never break a query)."""
    import json

    raw = os.environ.get("RC_DB_DESCRIPTIONS")
    if not raw or not raw.strip():
        return {}
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(val, dict):
        return {}
    return {str(k): str(v) for k, v in val.items()}


def _format_catalog() -> str:
    """Human listing of this run's databases: short name, exact env var, and purpose when known.

    One line each, ``  - <short>  <ENV_VAR> — <description>`` (the ``— …`` omitted when no
    description); ``  (none configured)`` when there are none."""
    avail = databases()
    if not avail:
        return "  (none configured)"
    descs = _descriptions()
    width = max(len(_short_name(c)) for c in avail)
    lines = []
    for env in avail:
        line = f"  - {_short_name(env):<{width}}  {env}"
        desc = descs.get(env)
        if desc:
            line += f" — {desc}"
        lines.append(line)
    return "\n".join(lines)


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
        # short name can't silently bind a longer, differently-named database. `_short_name` lowercases,
        # so compare uppercased to stay consistent with `key`.
        named = [c for c in avail if c == key or c == f"{key}_DSN" or _short_name(c).upper() == key]
        if len(named) == 1:
            return os.environ[named[0]]
        if len(named) > 1:
            raise RuntimeError(
                f"db={db!r} is ambiguous (matches {named}); pick an exact one:\n{_format_catalog()}"
            )
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
            raise RuntimeError(
                f"db={db!r} is ambiguous (matches {sub}); pick an exact one:\n{_format_catalog()}"
            )
        raise RuntimeError(f"unknown db={db!r}. Valid databases:\n{_format_catalog()}")
    if os.environ.get("PG_DSN"):
        return os.environ["PG_DSN"]
    avail = databases()
    if len(avail) == 1:
        return os.environ[avail[0]]
    if not avail:
        raise RuntimeError("no project database configured for this run (no *_DSN env var set)")
    raise RuntimeError(
        "multiple databases available — pass db= to pick one (short name, env var, or raw DSN):\n"
        f"{_format_catalog()}"
    )


def _undefined_hint(exc) -> str:
    """Guidance suffix for an undefined-column/table error — the data-scoping footgun, defused.

    On a scoped run the agent queries the per-run ``scope_<id>`` **views**, so a column (or table)
    the project's data-scoping projected away simply "does not exist" — at the wire level it's
    indistinguishable from a typo, and the bare Postgres error tempts the LLM to rewrite the whole
    query from scratch. Instead, point it at the introspection helper so it drops just the one
    unavailable name and re-runs. Best-effort: prepends Postgres's own HINT when present."""
    parts = []
    diag = getattr(exc, "diag", None)
    pg_hint = getattr(diag, "message_hint", None) if diag is not None else None
    if pg_hint:
        parts.append(pg_hint)
    parts.append(
        "This column/table may be intentionally hidden by this project's data-scoping — you query "
        "projected views, not the base tables, so a hidden column reads as 'does not exist' (NOT "
        "necessarily a typo). Run lib.db.columns('<table>') to list exactly what's queryable, then "
        "drop the unavailable name and re-run — no need to rewrite the whole query."
    )
    return " ".join(parts)


def _excluded_columns() -> dict:
    """Parse ``RC_DB_EXCLUDED_COLUMNS`` (JSON: exact DSN env name → the columns the project's
    data-scoping hides). Shape per env: ``{"global_exclude": [...], "tables": {"<t>": {"exclude":
    [...]}|{"include": [...]}}}``. Host-injected from the scope_manifest. Absent/malformed → ``{}``
    (never raise — auto-heal is best-effort, a query must never break because this is missing)."""
    import json

    raw = os.environ.get("RC_DB_EXCLUDED_COLUMNS")
    if not raw or not raw.strip():
        return {}
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


def _excluded_map_for_db(db: str | None) -> dict:
    """Hidden-column metadata for this db, keyed like RC_DB_EXCLUDED_COLUMNS."""
    excluded = _excluded_columns()
    if not excluded:
        return {}
    dsn = _resolve_dsn(db)
    return excluded.get(_env_name_for_dsn(dsn) or "", {})


def _env_name_for_dsn(dsn: str) -> str | None:
    """The ``*_DSN`` env var whose value is this resolved DSN — the key `RC_DB_EXCLUDED_COLUMNS` uses.
    A raw DSN passed straight to ``db=`` has no env name (→ no heal data, which is fine)."""
    for k, v in os.environ.items():
        if k.endswith("_DSN") and v == dsn:
            return k
    return None


def _pattern_matches(pattern: str, value: str) -> bool:
    """Postgres ILIKE-ish matcher for helper hints (% and _ wildcards only)."""
    rx = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern)
    return re.fullmatch(rx, value, flags=re.IGNORECASE) is not None


def _hidden_column_notes(emap: dict, table: str | None = None, pattern: str | None = None) -> list[str]:
    """Short warnings about manifest-hidden columns; never changes the visible schema result."""
    if not emap:
        return []
    notes = []
    globals_ = sorted(str(c) for c in (emap.get("global_exclude") or []) if isinstance(c, str))
    if pattern:
        globals_ = [c for c in globals_ if _pattern_matches(pattern, c)]
    if globals_:
        notes.append(f"data-scoping: hidden column names: {', '.join(globals_)} (where present).")

    tables = emap.get("tables") or {}
    if table:
        rules = [(table, tables.get(table))]
    else:
        rules = sorted((str(t), rule) for t, rule in tables.items())
    hidden = []
    allowlisted = []
    for t, rule in rules:
        if not isinstance(rule, dict):
            continue
        if "exclude" in rule:
            for col in rule.get("exclude") or []:
                if isinstance(col, str) and (pattern is None or _pattern_matches(pattern, col)):
                    hidden.append(f"{t}.{col}")
        if "include" in rule:
            allowlisted.append(t)
    if hidden:
        notes.append(f"data-scoping: hidden columns omitted: {', '.join(hidden)}.")
    if allowlisted:
        target = ", ".join(sorted(allowlisted))
        notes.append(f"data-scoping: {target} shows an allowlisted subset; only shown columns are queryable.")
    return notes


def _warn_hidden_column_notes(emap: dict, table: str | None = None, pattern: str | None = None) -> None:
    for note in _hidden_column_notes(emap, table=table, pattern=pattern):
        warnings.warn(note, stacklevel=2)


def _is_hidden(emap: dict, table: str, col: str) -> bool:
    """Does the scope_manifest hide ``col`` on ``table``? True iff it's in the global blacklist, the
    table's exclude list, or (whitelist mode) NOT in the table's include list. The whitelist case is
    why we can't enumerate hidden columns up front — we test per requested column."""
    if col in (emap.get("global_exclude") or []):
        return True
    t = (emap.get("tables") or {}).get(table)
    if not isinstance(t, dict):
        return False
    if "exclude" in t:
        return col in (t["exclude"] or [])
    if "include" in t:
        return col not in (t["include"] or [])
    return False


def _split_top_level_commas(s: str) -> list[str]:
    """Split a SELECT list on commas that aren't inside double-quoted identifiers. Callers only reach
    here for the simple shape (no parens/subqueries — `_parse_simple_select` already bailed on those)."""
    out, buf, in_q = [], [], False
    for ch in s:
        if ch == '"':
            in_q = not in_q
            buf.append(ch)
        elif ch == "," and not in_q:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def _bare_column(item: str) -> str | None:
    """The plain column name of a SELECT item, or None if it's not a bare column (alias/AS/expression/
    function/star) — those we never auto-drop. ``t.col`` → ``col``; ``"Weird"`` → ``Weird``."""
    it = item.strip()
    if not it or it == "*" or "(" in it or " " in it:  # space ⇒ alias or AS ⇒ not a bare column
        return None
    if "." in it:
        it = it.rsplit(".", 1)[-1]
    if it.startswith('"') and it.endswith('"') and len(it) >= 2:
        it = it[1:-1]
    return it or None


def _parse_simple_select(sql: str):
    """Match the standard shape ``SELECT <plain col list> FROM <one table> [rest]`` and return
    ``(items, table, list_start, list_end)`` where ``sql[list_start:list_end]`` is exactly the column
    list (so the rebuild preserves the ``FROM`` keyword + everything after). None for anything else
    (``SELECT *``, joins, multiple tables, expressions, subqueries) — a non-match just means "don't heal"."""
    import re

    m = re.match(r"(?is)\s*select\s+(.*?)\s+from\s+(.+)", sql)
    if not m:
        return None
    list_str = m.group(1)
    if "(" in list_str or "*" in list_str:  # expression/subquery/star ⇒ not the simple shape
        return None
    rest = m.group(2).lstrip()
    # First token after FROM is the table; bail on a join or a comma (multiple tables) anywhere after.
    tbl_m = re.match(r'([A-Za-z0-9_."]+)(\s|$)', rest)
    if not tbl_m:
        return None
    table = tbl_m.group(1)
    tail = rest[tbl_m.end():]
    if re.search(r"(?is)\bjoin\b", " " + rest) or "," in rest.split(None, 1)[0] or tail.lstrip().startswith(","):
        return None
    if "." in table:
        table = table.rsplit(".", 1)[-1]
    table = table.strip('"')
    items = _split_top_level_commas(list_str)
    return items, table, m.start(1), m.end(1)


def _strip_excluded(sql: str, emap: dict):
    """Pre-flight heal: drop SELECT-list columns the manifest hides for the FROM table, returning
    ``(new_sql, dropped)``. No-op (``(sql, [])``) unless the query is the simple shape AND names a
    genuinely-hidden column AND at least one column survives — so a working query, a typo (not in the
    manifest), or a query we can't safely parse is left untouched for Postgres to handle."""
    if not emap:
        return sql, []
    parsed = _parse_simple_select(sql)
    if not parsed:
        return sql, []
    items, table, list_start, list_end = parsed
    keep, dropped = [], []
    for it in items:
        col = _bare_column(it)
        if col is not None and _is_hidden(emap, table, col):
            dropped.append(col)
        else:
            keep.append(it)
    if not dropped or not keep:  # nothing hidden, or stripping would empty the SELECT ⇒ don't heal
        return sql, []
    new_sql = sql[:list_start] + ", ".join(s.strip() for s in keep) + sql[list_end:]
    return new_sql, dropped


def query(
    sql: str,
    params: list | tuple | None = None,
    db: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> list[dict]:
    """Run a read-only SELECT and return rows as a list of dicts.

    Opens a fresh read-only connection per call (the container is disposable, so pooling buys
    nothing). ``db`` selects the database (see module docstring); ``timeout_ms`` caps the statement.

    Placeholders: bind UNTRUSTED INPUT as ``%s`` with ``params`` — never string-format input into
    ``sql`` (injection). But a literal ``%`` wildcard is fine inline: ``ILIKE 'avo%'`` with no
    ``params`` runs verbatim (psycopg only treats ``%`` as a placeholder when ``params`` is passed).
    So either inline a static wildcard (``ILIKE 'avo%'``) OR bind a dynamic one (``ILIKE %s`` with
    ``['%' + term + '%']``) — both work; don't mix a static-`%` literal into a query that also binds
    params, since then psycopg scans the whole string and the literal ``%`` needs doubling (``%%``).

    Auto-heal: if the project's data-scoping hides a column you SELECT (standard single-table shape),
    that column is dropped, the trimmed query runs, and a warning names what was dropped — so one extra
    field doesn't fail the whole query. A column that ISN'T manifest-hidden (a typo) is left in and
    raises with a scoping-aware hint (`_undefined_hint`) rather than being silently swallowed.
    """
    import psycopg

    dsn = _resolve_dsn(db)
    emap = _excluded_columns().get(_env_name_for_dsn(dsn) or "", {})
    sql, dropped = _strip_excluded(sql, emap)
    if dropped:
        warnings.warn(
            f"data-scoping: dropped column(s) {dropped} from your SELECT — hidden by this project's "
            f"scope_manifest. Ran the trimmed query; the rest of your result is intact.",
            stacklevel=2,
        )
    with psycopg.connect(dsn, autocommit=False) as conn:
        # Read-only transaction: a write attempt errors instead of mutating customer data.
        conn.read_only = True
        with conn.cursor() as cur:
            if timeout_ms:
                cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            try:
                # Pass None (not []) when there are no params: psycopg only scans the SQL for
                # placeholders when params is a sequence, and that scan rejects a literal `%` (the
                # `ILIKE 'avo%'` wildcard footgun → "only '%s','%b','%t' are allowed as
                # placeholders"). With None the query is sent verbatim, so inline wildcards just work;
                # parameterised queries (params given) still bind `%s` normally.
                cur.execute(sql, params if params else None)
            except (psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable) as e:
                # Still undefined after the pre-flight heal ⇒ a typo, a hidden column used in WHERE/
                # ORDER BY (which we don't rewrite), or a shape we couldn't parse. Enrich so the agent
                # fixes the one bad name instead of rewriting. `from e` keeps the original traceback.
                raise RuntimeError(f"{e}\n\n{_undefined_hint(e)}") from e
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
    rows = query(
        "select column_name, data_type from information_schema.columns "
        "where table_schema = coalesce(%s::text, current_schema()) and table_name = %s "
        "order by ordinal_position",
        [schema, table],
        db=db,
    )
    _warn_hidden_column_notes(_excluded_map_for_db(db), table=table)
    return rows


def tables_with_column(name_like: str, schema: str | None = None, db: str | None = None) -> list[dict]:
    """Find (table, column) pairs whose column name matches an ILIKE pattern, e.g. ``%email%``.

    The entry point for locating where data lives (an account email, a usage column) when the
    schema isn't pinned down — discover the identifier here, never take it from the ticket.
    ``schema=None`` (default) searches the run's EFFECTIVE schema (``current_schema()``) — the
    ``scope_<id>`` views on a scoped run, ``public`` on a flat project (see `columns`).
    """
    rows = query(
        "select table_name, column_name, data_type from information_schema.columns "
        "where table_schema = coalesce(%s::text, current_schema()) and column_name ilike %s "
        "order by table_name, column_name",
        [schema, name_like],
        db=db,
    )
    _warn_hidden_column_notes(_excluded_map_for_db(db), pattern=name_like)
    return rows


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
        print(_format_catalog())
        return 0
    if not args.sql:
        p.error("provide SQL, or --list")
    rows = query(args.sql, db=args.db, timeout_ms=_parse_duration_ms(args.timeout))
    _output.emit_rows(rows, args.format, label="db")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
