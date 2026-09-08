# `suggestions.json` — the one file you write

Source of truth: `scripts/schema.py`. Unknown keys are rejected, on this file **and** on
`evidence.json`. Validate before you render:

```bash
uv run scripts/validate.py <dir>/suggestions.json        # evidence.json is picked up next to it
uv run scripts/render.py   <dir>/suggestions.json        # validates again → report.html + learnings.md
```

Each error is one JSON path (`suggestions[3].evidence[1].quote: …`) — patch that path, never
regenerate the file. `warn:` lines are steering, not failure. Report chrome is English; your
titles, texts and quotes stay in the customer's language.

## Root

| Path | Type | Rules |
|---|---|---|
| `schema_version` | int | `1` |
| `evidence_sha256` | str | the sha256 `collect.py` printed for this `evidence.json`; the validator re-hashes the file bytes and refuses a stale pairing |
| `headline` | str | one sentence for the help-centre owner |
| `classification[]` | list | **every** conversation in `evidence.conversations`, exactly once |
| `suggestions[]` | list | one per cluster; ranking is computed, never typed |
| `learnings[]` | 0–5 | what this run taught about the skill |

## `classification[]`

| Field | Rules |
|---|---|
| `conversation_id` | an `evidence.conversations[].id` (`hs:…`, `run:…`), unique |
| `verdict` | `answered` \| `partial` \| `missing` \| `wrong_title` \| `not_kb` \| `uncertain` |
| `article_ids[]` | the `evidence.articles[].id`s you judged against (may be empty) |
| `topics[]` | short cluster slugs (`["boekingshorizon"]`), shared with every conversation in the cluster. At least one unless `verdict` is `not_kb`; **first = primary** |

`topics` is the join key: a suggestion's score and its "N conversations" line come from the
classifications listing that topic. Cluster first, then suggest.

One thread often carries two questions — list both slugs and the conversation counts once for each
topic, so neither signal is dropped. The old singular `topic` key is refused with
`classification[3].topic: unknown key — use topics: [..]`.

`evidence.conversations[].noise` is the **collector's** pre-tag (`{verdict: not_kb, reason:
calendar_invite | no_reply_sender | test | duplicate_outreach | empty}`). It is a hint, not a
verdict: you may classify the conversation any way the text supports. The reason renders as the
"noise hint" column in the detail table.

## `suggestions[]`

| Field | Rules |
|---|---|
| `id` | `S1`, `S2`, … |
| `kind` | `new` \| `rewrite` \| `retitle` \| `merge` \| `delete` \| `add_alias` — cheapest edit that closes the gap |
| `topic` | one slug; must appear in some `classification[].topics` |
| `title` | proposed title (for `rewrite`/`add_alias`: the article's title as it will read) |
| `target_articles[]` | `evidence.articles[].id`s, **`home: kb` only** — brain documents are never a public suggestion |
| `destination` | surviving/redirect article, `merge` and `delete` only |
| `section` | the section to replace, `rewrite` only |
| `text` | the proposed body, verbatim as it should be pasted |
| `aliases[]` | search phrases to add, `add_alias` only |
| `why` | one line, the owner's reason |
| `route` | `kb` (edit the help centre) or `brain` (really a brain fix — rendered with a badge) |
| `evidence[]` | 1–5 `{conversation_id, quote}` — see below (1 conversation warns, 2+ is a pattern) |
| `seed_reply` | conversation whose **human** reply seeds the article, or `null` (`draft` and `bot` provenance are refused) |

### Kind rules

| kind | targets | also required | refused when |
|---|---|---|---|
| `new` | none | `text` | any `target_articles` |
| `rewrite` | exactly 1 | `section` + `text` | 0 or 2+ targets |
| `retitle` | exactly 1 | `title` | `title` equals the article's current title (case/whitespace-insensitive) |
| `add_alias` | exactly 1 | `aliases` non-empty | an alias already sits on the article or equals its title |
| `merge` | ≥ 2 | `destination` ∈ `target_articles`, `text` | destination outside the targets |
| `delete` | exactly 1 | `destination` = another existing article | destination missing or equal to the target |

### `evidence[]`

- `conversation_id` must exist, have a non-null `url` (a conversation without one cannot back a
  suggestion), and be classified `partial`, `missing`, `wrong_title` or `uncertain`.
- `quote` must be a **verbatim** substring of that conversation's *customer* text: `first_message`
  \+ `first_raw` (the raw opening turn, present when a later turn was picked as the question) +
  later turns with `role: customer`. Only whitespace and case are normalised; do not fix typos, do
  not translate, do not stitch two sentences together.
- Turns with `role: agent` or `role: unknown` are **not** customer words — a quote that only matches
  one is refused with `matches only an unknown-role/agent turn in run:… (quote customer turns only)`.
  `unknown` means the collector could not resolve the sender; treat it as vendor text.

## `learnings[]`

`{observation, proposed_change, target}` with `target` ∈ `rubric` | `recipe` | `normaliser` |
`validator` | `render`. Rendered into `learnings.md` for the skill's iteration log.

## What the validator guarantees

Every conversation classified exactly once · every id (conversation, article, destination,
seed_reply) resolves · evidence hash matches the evidence file · quotes are real customer words
(never an agent or unknown-role turn) ·
seed replies are human, never drafts · targets are public KB articles · kind rules above ·
`schema_version`/enums/limits. Soft warnings: more than 10 suggestions, a suggestion resting on one
conversation, a partial/unavailable feed, a partial KB inventory.

Computed for you — do not type them: rank and score (distinct conversations in the topic weighted
`missing` 3, `partial` 2, `wrong_title` 1, `uncertain` 1; ties go to the cheaper kind), the tiles
(scanned, how-to, answered, partial, missing, wrong-title, uncertain, not-KB) and the top unanswered
topics. A conversation with two topics counts once in each.

## Worked example (synthetic; the real one is `fixtures/suggestions.json`)

```json
{
 "schema_version": 1,
 "evidence_sha256": "3663db08dff1d3c44d9aa27050f3992d12fdb57d6dbcfb1309dc7b91371a39bf",
 "headline": "Vier bewerkingen sluiten de gaten die klanten deze week zelf moesten navragen.",
 "classification": [
  {"conversation_id": "hs:1001", "verdict": "missing", "article_ids": [], "topics": ["boekingshorizon"]},
  {"conversation_id": "hs:1004", "verdict": "not_kb", "article_ids": [], "topics": []},
  {"conversation_id": "hs:1006", "verdict": "answered", "article_ids": ["A1"], "topics": ["openingsuren", "feestdagen"]}
 ],
 "suggestions": [
  {"id": "S1", "kind": "new", "topic": "boekingshorizon",
   "title": "Agenda openzetten voor een volgend jaar",
   "target_articles": [], "destination": null, "section": null,
   "text": "Ga naar Instellingen > Agenda > Boekingshorizon en zet de horizon op 18 maanden.",
   "aliases": [], "why": "Geen artikel legt uit hoe je een volgend kalenderjaar openzet.",
   "route": "kb",
   "evidence": [{"conversation_id": "hs:1001", "quote": "Hoe zet ik mijn agenda open voor 2027?"}],
   "seed_reply": "hs:1001"}
 ],
 "learnings": [
  {"observation": "Chatgesprekken bevatten vaak twee vragen in één blok.",
   "proposed_change": "Splits chatberichten op vraagteken voor het clusteren.",
   "target": "normaliser"}
 ]
}
```
