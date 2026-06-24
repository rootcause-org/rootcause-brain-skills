# `rc` — the project's self-service window into its own rootcause data

`rc` (repo: **`rootcause-org/rootcause-cli`**) is a thin Go CLI that lets a **project consume its OWN
rootcause data and change its own config** — over rootcause's public JSON `/api/v1`, authed with an
**OAuth access token** (`rc login`). No business logic lives in it (MCP is a planned layer over the same
endpoints); it's a typed, paginating, TTY-aware front-end to the API. The **token's scope is the
dual-audience switch**: a project-scoped token sees only that project; a global admin's all-projects
token sees the whole fleet — the SAME commands serve both.

In a brain checkout, use `rc` instead of RootCause MCP unless the user explicitly asks for MCP. MCP is
the end-consumer/app-facing plane; `rc` is the brain-dev/observability plane.

> **Why it's documented in this kit.** This repo is the *customer-world-facing, infra-free* brain
> tooling — the litmus test ([AGENTS.md](../AGENTS.md)) is "does it touch OUR host?". `rc` does **not**:
> it speaks the public API with the project's own OAuth token, so it's the **project-dev's read-side
> counterpart** to the operator-only host-debug tools (`db.py`, `logs.py`) that stay in `rootcause`. A
> dev with no operator/SSM access can still ground themselves in real runs. `rc` lives in its own repo;
> this page is where the **author→verify loop** that uses it is taught.

## Commands (progressive disclosure: index → one run → detail)

```bash
rc ask "<customer-style question>"      # trigger a REAL prod run, wait for the answer; prints the run_id   (POST /api/v1/runs)
rc ask "<q>" --brain-ref dev/x          # …against a pushed dev/* branch — NO main push, main stays live
rc ask --project dentai "<q>"           # outside a brain: all-projects admin token selects a project
rc run <id>                     # one run, high level: status, category, draft?/note?, cost, duration (+ kind/outcome/turns/bash/created/finished/trace)
rc run <id> --events            # full detail: per-event trace — bash command + stdout/stderr, exit code, timing
rc run <id> --brain-diff        # the journal commit this run wrote to the brain (SHA + files + diff)  (GET …/brain-diff)
rc run <id> --debug             # decompose the run → rc-debug/<run8>-<project>.{md,jsonl} (index + jq-able log)
rc run <id> --full -o json      # the whole run-dump BUNDLE ({run, events}) — what brain_dump.py renders  (GET /api/v1/runs/{id}/full)
rc runs [--limit N] [--kind email|prompt|mcp|analysis] [--category ok|timeout|...]
rc status                       # health summary + recent runs                          (GET /api/v1/runs)
rc fleet [--days N] [--kind …]  # fleet digest: per-run flag table + rates + worst offenders (--format agent for token-lean)
rc patterns [--days N]          # cluster recent failures (bash fails + blocked egress) into ranked patterns
rc health [--hours N]           # mirror + dead-letter health; EXITS NON-ZERO when unhealthy (cron/CI usable)
rc thread <id>                  # rootcause-side trace of one thread/session: runs, what the agent did, callback delivery
rc config get                   # effective settings + box defaults
rc config set max_run_usd=5 default_tier=pro
rc env keys                     # key NAMES of the project's PRODUCTION grounding .env (log-safe)  (GET /api/v1/env)
rc env pull                     # write that env to a 0600 ./.env (so brain-dev --live can run grounding locally)
rc env diff                     # names-only drift: local ./.env vs the server (nonzero exit on drift)
rc login                        # OAuth sign-in (browser PKCE; --device for headless) — token stored 0600, per profile
rc whoami                       # which project/tenant will rc hit from here, and why (brain binding + sign-in status)
```

- **Scope is automatic, and it's the audience switch.** Main intent: a brain checkout chooses the
  project context; the profile only chooses which local token to use. `rc` first tries a profile with
  the brain project's name; if absent, it uses `default` and sends the brain project as `?project=` on
  supported endpoints. Use explicit `--project <id-or-name>` when outside a brain checkout or overriding
  it, and `--all` for fleet digests. A project-scoped run-UUID lookup 404s other projects' runs (no
  existence leak). `rc whoami` shows the resolved local binding.
- **Every command has `-o json`** for scripting (`rc runs -o json | jq …`); the thin endpoints return
  raw rows, so `-o json` is a verbatim passthrough you can roll up yourself.

- **`rc ask` is the high-fidelity loop test.** It runs the *real* prod loop (model, egress, `/brain:ro`,
  `/mirrors`, KB) — `--brain-ref dev/x` fetches a pushed `dev/*` branch so a brain change is tested on
  real infra **without** moving `main` (which is live). The run is **side-effect-free**: no callback,
  no journal push, and any proposed action/PR is flagged `test`. Dump it with the `brain-dev` skill's
  [`brain_dump.py`](../skills/brain-dev/SKILL.md#test-a-brain-change-on-real-prod-infra--without-pushing-main-rc-ask--brain_dumppy)
  (`rc run <id> --full` → the shared `run_dump` renderer → an index `.md` + jq-queryable `.jsonl`).

- **Output is TTY-aware** — pretty table on a terminal, **JSON when piped** (`rc runs | jq …`); force
  with `-o json|table`.

### Auth — OAuth, and the brain checkout selects the project (no `--profile`, no env wrangling)

`rc` is **OAuth-only**. `rc login` runs the browser PKCE flow (`--device` for a headless/SSH box) and
stores the tokens in **`~/.config/rootcause/tokens.json` (0600)**, keyed per *profile*; every later `rc`
refreshes the access token transparently. **There is no API key in any file** — the old
`.rootcause.secret.toml` / `ROOTCAUSE_API_KEY` are gone. The **scope** (one project, or — for an
admin — all projects) is chosen on the **browser consent screen**, not the CLI.

A brain repo **is** one project, so `rc` binds to it by convention via one committed, non-secret marker:

| File | Committed? | Holds | Role |
|---|---|---|---|
| **`.rootcause.toml`** | ✅ yes | `project = "<slug>"`, `base_url = "…"`, optionally `tenant = "<slug>"` | the binding — ships with the clone, so the project + endpoint (+ tenant) are known out of the box. It carries **no secret**. |

Run `rc` anywhere inside a brain clone and it auto-targets *that* project. Resolution (per field; an env
var always wins as a one-off override):

```
explicit --profile <name>          → that profile's stored token (no brain profile fallback)
inside a brain + project token     → profile named by .rootcause.toml project
inside a brain + no project token  → default profile + .rootcause.toml project as ?project=
outside any brain                  → default profile / built-in default
base_url:  ROOTCAUSE_BASE_URL > .rootcause.toml base_url > [profiles.<name>] base_url > built-in default
```

`rc whoami` shows what it resolved (profile · project · tenant · signed-in?) — locally, no server call.
`rc env pull` / `ask` / `run` all honor the same binding.

`--project <id-or-name>` is **not** a token/profile selector. It is a server-side `?project=` selector
for supported endpoints. Use it with an all-projects admin profile when you want to act on one project
from outside its brain checkout, or to override the checkout's project:

```bash
rc ask --project dentai --tenant belgium-staging "Run the real loop for this question"
```

A pinned project token ignores `--project` server-side; it cannot widen to another project.

**Onboard a brain (incl. an external customer who just cloned):**

```bash
git clone …/rootcause-brain-<project> && cd rootcause-brain-<project>   # .rootcause.toml already inside
rc login            # opens a browser; pick the project (or all-projects, if admin) on the consent screen
rc whoami           # confirms: profile, project, base URL, signed-in
rc ask "…"          # just works — no --profile, no export
```

The committed `.rootcause.toml` carries `base_url`, so a customer hits the right endpoint with zero env
setup; the OAuth token they mint on the consent screen is scoped to their project. A headless box uses
`rc login --device` (a short code approved in any browser). A superadmin who already has an all-projects
token in `default` does not need to log in per project; `rc whoami` will show `profile=default` plus the
brain's project.

**Tenant brains.** A delta repo over a tenant-enabled project (e.g. a single clinic under DentAI) adds
a `tenant` field to its marker — `project = "dentai"`, `tenant = "de-kies"`. `rc` then defaults
`--tenant` for `ask`/`env`/`whoami` to that tenant, so the checkout resolves the **project ∪ tenant**
scope without repeating the flag.

## Install

**No Go needed** — grab a prebuilt binary (cross-compiled per release by GoReleaser).

```bash
# Homebrew (macOS/Linux):
brew install rootcause-org/tap/rc

# Or a prebuilt binary: pick your OS/arch on the releases page, then (macOS arm64 example —
# substitute the real version + your arch from the asset you downloaded):
curl -sSL https://github.com/rootcause-org/rootcause-cli/releases/latest/download/rc_<ver>_darwin_arm64.tar.gz \
  | tar -xz && sudo mv rc /usr/local/bin/ && rc --version
# macOS Gatekeeper may quarantine the unsigned binary: xattr -d com.apple.quarantine $(which rc)

# Go devs:
go install github.com/rootcause-org/rootcause-cli/cmd/rc@latest
```

Binaries + the Homebrew formula are published per `vX.Y.Z` tag (see the
[releases page](https://github.com/rootcause-org/rootcause-cli/releases)). Cut a release with
`scripts/release.sh patch|minor|major` from the `rootcause-cli` repo.

## The author → verify loop — ground in real runs *before* you write an action

This is the headline `rc` unlocks, and the standard this repo now teaches: **don't author an action
(or any brain change) blind — verify against real data first.** Before you write or change an
`actions/<id>/`, inspect exactly what the agent actually did on real cases, then author from evidence.

```mermaid
flowchart LR
    F["find<br/>rc runs --limit 20<br/>(filter --kind/--category)"] --> I["inspect<br/>rc run &lt;id&gt; --events<br/>per-event: bash command + stdout/stderr"]
    I --> A["author<br/>edit manifest.yaml + script.rb<br/>params + description from evidence"]
    A --> V["verify (ship loop)<br/>push → sync → propose → execute"]
    V -- "iterate on real ❌/❓ runs" --> F
```

1. **Find relevant cases** — `rc runs --limit 20`, narrowing with `--kind` / `--category`, to surface
   the real runs your action is meant to handle (e.g. the timeouts, the refunds, the failures).
2. **Inspect what the agent did** — `rc run <id> --events` shows the full per-event trace: each tool
   call with its exact bash/grounding command, its stdout/stderr, plus exit code and timing (and the
   reply's draft/note markers). This is the ground truth for *which params the action needs* and *what
   its `description` must say* so a future run reaches for it.
3. **Author from evidence** — only now edit `actions/<id>/{manifest.yaml,script.rb}`. The param schema
   and `description` are shaped by what you saw, not by a guess.
4. **Verify it's live and works** — push → sync → propose → execute, the loop in
   [`ship-and-verify.md`](../skills/brain-dev/ship-and-verify.md) (and the concept in
   [`actions.md`](actions.md)). `rc run <id> --events` is also how you read back the run you triggered
   in *Mode A* ("did the agent reach for the action, with the right params?") **without** operator host
   access.

The same verify-first discipline applies to **value/env conventions**: `rc config get` shows the
effective settings + box defaults you're authoring against (e.g. `max_run_usd`, `default_tier`), so you
tune config to what's actually live rather than to assumptions.

## Sync the grounding env (`rc env`)

A brain's grounding scripts read their credentials (the `*_DSN`s, API keys) from a **gitignored
`./.env`** at the brain root. `rc env` lets a **project dev self-serve** that env — the same role the
operator-only `scripts/rc_env.py --pull` plays, but over your **OAuth token** instead of AWS/SSM, so
no operator access is needed:

```bash
rc env keys                 # what keys exist (NAMES only — safe to paste/log)
rc env pull                 # fetch the PRODUCTION grounding .env → write 0600 ./.env
rc env diff                 # has my local ./.env drifted from prod? (names-only; exit≠0 on drift)
rc env pull --tenant <slug> # tenant-enabled project (e.g. dentai): the project ∪ tenant env a run sees
```

Pull it once and `brain-dev`'s **`--live`** tier can run grounding scripts against real prod data
locally. **Secret hygiene:** no subcommand ever prints a secret VALUE (`pull` writes them only to the
0600 file; `keys`/`diff` are names-only). The pulled `.env` holds **real production secrets** on your
laptop — it's gitignored in every brain; treat it like a password file.

## Related

- [`actions.md`](actions.md) — the action plane + the author→test loop (`rc` is the *ground-first* step
  that precedes it).
- [`ship-and-verify.md`](../skills/brain-dev/ship-and-verify.md) — the outer push→sync→feedback loop;
  `rc run <id> --events` is the project-dev way to read a triggered run's trace.
- [`brain-dev` SKILL](../skills/brain-dev/SKILL.md) — the local, read-only diagnosis counterpart.
