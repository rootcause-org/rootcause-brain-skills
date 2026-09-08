# brain-schema-intake: files and contracts

One output directory per intake: `$OUT = .rootcause/schema-intake/<date>/`. Python writes the
evidence tier, you write four small things, `validate.py` assembles `intake.json`, `render.py`
turns it into `report.html`. Every locator is `table` or `table.column` and must exist in
`schema.json`.

## Python writes (evidence)

| File | Tier | Content |
|---|---|---|
| `schema.md` | read whole | header (engine, version, database, table/column/FK counts, tenant candidates with table counts, tables lacking the tenant column), then one line per base table sorted by rows: `name · rows (exact when counted) · MB · role · tenant column · flags`; then views, the query budget and the skipped list |
| `schema.json` | drill by key | the probe's output: `{engine, version, database, db, collected_at, tenant_candidates[], tables[], columns[], keys[], indexes[], profiles[], counts[], budget{queries, seconds, skipped[]}}` |
| `questions.tsv` | read whole | `id \t date \t channel \t question \t url`, from the sibling source-intake collector |
| `raw/helpcenter/` | drill | that collector's full output; grep by id, never read whole |

Roles are a guess from names, indexes and counts: `log` (name looks like a log or queue, or an
`already_sent`-style column next to a time column) · `settings` (one row per tenant, proven by a
unique index or by `count(*) == count(distinct tenant)`) · `link` (two or three columns, all
`*_id`) · `lookup` (at most 200 rows) · `core`. Flags: `soft-delete:<col>`, `ts:<col>` (the
timestamp style, a legacy versus new hint), `fk-out:N fk-in:N`, `enum:<col>=<top values with
shares>`, `comment:<table comment>`.

## You write (judgement)

### `proposal/<db>.md`

The proposed `skills/databases/` file for the project brain, up to 6 files. Rules the validator
enforces:

- YAML frontmatter with `description:` on every file.
- Every backtick token shaped `table.column` must exist in `schema.json`. A bare backtick token
  that looks like an identifier must be a table name or some column name; anything else is an
  error, so do not backtick prose.
- No fenced code blocks, no line over 400 characters, at most 150 lines per file. A markdown table
  row is one line: list many columns as bullets instead.
- No em or en dashes as connectors, no emoji, no secret shapes.

Prefix every unconfirmed claim with `(?)`.

### `benchmark.tsv`

One row per `questions.tsv` id, tab separated, header row required:

```
id	cluster	verdict	where	note
Q1	booking-state	db	agenda.status;agenda.deleted	the row carries the state and the soft delete
Q3	reminder-settings	both	settings.reminder_time	the row holds the hour, the rule around it is knowledge
Q5	pricing	kb		a sales question, no row would answer it
Q6	refunds	human		money leaves the account, a person decides
```

- `cluster`: a slug for the question type, the same slug on every row of that type. Clusters, not
  rows, are what the dev questions cite.
- `verdict`: `db` (answer from data) · `kb` (static knowledge) · `both` · `human` (write, money or
  judgement).
- `where`: `;`-separated `table[.column]`. `db` and `both` need at least one, `kb` and `human` take
  none.

### `devquestions.tsv`

The questionnaire, header row required:

```
id	group	question	proposal	evidence
D2	entities	Is `remarks` really the point of sale ticket header?	(?) remarks is the ticket header, remarks.total is the amount	pos-ticket;remarks
D3	entities	Which table holds the end customer? We see `agenda.customer_id` but no table it points to.		agenda.customer_id
```

- `group`: `tenant` · `entities` · `settings` · `queues` · `conventions` · `ownership`.
- `question`: one concrete question, answerable with a yes, a no or a table name.
- `proposal`: optional; our hypothesis, rendered with a confirm / deny / unsure toggle.
- `evidence`: `;`-separated benchmark cluster slugs or `table[.column]` locators, each resolvable.
  At most 40 questions, a warning above 25.

### `headline.txt`

Optional, 1 to 3 lines for the top of the report: which database was read, what the probe skipped,
the one thing the dev must know first.

## Assembled

`intake.json` = `{project, db, engine, collected_at, schema, questions[], benchmark[],
devquestions[], proposal{<file>: <markdown>}, headline[], tally{db, both, kb, human}}`. The
benchmark rows keep the verdict under the key `status`, which is what the shared source-intake
renderer reads. `render.py` reads only this file.

## After the dev answers

The report's copy button yields markdown, one block per question:

```
### D2 (entities): Is `remarks` really the point of sale ticket header?
Proposal: (?) remarks is the ticket header, remarks.total is the amount
Verdict: confirmed
Answer: yes, and a refunded ticket keeps state = open with a negative total
```

Paste that into the agent thread. Corrected proposals are rewritten before they move into the
brain; a question without an answer stays `(?)`.
