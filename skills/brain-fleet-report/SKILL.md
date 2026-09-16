---
name: brain-fleet-report
description: "Investigate daily fleet evidence and publish technical/owner review queues. Use for fleet reports, dagrapport, or what to fix today."
---

Collect → drill → judge → validate → publish in the brain. Read
`_internal/fleet-report/OVERLAY.md`, [overlay.md](overlay.md), [schema](report_schema.md) and
[evidence semantics](evidence.md). Requires `rc` login; operator DB uses
`RC_HOST_CHECKOUT` (default `~/code/rootcause-org/rootcause`); DB scripts accept `--dsn` for tests.
Investigation never runs actions, `rc ask`, or implementation edits.

```bash
FR="$PWD/.agents/skills/brain-fleet-report"
uv run "$FR/scripts/collect.py" --date YYYY-MM-DD
# Read OUT/digest.md; drill evidence.
uv run "$FR/scripts/drill.py" --date YYYY-MM-DD --run RUN_ID
# Write OUT/report.json; copy collector kpis/manifest verbatim.
uv run "$FR/scripts/validate.py" "$OUT/report.json" --evidence "$OUT/evidence.json"
uv run "$FR/scripts/publish.py" "$OUT/report.json" --write
```

Before production writes, verify migrated review columns exist; otherwise use `--dsn` locally.
Print the owner/technical review URLs. Members use their project sessions; tenants
use tenant owner sessions; technical twins remain on project sessions. No ClickUp. After a human decision, use
the host checkout's `.agents/skills/review-implement/SKILL.md`: commit, ship, production-test.

1. Require an actionable remedy/decision or bounded investigation. Recovery/by-design is context;
   zero findings is valid.
2. Rank findings; one remedy per finding, stated once. Headlines add context.
3. Reuse Postgres prior signatures exactly; otherwise stable `<kind>:<slug>`.
4. `unchanged` inherits text/ask/prompt; counts alone do not. `changed` supplies full fields
   and delta. Owner/both supply 1–4 real options, including unchanged.
5. Fixes name diagnosed edits; investigations name checks/stop/output. Open every target path today.
   Decisions precede implementation; implementation owns verification, at most two fix/ship/test rounds.
6. Owner = reachable task, in the owner language, named recipient. Use Owner reach;
   shared findings need a distinct owner ask. Only brain-content fixes expose prompts.
   Owner asks about one practice/tenant use tenant scope (FAQ/profile/master data included);
   project scope is for asks crossing tenants. Supply title_nl in the owner language.
   Owner language = digest "Owner language" line / `manifest.owner_lang` (a tenant-scoped finding
   uses its `owner_lang_by_tenant` entry); the host resolves it, never assume Dutch.
   Ask one direct question or concrete task naming each fill-in item. Options name owner actions
   (“Prijzen invullen”, “Regel bevestigen”); admin instructions are fill-in templates, decisions
   contain concrete policy text. Never “Done in dashboard” or bare Ja/Nee/Ok.
   Drill writes details/excerpts-<uuid>.json via evidence_excerpts(run_id): shorten those originals
   into evidence.entries, stripping greeting/footer with … omissions. Include manual feedback only
   from the feedback feed; omit unavailable fields. Keep evidence.json beside report.json.
7. Aim ≤5 expanded technical, ≤4 owner findings; prioritize customer impact. Counters own numbers.
8. Honour DB decisions: accepted/noise reopen only on retest trigger; later after its date; post-fix
   sightings become regressions. Unpublished runtime drift needs a remedy; tooling drift stays parked.
9. First names only; no credentials/contact details. Prompt run URLs are canonical and token-free.
