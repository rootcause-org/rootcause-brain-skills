---
name: brain-helpcenter-publish
description: "Apply one approved help-centre suggestion to the live provider (Help Scout Docs / Intercom) from a brain checkout: save the suggestion's `replypen: helpcenter/v1` block to a file, dry-run it, then `rc project knowledge article apply` writes the article as a draft and re-syncs /kb. Use for 'publiceer dit artikel in het helpcenter', 'apply this help-centre suggestion', 'update the Help Scout / Intercom article', 'create the KB article we agreed on'."
---

# brain-helpcenter-publish — the help-centre write path

The last mile of [`brain-helpcenter-suggestions`](../brain-helpcenter-suggestions/SKILL.md): each
suggestion card already carries a `replypen: helpcenter/v1` markdown block, and **that block is the
input** — no retyping, no provider UI. One command applies it. RootCause does the provider write
host-side with a typed body (your markdown → provider HTML), **never publishes silently** (create =
draft, update on a published article needs `--publish`), and re-syncs `/kb` so the next run grounds on
what you just wrote. The block contract lives in the suggestions skill; this file is the apply logic.

This is a **side effect** ([docs/side-effects.md](../../docs/side-effects.md)): `apply` without
`--dry-run` writes to the customer's live help centre. The owner decides *what* ships; you only run
the command they approved.

## One-time grant

The write path needs its own sealed credential — the read connector used by `/kb` sync is not enough:

```bash
rc project connection add integration_key=helpscout_docs label=help-center tier=write token=<token>
rc project connection ls        # the row shows `tier: write`
```

| Provider | `integration_key` | Token |
|---|---|---|
| Help Scout Docs | `helpscout_docs` | Docs API key — Help Scout **Manage → API keys** (Docs, not Mailbox) |
| Intercom | `intercom` | Access token with **Articles write** |
| KnowledgeOwl | `knowledgeowl` | **Not supported yet** — `UNSUPPORTED_PROVIDER`; the owner edits in the UI |

Sealed like an action credential ([docs/secrets.md](../../docs/secrets.md)): never in the brain, never
in a run — write-tier rows are host-only and are never injected into a workspace. Missing grant ⇒ the
verb refuses with `NO_WRITE_GRANT` and names the exact `rc project connection add …` line above.

## Recipe

```bash
cd ~/code/rootcause-org/rootcause-brain-<project>
mkdir -p .rootcause/helpcenter/apply

# 1. save the card's block verbatim (front matter + body) — prose above the first `---` is ignored
pbpaste > .rootcause/helpcenter/apply/reminder-mail.md

# 2. rehearse: prints op/changes and the exact provider request(s) that WOULD be sent
rc project knowledge article apply --from .rootcause/helpcenter/apply/reminder-mail.md --dry-run

# 3. apply for real (still a draft on create / Help Scout update)
rc project knowledge article apply --from .rootcause/helpcenter/apply/reminder-mail.md

# 4. read it back — prints the article as a fresh `op: update` block
rc project knowledge article get --provider helpscout --id 5f1a… > .rootcause/helpcenter/apply/reminder-mail.md

# 5. go live only when the owner said so
rc project knowledge article apply --from .rootcause/helpcenter/apply/reminder-mail.md --publish

# tenant-scoped project: one help centre per tenant
rc --tenant <slug> project knowledge article apply --from <file> --dry-run
```

**Always dry-run first and read the printed request** — it is the only place the rendered HTML and the
target url are visible before the write. `get` is also how you build an update block from scratch when
there is no suggestion card: fetch it, edit the title/body, keep the front matter as-is.

Scratch stays under the gitignored `.rootcause/`; article text is customer-facing content, not brain
content — never commit these files into the brain.

## What an op does per provider

| | Help Scout Docs | Intercom |
|---|---|---|
| `op: create` | new article, status `notpublished` (draft) | new article, state `draft` |
| `op: update`, article is a draft | updates the draft | updates the draft |
| `op: update`, article is **published** | writes the **draft lane** — the live article is untouched, `draft_saved: true`; `--publish` publishes | refused with `PUBLISHED_ARTICLE` until you pass `--publish` |
| no effective change | `changed: false` — nothing sent, no resync, no audit row | same |

Anchors (update only, exactly one): `old:` replaces that verbatim block in the current body, `after:` /
`before:` splice the body around that verbatim line. Matching is against the current body rendered the
way `/kb` renders it, verbatim after trimming per-line trailing whitespace — **no fuzzy apply**: not
found, or found twice, is an error naming the anchor. No anchor + empty body = title/keywords-only
edit; no anchor + body = whole body replaced. `create` with an anchor is an error.

## Body markdown — write it in the round-trip canon

The host renders the body to provider HTML; the next `/kb` sync renders that HTML back to markdown,
and anchors must match *that* text. So write bodies the way `/kb` files look: `- ` bullets,
`**bold**` / `*italic*`, ATX headings, one line per paragraph (no soft wraps), an image alone on
its own line. **Not supported** (lossy on the way back, so an anchor can never target it): GFM
tables, `~~strikethrough~~`, raw HTML — use lists or a heading-per-row instead of a table.
**Intercom headings:** Intercom stores the top heading level you use as `#` (a body whose largest
heading is `##` comes back with `#`/`##`). The host promotes the levels the same way before it
diffs, so re-applying is still a no-op — but write Intercom bodies with `#` as the top level so the
block you keep matches what `get` returns.

## Manual kinds

`op: manual` (and a card with no `op`) is **not applyable** — the verb refuses in one line. Merge and
delete are manual by design: they destroy or redirect existing URLs, so the owner does them in the
provider UI and the card is the instruction sheet.

## Where a write shows up

Every real write is an action run: `rc fleet actions --action helpcenter.article.update` shows one row
per write with the grounded params (provider, article id, title, `changes[]`) and its originating
run/audit link — never the token ([`rc-fleet`](../rc-fleet/SKILL.md)). The response's `audit_id` is
that row. A changed write also queues a `/kb` resync (`resync.queued`); confirm the new text landed
with [`prod-console`](../prod-console/SKILL.md) (`rc dev console bash run 'rg -n "…" /kb'`) before
telling the owner grounding is current.

## Errors worth recognising

`INVALID_BLOCK` (parse/validate — the message is the one-line reason; an unknown front-matter key is a
typo, not a feature) · `ARTICLE_NOT_FOUND` (wrong `id`/provider, or the article was deleted) ·
`PUBLISHED_ARTICLE` (re-run with `--publish`) · `NO_WRITE_GRANT` (add the connection above) ·
`UNSUPPORTED_PROVIDER` (KnowledgeOwl) · `PROVIDER_ERROR` (upstream — retry once, then report; do not
loop). Needs `rc` ≥ 1.26.0.

## Close-out

Report the article **url**, its **status** (draft / published, and whether a Help Scout draft lane was
used), and the **changes** that were applied — plus the `rc fleet actions` row and whether the `/kb`
resync was queued. Distinguish a dry-run from a real write, and a draft from a live change.

## More

[`brain-helpcenter-suggestions`](../brain-helpcenter-suggestions/SKILL.md) produces the block ·
[`prod-console`](../prod-console/SKILL.md) `/kb` reads · [`rc-fleet`](../rc-fleet/SKILL.md) write
history · [docs/secrets.md](../../docs/secrets.md) · [docs/knowledge-base.md](../../docs/knowledge-base.md).
