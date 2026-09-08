---
name: brain-source-intake
description: "Turn a customer's codebase that just became readable (a rootcause source mirror or a local clone) into brain knowledge that lets future runs navigate it fast: a bounded orientation scan, a benchmark of real customer questions against the code and DB, and a self-contained questionnaire (report.html) for the customer's developer whose answers you write back into the project brain. Use for 'we got the source', 'map the codebase for the brain', 'which questions can the code answer', 'questions for their dev'."
---

# brain-source-intake: from readable source to a brain that can navigate it

**Python is the evidence, you are the judgement.** `scan.py` walks the repo(s) within a hard budget
and writes `scan.md`; `questions.py` reuses the help-centre collector for a window of real customer
questions; you write a proposed codebase map, a benchmark row per question and a short questionnaire;
`validate.py` refuses any path that does not exist and any snippet, secret or unanchored claim;
`render.py` renders `report.html` for the developer. Their pasted answers are step 4.

North star: **a helicopter view, not an audit.** The brain must let a future run answer "where would
I look for X" in one hop: which app, which directory, which table. It must not teach the framework,
mirror the code, or judge its quality. Everything you infer stays `(?)` until the developer confirms.

Read-only: no `rc ask`, no writes to the mirror, no brain edits before step 4
([docs/side-effects.md](../../docs/side-effects.md)). Scratch lives in the gitignored `.rootcause/`.

## When to use

- A source mirror appeared for the project (`rc dev console capabilities` lists it under
  `/mirrors/<name>`), or the developer handed over a clone or a read-only GitHub grant.
- A brain answers "check your settings" where the code could say which setting.
- Onboarding a project whose product is configuration-heavy and undocumented.

Not for: reviewing code, hunting bugs, or documenting an API. Those are runs, not intake.

## Pipeline

```bash
cd ~/code/rootcause-org/rootcause-brain-<project>
SI="$PWD/.agents/skills/brain-source-intake"

# repo readable on this laptop (clone, or `[mirrors]` in .rootcause.toml)
uv run "$SI/scripts/scan.py" --repo app=~/code/customer/app [--tables tables.txt]
# repo only readable as a production mirror: capture a listing through the console, scan that
rc dev console bash run 'mkdir -p /tmp/rootcause-out; find /mirrors/app -type f -not -path "*/.git/*" > /tmp/rootcause-out/listing.txt; wc -l < /tmp/rootcause-out/listing.txt'
rc dev console file get /tmp/rootcause-out/listing.txt --out /tmp/listing-app.txt
uv run "$SI/scripts/scan.py" --listing app=/tmp/listing-app.txt [--tables tables.txt]
# the table list, when a grounding database is registered (rc dev console database list)
rc -o json dev console database query <db> "select table_name from information_schema.tables where table_schema = database()" --all --format csv --out tables.txt
OUT="$PWD/.rootcause/source-intake/<date>"                       # last line of the scan summary

uv run "$SI/scripts/questions.py" --days 60                      # or --from questions.txt (one per line)
cat "$OUT/scan.md" "$OUT/questions.tsv" "$OUT/tables.tsv"        # the read-whole tier

# write $OUT/proposal/*.md, benchmark.tsv, devquestions.tsv, headline.txt (intake_schema.md)
uv run "$SI/scripts/validate.py" "$OUT"                          # exit 1 + one line per problem, per file
uv run "$SI/scripts/render.py" "$OUT/intake.json"                # → report.html
open "$OUT/report.html"
```

Local clone beats listing: line counts, manifests and the table mapping need file contents. In
listing mode you drill through `rc dev console bash run 'head -60 /mirrors/app/...'`; budget the
same way.

## Step 1: orientation (bounded)

Budget: `scan.md` whole (≤ 300 lines), then **at most 40 drills** of `head -80`, `sed -n a,b p` or
`rg -n -l`; never `cat` a file over 200 lines, never read a whole directory. Sample two or three
files per area and stop when the pattern is clear. Whole-repo reads are the failure mode this skill
exists to prevent: a 10-year PHP app is millions of tokens and teaches nothing a map would not.

What to settle, in this order, one line each in the proposal:

1. **Apps.** Which apps live in the repo(s) and how a request reaches each (legacy CodeIgniter under
   `/` and Symfony under `/v2/`, a monorepo split, a separate admin). The marker that proved it.
2. **Entry and routing.** Front controllers, route files, how a URL maps to a controller or action.
3. **Settings, feature flags, config.** Where per-tenant settings live (a table, a JSON column, a
   config file), where global flags live, how the app reads them.
4. **Data.** The ORM, the table to model mapping (`tables.tsv`), the tenant key, naming oddities
   (`users` is the salon, `customers` the end customer). Ten important tables beat all 172.
5. **Senders.** Mail and notification senders, templates, the provider, the log or table that proves
   a mail left.
6. **Jobs.** Cron, queue workers, schedulers: the reminder that runs at 07:00 and where.
7. **Integrations.** Payment, mail, SMS, accounting: the client class and the config key name (never
   the value).
8. **Conventions and oddities.** Naming, timezones, soft deletes, magic status integers, dead
   directories still deployed.

Write it as `proposal/INDEX.md` (routing: customer symptom → area file → first path to open) plus
one file per area, paths in the production shape `/mirrors/<name>/…:method`, every guess prefixed
`(?)`. Progressive disclosure: the index is what a run reads first, an area file is what it opens
next, the code is what it opens last.

## Step 2: question benchmark

`questions.tsv` is the last 60 days of real inbound questions (email runs, chat runs, Help Scout:
the [`brain-helpcenter-suggestions`](../brain-helpcenter-suggestions/SKILL.md) recipe). No corpus
yet: write 20 to 40 representative questions by hand from the brain's notes and pass `--from`.

Cluster by the product concept the answer hinges on (booking visibility, reminder delivery, deposit
refund), then for each cluster locate where a run would ground the answer: a file and method, a
table and column, or both. One row per question in `benchmark.tsv`:

- `found`: one place, named. A future run reads that and answers.
- `ambiguous`: two candidate places (legacy and new sender both exist, two settings tables). The
  developer decides, so it becomes a question.
- `missing`: nothing locatable within budget. Also a question.
- `n/a`: no code or table would ground the answer (pricing, sales, a human decision).

The tally is the acceptance number: what share of real questions the code can ground today.

## Step 3: the questionnaire

Every `ambiguous` and `missing` cluster yields one question in `devquestions.tsv`; add the
`(?)` guesses from step 1 as questions with a `proposal`, so the developer confirms or denies with
one click. Groups: `architecture` · `where` · `conventions` · `db` · `deploy`. Rules:

- Concrete and closed: "Which table holds the per-salon reminder time, and which column?" beats
  "How do reminders work?". A path or a sentence must be enough to answer.
- 15 to 25 questions; the developer gives this fifteen minutes, not an afternoon.
- Cite evidence: the cluster slug (the report shows the customer questions behind it) or the path.

`report.html` is self-contained (file://, no external assets): question cards with a free-text
answer, a confirm / deny / unsure toggle on every proposal, the benchmark and the proposed map
collapsed underneath, and one "Copy all as Markdown" button. Deliver it where the developer already
works; answers persist in the browser until copied.

## Step 4: write the answers into the brain

Paste the copied markdown into this thread. Then, for the project brain:

1. Rewrite denied or corrected proposals in `proposal/`; drop `(?)` on confirmed ones; keep `(?)`
   on the unanswered. Re-run `validate.py`.
2. Move `proposal/*.md` to `skills/codebase/` in the brain. Add the symptom → area rows to the
   brain's `AGENTS.md` routing, new words to `terminology.md`, DB semantics next to the grounding
   notes. Keep `description:` frontmatter on every file.
3. Re-run the benchmark in your head against the brain alone: for three `found` questions and every
   answered one, can a run name the file or table from the brain without opening the code? If not,
   the map is missing a hop.
4. [`brain-git-sync`](../brain-git-sync/SKILL.md), then [`brain-publish`](../brain-publish/SKILL.md);
   confirm with one [`brain-ask`](../brain-ask/SKILL.md) run on a `found` question.

## What stays out of the brain

- Secrets, DSNs, keys, `.env` contents: name the key, never the value.
- File dumps, snippets, code fences: a path and a method name is the whole reference.
- Framework tutorials: a run knows Laravel. It does not know that this Laravel serves `/v2/` only.
- Unconfirmed guesses without `(?)`, and any claim without a path behind it.
- Judgements on code quality, and anything the developer asked to keep out.

## Acceptance

A future run, given only the brain, can answer "where would I look for X" for the benchmark's
`found` questions with a path or a table, and knows which questions need the developer. The
benchmark tally is in the brain's notes with a date, so the next intake measures progress.

## More

[intake_schema.md](intake_schema.md) the files you write and the validator's rules ·
[docs/mirrors.md](../../docs/mirrors.md) declaring a local checkout as a mirror ·
[docs/brain-model.md](../../docs/brain-model.md) routing rows and the source map convention ·
[`prod-console`](../prod-console/SKILL.md) console drills and `database schema` ·
[`brain-website-scout`](../brain-website-scout/SKILL.md) the same scout then synthesise shape for a website.

## Iteration log

- **2026-09-08** built (scan/questions/validate/render, fixture tests). First target: iBeauty (PHP,
  CodeIgniter plus Symfony side by side, MySQL 5.6 with 172 tables). Not yet run against it.
