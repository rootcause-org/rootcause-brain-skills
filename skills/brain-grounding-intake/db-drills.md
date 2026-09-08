# Drilling a production database without hurting it

The grounding database is production, or a restore of it, shared with real users. A wrong query
does not return an error, it stalls the box for everyone. `lib.db` caps every statement with a
server-side timeout and a read-only transaction, and `query.py` refuses the obvious mistakes, but
the caps are a seatbelt, not a licence: a statement that times out has already burned that time.
The same caps apply to a production run, so anything slow for you is impossible for a run, and the
brain must say so (which table is expensive, which indexed path to use).

Budget: `schema.md` whole, then at most 20 drills. Every drill goes through
`uv run "$GI/scripts/query.py" --db <key> "SQL"` so it lands in `drills.log`.

## Before any query on a table

1. Know its size. `schema.md` prints the row estimate and the size per table; trust it. Under
   200k rows anything with a `LIMIT` is fine. Between 200k and 2M rows, group and filter only on an
   indexed column and `EXPLAIN` first. Above 2M rows: no `GROUP BY`, no `COUNT(DISTINCT)`, no
   `WHERE` on an unindexed column, no `ORDER BY` on anything but the primary key. Sample instead.
2. Know its indexes. `schema.json` has them (`indexes[]`); or `SHOW INDEX FROM t` (MySQL),
   `select indexdef from pg_indexes where tablename = 't'` (Postgres). Both are metadata reads.
3. `EXPLAIN` the statement. MySQL: `type: ALL` or a `rows` estimate in the millions means stop.
   Postgres: a `Seq Scan` on a big table means stop. Never `EXPLAIN ANALYZE`, it executes.

## Read the statistics the engine already keeps (free)

The value distribution you want (which status values exist, how skewed, how many distinct) is
usually already computed by the engine's statistics. Read those before touching the table.

Postgres:

- `select attname, n_distinct, null_frac, most_common_vals, most_common_freqs from pg_stats where
  tablename = 't'`: the top values and their shares per column, from the last ANALYZE. This answers
  "what can `status` be" for any table size in one metadata read.
- `select reltuples::bigint from pg_class where relname = 't'` row estimate;
  `pg_total_relation_size('t')` bytes; `pg_stat_user_tables.n_live_tup` and `last_autoanalyze`.
- Enum types: `select enumlabel from pg_enum e join pg_type t on t.oid = e.enumtypid where
  t.typname = 'x'`. Check constraints: `pg_constraint`. Comments: `obj_description`,
  `col_description`.
- Cheap random sample of a huge table: `select <cols> from t tablesample system (1) limit 30`.

MySQL and MariaDB:

- `information_schema.TABLES`: `TABLE_ROWS` (an estimate on InnoDB, off by a factor sometimes),
  `DATA_LENGTH`, `INDEX_LENGTH`, `TABLE_COMMENT`, `UPDATE_TIME`.
- `information_schema.STATISTICS`: `CARDINALITY` per indexed column, the distinct-count estimate.
  A `status` column with cardinality 4 has four values; read them with `select distinct status from
  t limit 20` only if the column is indexed or the table is small.
- `information_schema.COLUMNS`: `COLUMN_TYPE` shows `enum(...)` and `set(...)` members verbatim,
  `COLUMN_COMMENT` the developer's own note. The probe already put these in `schema.md`.
- MySQL 8: `information_schema.COLUMN_STATISTICS` holds histograms when someone ran
  `ANALYZE TABLE ... UPDATE HISTOGRAM`; rare, but free when present.
- `show create table t` for the full DDL in one read.

## Sampling rows, safely

- Name the columns, never `select *` on a wide or PII-bearing table. Skip text and blob columns.
- Recent rows: `select <cols> from t order by <pk> desc limit 30`. Descending primary key is an
  index walk, cheap on any size. `order by created_at` on an unindexed column sorts the table.
  The primary key is not always `id`: read it from `schema.json` (`keys[]`, `indexes[]`) before
  guessing (a 7M-row `agenda` had `agenda_id`, and the first drill failed on `id`).
- Ids are sparse more often than not: `where agenda_id > 6900000` on a 6.95M-row table still
  matched 2.5M rows. Take `select max(<pk>) from t` first (an index tail read), then slice within
  50k of it.
- A slice for grouping on a huge table: `select status, count(*) from t where <pk> > <max minus
  50000> group by status limit 20`.
- One tenant: filter on the tenant key plus the indexed date column, `limit 30`. The first index
  in `schema.json` for the table tells you the column order the filter must follow.
- Joins: at most one, the big side filtered on an indexed key, always `limit`. Two big tables in
  one join is the query that stalls the box.
- Counts: `count(*)` on InnoDB walks an index; fine under 2M rows, use the estimate above it.

## Decoding a value you do not understand

An integer status, a cryptic flag, a table whose name lies: do not guess from data alone.

1. Grep the code first: `rg -n "status" /mirrors/<name>/application/models/m_agenda.php` or the
   model of the table. Constants, enums and `case` branches name the values.
2. Search the web for the framework or vendor convention before inventing one:
   CodeIgniter `ci_sessions`, Laravel `jobs` and `failed_jobs`, Rails `schema_migrations`,
   Doctrine `doctrine_migration_versions`, Mollie payment statuses, SendGrid event names. A
   vendor's own documentation beats a sample.
3. Only then confirm with data: `select <non PII cols> from t where status = 3 limit 5`.
4. Still unclear: it is a dev question, with the guess as the proposal.

## What never leaves the drill

Names, addresses, mail, phone, tokens, free text. Sample flags, dates, amounts and ids; when a
row value must appear in the brain it is an enum label or a count, never a person.
