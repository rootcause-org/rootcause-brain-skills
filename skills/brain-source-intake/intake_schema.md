# brain-source-intake: files and contracts

One output directory per intake: `$OUT = .rootcause/source-intake/<date>/`. Python writes the
evidence tier, you write four small things, `validate.py` assembles `intake.json`, `render.py`
turns it into `report.html`. Every path in every file is the **production** shape
`/mirrors/<name>/<relative path>[:symbol]`, never the laptop path; `scan.md` prints the mapping.

## Python writes (evidence)

| File | Tier | Content |
|---|---|---|
| `scan.md` | read whole (≤ 300 lines) | per repo and per app root: framework guesses with the marker that proved them, then one block per area (entry · routes · config · models · mail · jobs · integrations · migrations · tests) listing ≤ 12 candidate paths with line counts; the mapping `<name> → <local path>`; extension tally per top-level dir; `tables.tsv` summary when tables were given |
| `scan.json` | drill by key | the same, machine-shaped: `repos[]{name, root, mode: local|listing, files, apps[]{root, frameworks[]{name, marker}, areas{<area>: [{path, lines}]}, deps[]}}` plus `tables[]` |
| `tables.tsv` | read whole | only with `--tables FILE`: `table \t model_files \t mentions \t guess`; `model_files` = files declaring that table (`$table = '…'`, `#[ORM\Table(name: …)]`, `@ORM\Table`, `protected $table`), `mentions` = files in model/migration areas containing the bare word, `guess` = a model file whose class name matches the table (`salon_users` → `SalonUser`), empty when nothing matched. Unmatched tables are listed last |
| `questions.tsv` | read whole | `id \t date \t channel \t question \t url`; `id` = `Q1…`; `question` ≤ 200 chars, PII-free; noise-tagged conversations are dropped |
| `raw/listing-<name>.txt` | validator only | every relative file path of that repo (the walk, or the listing you passed) |
| `raw/helpcenter/` | drill | the sibling collector's full output (`digest.md`, `evidence.json`); grep by id, never read whole |

## You write (judgement)

### `proposal/INDEX.md` and `proposal/<area>.md`

The proposed `skills/codebase/` tree for the project brain, one file per area, `INDEX.md` routing by
customer symptom to the area files. Rules the validator enforces:

- YAML frontmatter with `description:` on every file (the brain's skill contract).
- Every backtick token that starts with `/mirrors/<name>/` must exist in that repo's listing
  (`:symbol` suffixes are stripped first; a directory counts when any listed file is under it).
- No fenced code blocks and no line over 400 characters: paths and method names, never snippets.
- No em or en dashes as connectors, no emoji.
- No secret shapes: `AKIA…`, `sk_live_`, `-----BEGIN`, `password=…`, DSN URLs with credentials.
- ≤ 150 lines per file, ≤ 12 files.

Prefix every unconfirmed claim with `(?)`. The report shows the proposal so the dev can strike
those.

### `benchmark.tsv`

One row per `questions.tsv` id, tab separated, header row required:

```
id	cluster	status	where	note
Q1	booking-visibility	found	/mirrors/app/application/models/Treatment_model.php:online_bookable;db:treatments.online	rule lives in one place
Q2	mail-delivery	ambiguous	/mirrors/app/src/Mailer/ReminderMailer.php;/mirrors/app/application/libraries/Mailer.php	legacy and new sender both exist
Q3	invoices	missing		no invoice code found in either app
Q4	pricing	n/a		sales question, no code needed
```

- `cluster`: a slug for the question type; the same slug on every row of that type. Clusters, not
  rows, are what the dev questions cite.
- `status`: `found` (one grounding place) · `ambiguous` (two or more candidate places) · `missing`
  (nothing locatable) · `n/a` (no code or table would ground the answer).
- `where`: `;`-separated locators, each `/mirrors/<name>/path[:symbol]` (must exist) or
  `db:<table>[.<column>]` (must be in `tables.tsv` when tables were given). `found` needs ≥ 1,
  `ambiguous` ≥ 2, `missing` and `n/a` none.

### `devquestions.tsv`

The questionnaire for the customer's developer, header row required:

```
id	group	question	proposal	evidence
D1	architecture	Which requests still hit the CodeIgniter app and which the Symfony app? Is there a routing rule or is it per URL prefix?	(?) /mirrors/app/public/index.php forwards /v2/* to Symfony, everything else to CI	mail-delivery;/mirrors/app/public/index.php
D2	db	Is `users` the salon (tenant) table? Where is the end-customer table?	(?) users = salons, customers = end-customers	db:users;db:customers
```

- `group`: `architecture` · `where` (where does X live) · `conventions` · `db` · `deploy`.
- `question`: one concrete question the dev answers in a sentence or a path. Never "explain the
  architecture".
- `proposal`: optional; our hypothesis, rendered with a confirm / deny / unsure toggle. Empty when we
  have none.
- `evidence`: `;`-separated benchmark cluster slugs or locators; every `ambiguous` and `missing`
  cluster must be cited by at least one question (validator error otherwise). ≤ 40 questions.

### `headline.txt`

Optional, 1 to 3 lines for the top of the report: coverage caveats (listing mode, no tables,
questions from a hand list), the one thing the dev must know first.

## Assembled

`intake.json` = `{project, collected_at, scan, questions[], benchmark[], devquestions[], proposal:
{<file>: <markdown>}, headline, tally{found, ambiguous, missing, n_a}}`. `render.py` reads only
this file.

## After the dev answers

The report's copy button yields markdown, one block per question:

```
### D2 (db): Is `users` the salon table? …
Proposal: (?) users = salons, customers = end-customers
Verdict: confirmed
Answer: yes; `customers` holds end-customers, `users` is one row per salon login (a salon can have several)
```

Paste that into the agent thread. Denied or corrected proposals are rewritten in the proposal files
before they move into the brain; a question without an answer stays `(?)` in the brain.
