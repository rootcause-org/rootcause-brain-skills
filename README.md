# rootcause-brain-skills

One external-developer kit to iterate on a project's **brain** locally and verify production behavior
through public `rc`/API surfaces. No RootCause private source, host credentials, SSM, or infrastructure
shell is required.

A brain is `rootcause-org/rootcause-brain-<project>`: committed markdown knowledge, playbooks,
grounding scripts, tests, projection templates, and optional actions. Production mounts committed
brain refs read-only at `/brain`; local `.env`, installed skills, and `.rootcause/` artifacts never
reach a run. Source mirrors (`/mirrors/<name>`), optional knowledge-base sync (`/kb`), live grounding
data, and actions are separate runtime inputs. Start with [docs/brain-model.md](docs/brain-model.md).

## Install

From a brain checkout:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/main/install.sh)
SKILL="${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}/skills/local-brain-work"
uv run "$SKILL/scripts/brain_run.py" --brief
uv run "$SKILL/scripts/brain_test.py" --live
```

Other paths:

- Claude Code plugin: `/plugin marketplace add rootcause-org/rootcause-brain-skills`, then
  `/plugin install brain-dev`.
- Codex plugin: `codex plugin marketplace add rootcause-org/rootcause-brain-skills`, then
  `codex plugin install brain-dev`.

Full walkthrough: [docs/onboarding.md](docs/onboarding.md).

## Canonical Skills

| Skill | Job |
|---|---|
| `local-brain-work` | Map a brain, run local/live/docker tests, projection, mirror checks, and action dry-runs. |
| [`brain-dream-cycle`](skills/brain-dream-cycle/SKILL.md) | Full local dreamcycle pass from a brain checkout: mine feedback/sent deltas/patterns with `rc`, then update brain files plus persona/triage settings. |
| [`brain-harvest`](skills/brain-harvest/SKILL.md) | Full local harvest pass: trigger a production sent-history export, download the cleaned corpus, fan out per-topic subagents to distil patterns (privacy-linted), decide brain/persona/triage homes, verify, publish, then delete the corpus. |
| `brain-ask` | Last-mile production-loop `rc ask` validation, usually against a pushed `dev/*` ref. |
| `rc-debug` | One run/thread/session to trace/debug/index/JSONL drilldown; analysis-first before edits. |
| `rc-health` | Stale mirrors plus dead-lettered runs. |
| `rc-fleet` | Recent fleet digest plus recurring failure patterns. |
| `brain-dev-upgrade` | Update local kit and `rc` CLI. |
| `brain-publish` | Post-edit `rc brain status/sync`, promote/support-request step using public surfaces only. |

Older duplicate entrypoints are not shipped; use the canonical skills above.

## Side Effects

Diagnosis is read-only by default. Test-run creation and action execution are explicit exceptions.
Details: [docs/side-effects.md](docs/side-effects.md).

| Surface | Side effect |
|---|---|
| `brain_run.py`, `brain_test.py`, `rc run`, `rc fleet`, `rc health`, `rc thread` | Read-only. |
| `rc mailbox harvest` | Creates a production export job (provider sweep → stored corpus). |
| `rc export download` | Marks the export consumed (eviction grace) and lands raw mail on local disk. |
| `rc ask` against `main` | Creates a real production run; may create draft/journal/test artifacts and bill usage. |
| `rc ask --brain-ref dev/<branch>` | Creates a test run; no callback or durable journal push; proposals are test artifacts. |
| Action proposal | LLM proposes only; no mutation. |
| Action confirmation / public dev-trigger when exposed | Real mutation path. |
| `brain_action.py --commit` | Local real write to whatever `.env.action` targets. |

## Fidelity

- `uv` mode is the fast inner loop. It pins Python/runtime deps and uses only the brain's `.env`, but
  it does not reproduce read-only mounts, container isolation, OS behavior, or production egress.
- `docker` mode uses the published workspace image and read-only mounts. It still does not prove the
  production egress allowlist.
- `rc db` / `rc bash` are the preferred production debugging path for exact SQL, scripts, logs, and
  tool parity; they are much faster than wrapping the check in an LLM run.
- `rc ask --brain-ref dev/<branch>` is the full production-loop confidence check without moving live
  refs.
- `rc brain sync` refreshes the deployed brain cache from `origin/main` and invalidates warm `rc bash`
  workspaces.

## Docs

| Path | What |
|---|---|
| [docs/brain-model.md](docs/brain-model.md) | Audience, brain-vs-external context, prompt boundary, layout, mounts, refs. |
| [docs/run-trace-model.md](docs/run-trace-model.md) | How to read `rc run --debug` index/JSONL. |
| [docs/mirrors.md](docs/mirrors.md) | Source mirrors and freshness/debug rules. |
| [docs/knowledge-base.md](docs/knowledge-base.md) | Traverse `/kb` and committed `knowledge/`, including frontmatter search from `rc bash`. |
| [docs/support-boundary.md](docs/support-boundary.md) | Brain-change vs RootCause-support decision tree. |
| [docs/actions.md](docs/actions.md) | Action plane and local hosted-Python action tests. |
| [docs/rc-cli.md](docs/rc-cli.md) | Public `rc` CLI reference for this kit. |

## Single Version Line

The plugin versions, `rootcause-runtime` pin, workspace image tag, and production runtime pin move
together; see [RELEASING.md](RELEASING.md). Current line: **`v0.1.65`**.

- Runtime pin:
  `rootcause-runtime @ git+https://github.com/rootcause-org/rootcause-brain-skills@v0.1.65#subdirectory=runtime`
- Workspace image: `ghcr.io/rootcause-org/workspace:v0.1.65`

Check coherence:

```bash
./check-release-coherence.sh
```

## Develop

```bash
cd runtime && uv run --with . --with pytest --no-project pytest tests -q
```
