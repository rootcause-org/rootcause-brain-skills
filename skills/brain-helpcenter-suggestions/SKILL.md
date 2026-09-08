---
name: brain-helpcenter-suggestions
description: "Turn one window of real customer questions into a short, evidence-backed edit list for the project's public help centre (new / rewrite / retitle / merge / delete / add-alias), each with verbatim customer quotes and a link per conversation. Python collects the question corpus and the article inventory from a brain checkout with public rc, you judge and write suggestions.json, Python validates and renders report.html. Use for 'help centre gaps', 'which articles are missing', 'KB suggestions for the owner', 'wat vragen klanten dat niet in de kennisbank staat'."
---

# brain-helpcenter-suggestions — what customers asked vs what the help centre says

**Python is the evidence, you are the judgement.** `collect.py` pulls a window of real conversations
and the help-centre inventory into `evidence.json` + `digest.md`; you read the digest, drill the few
articles that matter, and write `suggestions.json`; `validate.py` refuses anything not provable from
the evidence; `render.py` turns it into one `report.html` for the help-centre owner.

North star: **five suggestions with proof beat forty guesses.** Every suggestion is one concrete edit
the owner can make in minutes, with the customer's own words and a link to each conversation. Zero
suggestions is a valid result. The report is for the owner (e.g. the founder who writes the articles),
not for a developer. **One report = one help centre = one owner**: on a tenant-scoped project the
tenant's public KB and the project's own docs are different audiences — run per tenant (`--tenant`),
never mix them.

Read-only: every `rc` call lists, traces or reads. Never `rc ask`, never edit the brain or the help
centre from here ([docs/side-effects.md](../../docs/side-effects.md)). Scratch stays under the
gitignored `.rootcause/` — the report carries customer snippets and tokenized run links.

## Pipeline

```bash
cd ~/code/rootcause-org/rootcause-brain-<project>
HC="$PWD/.agents/skills/brain-helpcenter-suggestions"

uv run "$HC/scripts/collect.py" --days 9              # N calendar days ending today · --out DIR · --corpus dump.txt
uv run "$HC/scripts/collect.py" --days 8 --tenant yes_events   # tenant-scoped project: one tenant per run
OUT="$PWD/.rootcause/helpcenter/<window-end-date>[-<tenant>]"  # last line of the collect summary
cat "$OUT/conversations.tsv" "$OUT/articles.tsv"       # tier 0 — read whole; copy the evidence sha
grep -A12 "^##### run:53ff0f29" "$OUT/digest.md"       # tier 1 — one conversation in full, drill by id

# drill 2 — prove absence: one word-bounded grep over the KB root for the customer's words
rc dev console bash run -o json --raw-output 'rg -n -i -w "cabine|toestel|klantenstop" /kb --glob "*.md" 2>&1 | head -60' | jq -r .stdout
# drill 3 — open the bodies of the articles a suggestion touches (batch several per call; 2>&1 so a bad path is visible)
rc dev console bash run -o json --raw-output 'sed -n "1,150p" <path-from-articles.tsv> 2>&1' | jq -r .stdout
# tenant scope: rc dev console --tenant <slug> bash run …

# write $OUT/suggestions.json — suggestions_schema.md is the contract, fixtures/ the example
uv run "$HC/scripts/validate.py" "$OUT/suggestions.json"   # exit 1 + one line per problem; fix, re-run
uv run "$HC/scripts/render.py"   "$OUT/suggestions.json"   # → report.html + learnings.md
open "$OUT/report.html"
```

`evidence.json` is the raw tier — grep it, never read it whole; `digest.md` is the drill tier (190 KB
at 60 email threads — never read it whole either). `raw/` keeps the fetched pages/traces
so a re-run is offline — but every re-run rewrites `evidence.json` (new `collected_at`) and therefore
the sha the validator binds `suggestions.json` to; the validator's error line prints the current one.

## Where the questions come from (collect picks automatically)

| Source | When | Corpus | Link per conversation | Reply provenance |
|---|---|---|---|---|
| `email_runs` | the project has email runs in the window (`rc fleet runs --kind email`) | `rc run trace --stream` header: inbound `question`, agent `draft`, prior messages; roles from sender vs mailbox domains, `unknown` when neither (a sibling vendor, a CC) | tokenized run URL (human view) | `draft` — the agent's proposal, **not** a human answer; `human` when a mailbox reply precedes it |
| `helpscout` | no runs, a Help Scout mailbox is watched (`rc project mailbox ls`) | `lib.api get helpscout /conversations?embed=threads` paged in the prod workspace, spilled to `/tmp/rootcause-out` and fetched with `rc dev console file get` (console stdout caps at 64 KiB) | `secure.helpscout.net/conversation/<id>/<number>/` | `human` — the real reply (`bot` when the mailbox AI assistant answered) |
| `harvest` | `--corpus dump.txt` (a `brain-harvest` style dump) | block format `##### C1 \| … ` / `[customer\|…]` / `[message\|…]` | none → those conversations can be classified but **cannot back a suggestion** | `human` |

Recipes to add when a project needs them (say so in `learnings`): Intercom conversations
(`python -m lib.connectors.intercom list conversation …`, ten-page cap by default, no guessed inbox
URLs), Gmail/IMAP watched mailboxes without runs. `rc project mailbox harvest` refuses Help Scout.

**Roles are derived, not trusted.** `is_inbound` is delivery direction: a sibling vendor cc'd on the
thread also arrives inbound. A `later[]` turn is `customer` only when the sender matches the thread's
originating customer, `agent` when it is outbound or comes from one of the project's own mailbox
domains, and `unknown` otherwise — the validator refuses a customer quote from an `unknown` turn.
Bot furniture (widget greeting, flow prompts, auto-replies, bare salutations) is dropped entirely, and
`first_message` is the first customer turn that looks like a question (contains `?` or ≥ 8 words), never
truncated; the raw opener survives as `first_raw`.

Help Scout Beacon splits one chat into several conversations (one salon, five ids in twenty minutes):
the collector merges consecutive conversations from the same customer within an hour into the first
one (`tags: merged:<id>`), so counts are conversations, not chat fragments; the runs recipe merges
identical first messages from one sender the same way. The collector also pre-tags obvious noise
(`noise: calendar_invite | no_reply_sender | test | duplicate_outreach | empty`) as a *suggested*
`not_kb` — override it when you disagree, never skip reading the tsv line. Help-centre URLs the human
pasted in a reply become `linked_articles` on the conversation — the proof that the content exists and
discoverability failed (`wrong_title`), shown as `linked A10` in the digest header.

Article inventory: every `INDEX.md` under `/kb` (project scope: `/kb/<provider>/`; tenant scope:
only `/kb/tenant/<provider>/`), one line per article: title · keywords · summary · collection · aliases
· audience, plus `url`/`updated_at` from frontmatter; the brain's own `knowledge/` titles are listed
separately (`home: brain`) — they never receive a public-KB suggestion. An empty inventory is a hard
stop, not a footnote. Honour `audience:` when present (KnowledgeOwl mixes customer and internal docs):
an internal article does not "answer" a customer question.

## Rubric — classify every conversation, then cluster, then suggest

One verdict per conversation, all of them (the validator checks coverage), with `topics[]` — an
e-mail thread often carries two questions; list both, first = primary:

- `answered` — an article answers the question as asked (the human may even have linked it).
- `partial` — an article covers the topic but misses the asked point (a condition, an exception, a
  second screen, "does it show up on Google automatically?").
- `missing` — no article; the human answered by hand.
- `wrong_title` — the content exists but neither title, keywords nor aliases contain the customer's
  words ("bevestigingsmail" vs an article called "Reminder mail"). A discoverability hypothesis —
  there are no search analytics here, so say what the customer typed and propose those words.
- `not_kb` — do-it-for-me ("kunnen jullie … wissen"), billing disputes, bugs, feature requests,
  consumer-at-wrong-address (end customers writing to the software vendor), pricing/pre-sales, tests
  and internal chatter, pure acknowledgements. Not a suggestion, ever.
- `uncertain` — you could not tell without the raw thread (first reply is only "ik kijk even",
  contradictory later turns). Classify, do not suggest from it.

Only how-to and "waar vind ik" produce suggestions. Then:

1. **Cluster** by the product concept the answer hinges on, not by the symptom the customer named:
   "Nele double-books my two cabines", "the reminder mail names the wrong toestel" and "can I close a
   ruimte when it is in use" are one cluster (cabines/toestellen koppelen). One suggestion per
   cluster; give it a short slug, list it in `topics[]` of every classification row it covers, and use
   the same slug as the suggestion's `topic`.
2. **Cheapest edit first.** `add_alias` / `retitle` when the answer exists; `rewrite` (one article, one
   named section, the proposed text) when it is partial; `new` only when nothing covers it; `merge`
   only when two articles visibly confused customers; `delete` only with a destination and a reason.
3. **Rank is computed**, never typed: distinct conversations in the topic × how badly the KB fails
   (missing 3 · partial 2 · wrong_title 1). A single-conversation `missing` topic can still make the
   top five when the answer is reusable — the validator only warns.
4. **Seed text from the human reply** (`seed_reply`) when it was a good, reusable answer; the owner
   writes the article in the salon's words, not yours. When the human explained the mechanism but
   not the menu path, still propose the article and leave the path as `[[pad: …]]` for the owner —
   do not drop a proven gap because you cannot invent a screen. Draft provenance (`email_runs`) is
   never a seed.
5. **Quotes are verbatim customer words** — a contiguous substring of the first message (or
   `first_raw`) or a later `[customer]` turn, original language, PII-free by choice of substring
   (never edited). `[unknown|…]` turns (a CC'd partner, a sibling vendor) and `[agent]` turns are
   refused by the validator.
6. **Contradictions count.** Two live articles that disagree (credit "valid indefinitely" vs "valid
   two years") are a `rewrite` even when no customer named the contradiction — evidence is any
   conversation that touches the topic; say which article the brain/humans confirm as right.
7. **Route**: `kb` for the help centre; `brain` when the real fix is a brain playbook or persona rule
   (the KB is fine, the agent ignored it) — hand those to
   [`brain-dream-cycle`](../brain-dream-cycle/SKILL.md) instead.

Drill before you write: open the bodies of every article you target or claim "partial" for; the index
summary alone does not prove coverage. Stop drilling when more detail would not change the edit.

## Data semantics — how to not be wrong

- **Creation-window bias.** Conversations are selected on `created_at`; follow-ups on older threads
  are not in the corpus. Say so when a topic looks thin.
- **A draft is not an answer.** The collect summary prints how many conversations carry a human
  reply — on a draft-mode project that is often 1 in 40, so expect to write article text from the
  question plus the article bodies, not from `seed_reply`. In `email_runs` the reply is the agent's proposal; whether the human
  changed it lives in `rc fleet runs` deltas ([`rc-fleet`](../rc-fleet/SKILL.md)). Use it to
  understand the question, not as ground truth for the article text.
- **Chat transcripts** (Help Scout Beacon) are short, multi-turn and often start mid-thought; read
  the later customer turns before classifying.
- **Absence of questions proves nothing** about an article being unused — never suggest `delete`
  from silence.
- **Coverage first.** A feed at `partial`/`unavailable` goes into the `headline`; no claim rests on the
  missing part. `coverage[].reason` spells out where the other runs went (other tenant, same thread,
  duplicate mail, no inbound text) — quote it rather than the bare `63/121`.

## Privacy

First names or handles only, no e-mail addresses, no phone numbers — the collector strips them, keep
it that way when you quote. `evidence.json`, `raw/` and `report.html` stay local; deliver the report by
attaching it where the owner already works (ticket, chat), never by committing it.

## Meta-learning — close every run with what it taught the skill

`suggestions.learnings[]` (0–5, each `observation` → `proposed_change` → `target`: rubric / recipe /
normaliser / validator / render) renders into `learnings.md`. Promote the reusable, anonymised ones into
this file's iteration log and the code; project quirks stay in the brain's notes. The first three runs
(iBeauty, pro-backup, kampadmin) are expected to reshape the rubric.

## More

[suggestions_schema.md](suggestions_schema.md) the one file you write · [`brain-fleet-report`](../brain-fleet-report/SKILL.md)
the same collect → judge → render pattern for a day of runs · [`rc-debug`](../rc-debug/SKILL.md) one
run in full · [`rc-script-wrapper`](../rc-script-wrapper/SKILL.md) console artifacts and typed failures ·
[`prod-console`](../prod-console/SKILL.md) `/kb` and connector reads · [`brain-harvest`](../brain-harvest/SKILL.md)
where a `dump.txt` corpus comes from · [docs/knowledge-base.md](../../docs/knowledge-base.md).

## Iteration log

- **2026-09-08 round 2** — three real runs (iBeauty Help Scout · pro-backup email_runs + Intercom
  KB · kampadmin email_runs + tenant Intercom KB). Collector was the weak layer: agent replies from a
  sibling vendor labelled `[customer]` (roles now sender-vs-mailbox-domain, else `unknown`, refused as
  quotes) · tenant-scoped console never passed `--tenant` so 0 articles slipped through with exit 0
  (now `--tenant`, hard exit, one report per help centre) · INDEX discovery via `find`, read through
  the artifact path (64 KiB stdout cap truncated 192 articles) · `linked_articles` matches any
  article url · bot greetings skipped as reply · question-turn selection with `first_raw` · runs
  merged on sender+text · noise pre-tags · `topics[]` · `conversations.tsv`/`articles.tsv` as the
  read-whole tier. Second judge pass on the same three: replies from the mailbox's AI assistant
  (`operator+…@intercom.io`) were `human` → now provenance `bot`, never a seed; `linked_articles`
  came from drafts too → human replies only; Calendly bodies → `calendar_invite`; article ids now
  follow path order (stable across runs); Beacon `note` threads (IP, browsing history) dropped
  before `raw/` is written; contradictions between two live articles are a `rewrite`. Judge time:
  9–20 min per project. Unverified judge claim: some Beacon chats arrive with a preview-truncated
  body ending in `(...)` — treat such a quote as suspect.
- **2026-09-08** — built (collect/corpus/schema/validate/render). First real run: iBeauty (Help Scout
  recipe, 9-day window, 87 → 78 conversations after chat-fragment merge, 66 articles, 12 suggestions).
  Learnings applied: merge split Beacon chats · `linked_articles` from pasted help-centre URLs ·
  keep 12 later turns · absence-grep before opening bodies · console drill needs `-o json
  --raw-output | jq -r .stdout` · cluster on the product concept, not the symptom. Judge misses seen
  on the first pass: split one concept over two topics (cabine), called a wrong-title case
  `answered` because the human linked the article — the pasted link is the wrong-title signal.
