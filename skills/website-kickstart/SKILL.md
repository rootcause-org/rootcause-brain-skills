---
name: website-kickstart
description: Provision a new chat-editable public website for a tenant in one deterministic command — GitHub repo from rootcause-org/site-template, GitHub App access, Cloudflare Worker + Workers Builds triggers (production + branch previews), first build, rootcause role=website mirror, Browser Rendering screenshots. Also adopts an existing site repo (--adopt) and tears down a throwaway (--teardown). Use for "maak een website voor deze praktijk", "new practice site", migrating a site repo onto Workers Builds.
---

# website-kickstart — zero-click site provisioning

**Intent:** a new tenant website costs one command and no dashboard clicks; the practice then edits it
through ReplyPen chat (rootcause `features/website-editing.md`). Every step reads live state first, so a
rerun after a failure continues where it stopped. Operator laptop only (Cloudflare + GitHub admin creds).

```bash
KS=~/code/rootcause-org/rootcause-brain-skills/skills/website-kickstart/scripts/kickstart_site.py
mise -C ~/code/rootcause-org/rootcause exec -- python3 $KS <github-org> <slug> \
  --project <rc project> --tenant <rc tenant> --name "<Naam zoals op de site>" [--dry-run]
```

Repo + Worker = `site-<slug>`; live `https://site-<slug>.rootcause-sites.workers.dev`, previews
`https://<branch>-site-<slug>.rootcause-sites.workers.dev`. Output: `✓/✗ step — detail` per step, then a
summary (repo, URLs, worker tag, trigger uuids, `--out` files: `<slug>-triggers.json`,
`<slug>-mobile.png`/`-desktop.png`, default `~/Downloads`) and a chat sentence to try. ~2 min.

| Step | What | Notes |
|---|---|---|
| repo | `POST /repos/rootcause-org/site-template/generate`, fill `__WORKER_NAME__`/`__SITE_NAME__`/`__PROJECT__`/`__TENANT__`, push main | `--adopt`: existing repo, only checks `wrangler.jsonc` name = Worker |
| github apps | add repo to `rootcause-app` + `cloudflare-workers-and-pages` on the org | ids via `GET /orgs/<org>/installations` (gh login); `PUT /user/installations/{id}/repositories/{repo}` with the PAT. A missing install = one-time browser step |
| worker | first `bash build.sh && wrangler@4 deploy` creates the Worker | Builds triggers need its `tag` |
| workers builds | repo connection upsert, the account's build token, 2 triggers | prod `main` → `npx wrangler@4 deploy`; preview `*` minus `main` → `npx wrangler@4 preview`; `bash build.sh`, build cache on. Drift is PATCHed |
| first build | `POST /builds/triggers/{prod}/builds`, poll, GitHub check-run `Workers Builds: <worker>`, live 200 + name | failed build → logs saved to `--out` |
| rootcause mirror | `rc project repo add name=site role=website live_url preview_url_template deploy_check="Workers Builds"` + `dev mirror refresh --expect-sha` | `--mirror-name` if the tenant has another `site` |
| screenshots | Browser Rendering, 390 px + 1280 px full page | 429 = free-tier rate limit, retried |

**Env** (`~/.config/mise-env/rootcause-org/rootcause.env`, hence `mise -C …/rootcause exec`):
`CF_SITES_USER_TOKEN` (USER-scoped; the Builds API rejects account tokens with 12006),
`GH_SITE_PROVISIONING_PAT` (classic, `repo` + `read:org`), `CF_BROWSER_RENDERING_TOKEN`; plus a `gh`
login that owns the org, `rc` login, `pnpm`. Account default: Rootcause Sites `60c5feed78b351b48868d3a691184c4b`.

**After:** fill the `[placeholders]` via chat (never invent facts); domain/DNS move → the project's site
runbook (DentAI: `dentai/.agents/skills/practice-websites/SKILL.md`), then
`rc … project repo set site live_url=https://www.<domain>` and `url:` in `_data/site.yml`.

**Throwaway:** `--teardown site-<slug>` removes the rc mirror, Cloudflare triggers + connection + Worker,
App access, then renames the repo to `site-<slug>-teardown-<ts>` and archives it (our tokens lack
`delete_repo`; it prints the delete command). Tests: `uv run --no-project python -m unittest
skills/website-kickstart/tests/test_kickstart.py` (fake GitHub/Cloudflare/rc world).
