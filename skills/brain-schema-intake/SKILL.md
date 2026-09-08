---
name: brain-schema-intake
description: "Turn a grounding database that just became readable into brain knowledge that lets future runs answer from data: a bounded schema probe on the box, a benchmark of real customer questions into db / kb / both / human, and a self-contained questionnaire (report.html) for the customer's developer whose answers you write back as skills/databases/<db>.md. Use for 'we got the database', 'map the schema for the brain', 'which questions can we answer from data', 'what does this table really hold'."
---

# brain-schema-intake: from a readable database to a brain that can answer from it

**Python is the evidence, you are the judgement.** `collect.py` ships `probe.py` to the box and
reduces its JSON to `schema.md`; `questions.py` reuses the source-intake collector for a window of
real customer questions; you write the proposed database map, one benchmark row per question and a
short questionnaire; `validate.py` refuses any table or column that does not exist; `render.py`
renders `report.html` for the developer. Their pasted answers are step 4.

The database twin of [`brain-source-intake`](../brain-source-intake/SKILL.md), and it shares that
skill's TSV reader, hygiene rules and HTML renderer. Same shape, same report, other evidence.

North star: **a helicopter view, not a data dictionary.** The brain must let a run answer "which
table and column carries this fact" in one hop, and know when the data is not the answer.
Everything you infer stays `(?)` until the developer confirms.

Read-only: information_schema plus bounded value samples, no writes, no brain edits before step 4
([docs/side-effects.md](../../docs/side-effects.md)). Scratch lives in the gitignored `.rootcause/`.

## When to use

- A grounding database was registered for the project ([docs/secrets.md](../../docs/secrets.md)) and
  `rc dev console database list` shows it.
- The brain answers "please check in your account" where a row could state the fact.
- Runs keep guessing table names, or read the wrong one.

Not for: writing queries for one run (that is [`prod-console`](../prod-console/SKILL.md)), tuning
the schema, or documenting every column.

## Pipeline

```bash
cd ~/code/rootcause-org/rootcause-brain-<project>
SI="$PWD/.agents/skills/brain-schema-intake"

rc dev console database list                       # the short key, for example `staging`
uv run "$SI/scripts/collect.py" --db staging       # probe on the box, fetch, reduce
OUT="$PWD/.rootcause/schema-intake/<date>"         # last line of the collect summary

uv run "$SI/scripts/questions.py" --days 60        # or --from questions.txt (one per line)
cat "$OUT/schema.md" "$OUT/questions.tsv"          # the read-whole tier

# write $OUT/proposal/<db>.md, benchmark.tsv, devquestions.tsv, headline.txt (intake_schema.md)
uv run "$SI/scripts/validate.py" "$OUT"            # exit 1 + one line per problem, per file
uv run "$SI/scripts/render.py" "$OUT/intake.json"  # -> report.html
open "$OUT/report.html"                            # hand over the file path, never a localhost URL
```

`rc dev console database query` refuses MySQL today, so `collect.py` runs Python on the box through
`rc dev console bash run --timeout N -` (the `-` reads the command from stdin, so the probe travels
as a heredoc), writes JSON under `/tmp/rootcause-out/` and fetches it with
`rc dev console file get`. `--dry-run` prints that command without running it, `--from schema.json`
re-reduces without touching the console. Console sessions cache their env for about two minutes, so
a freshly sealed DSN can need one retry.

## Step 1: orientation (bounded)

Budget: `schema.md` whole, then **at most 20 drills** through
[`prod-console`](../prod-console/SKILL.md) (a `select` with a `limit`, on one table, never a text or
blob column, never a whole row of PII). Never dump a table, never read all columns of 170 tables.
The probe already sampled enum-like values for you.

What to settle, one line each in the proposal:

1. **Tenant key.** Which column scopes a row to a customer, which tables lack it and why.
2. **Core entities.** The ten tables a support answer actually touches, in the customer's words.
3. **Settings.** One row per tenant tables, and which column is the setting the customer asks about.
4. **Queues and logs.** What proves that something was sent or processed, and what it costs to read.
5. **Naming oddities.** A table whose name lies (`remarks` is the ticket header), a column that
   means something else, a status integer nobody documented.
6. **Soft deletes and time.** The delete convention, and which timestamp style a table uses.
7. **Ownership.** Which tables the current app writes and which only the legacy one still touches.

Write it as `proposal/<db>.md` (up to 6 files when it grows), every guess prefixed `(?)`.

## Step 2: question benchmark

`questions.tsv` is the last 60 days of real inbound questions (the sibling collector). No corpus
yet: write 20 to 40 representative questions by hand and pass `--from`.

Cluster by the product concept the answer hinges on, then decide per cluster what support should do:

- `db`: answer from data. Name the tables and columns a run reads.
- `kb`: answer from static knowledge. No lookup, so no schema question either.
- `both`: the row states the fact, the customer still needs the rule around it.
- `human`: a write, money, or a judgement call. Support prepares, a person decides.

The `db` and `both` tally is the acceptance number: what share of real questions the database can
ground today.

## Step 3: the questionnaire

Every oddity and every unconfirmed `(?)` becomes one closed question in `devquestions.tsv`. Groups:
`tenant` · `entities` · `settings` · `queues` · `conventions` · `ownership`. Rules:

- Concrete and closed: "Is `remarks` the ticket header, and is `remarks.total` the amount?" beats
  "How does the POS work?". A yes, a no or a table name must be enough to answer.
- 15 to 25 questions. The developer gives this fifteen minutes.
- Cite evidence: the cluster slug (the report shows the customer questions behind it) or the table.

`report.html` is self-contained (file://, no external assets): question cards with a free-text
answer, a confirm / deny / unsure toggle on every proposal, the verdict table, the proposed map and
the tables at a glance collapsed underneath, and one "Copy all as Markdown" button. Answers persist
in the browser until copied.

## Step 4: write the answers into the brain

Paste the copied markdown into this thread. Then, for the project brain:

1. Rewrite denied or corrected proposals; drop `(?)` on confirmed ones; keep it on the unanswered.
   Re-run `validate.py`.
2. Move `proposal/*.md` to `skills/databases/` in the brain. Add the symptom to table rows to the
   brain's `AGENTS.md` grounding hints, new words to `terminology.md`, and the `human` clusters to
   the action or escalation rules. Keep `description:` frontmatter on every file.
3. Re-read the benchmark against the brain alone: for three `db` questions, can a run name the table
   and column without opening `schema.md`? If not, the map is missing a hop.
4. [`brain-git-sync`](../brain-git-sync/SKILL.md), then [`brain-publish`](../brain-publish/SKILL.md);
   confirm with one [`brain-ask`](../brain-ask/SKILL.md) run on a `db` question.

## What stays out of the brain

- Customer data: no row values beyond the enum-like samples, no names, no mail addresses.
- DSNs, credentials, connection details: name the database key, never the value.
- Column-by-column dictionaries, DDL, index definitions: the schema is in the database.
- Unconfirmed guesses without `(?)`, and any claim without a table behind it.
- Performance opinions, and anything the developer asked to keep out.

## Acceptance

A future run, given only the brain, names the table and the column for the benchmark's `db`
questions, and stops at `human` ones. The tally is in the brain's notes with a date, so the next
intake measures progress.

## More

[intake_schema.md](intake_schema.md) the files you write and the validator's rules ·
[`brain-source-intake`](../brain-source-intake/SKILL.md) the code twin, whose renderer and hygiene
rules this skill reuses · [`prod-console`](../prod-console/SKILL.md) the drills ·
[docs/secrets.md](../../docs/secrets.md) registering a grounding database ·
[docs/brain-model.md](../../docs/brain-model.md) grounding hints and the brain layout.

## Iteration log

- **2026-09-08** built (probe/collect/questions/validate/render, fixture tests), sharing
  `intake_common.py` and the flavoured renderer with brain-source-intake. First run on iBeauty
  (Percona 5.6, 172 tables, 1819 columns): 192 queries in 70 s, nothing skipped; 83 questions,
  27 db, 12 both, 28 kb, 16 human. Two probe fixes from that run: never sample id-shaped string
  columns or tables under 20 rows (a Mollie customer id leaked into a profile), and spend the
  exact-count budget on tables the size of the tenant table, not the smallest ones (that is how
  `settings` earns its one-row-per-tenant role). Proposal tables with many columns hit the
  400-character line cap: write column lists as bullets, not table rows.
