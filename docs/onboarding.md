# Client Onboarding

From zero to iterating on a brain with public tooling only.

## 1. Enter A Brain

```bash
cd ~/code/rootcause-org/rootcause-brain-<project>
```

Need `.env` for local live checks?

```bash
rc login
rc whoami
rc env pull
```

`rc env pull` writes a `0600` gitignored `.env` using your OAuth token. It does not print secret
values.

## 2. Install The Kit

Local gitignored install, recommended:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/main/install.sh)
```

This creates/updates one shared clone at `~/.rootcause-brain-skills`, symlinks shipped skills into the
brain's gitignored `.agents/skills/` and `.claude/skills/`, and keeps the kit out of committed `/brain`.

Plugin installs:

- Claude Code: `/plugin marketplace add rootcause-org/rootcause-brain-skills`, then
  `/plugin install brain-dev`.
- Codex: `codex plugin marketplace add rootcause-org/rootcause-brain-skills`, then
  `codex plugin install brain-dev`.

Update later with `brain-dev-upgrade`.

## 3. Run Local Checks

```bash
SKILL="${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}/skills/local-brain-work"
uv run "$SKILL/scripts/brain_run.py" --brief
uv run "$SKILL/scripts/brain_run.py" skills/databases/scripts/lookup_customer.py --email a@b.com
uv run "$SKILL/scripts/brain_test.py"
uv run "$SKILL/scripts/brain_test.py" --live
uv run "$SKILL/scripts/brain_test.py" --mode docker --live
```

Docker mode needs Docker/colima and pulls `ghcr.io/rootcause-org/workspace:<tag>`. It covers image,
deps, read-only mounts, and container isolation; it does not prove production egress allowlists.

## 4. Verify Production Behavior

```bash
rc ask "Hi, my account is sophie@example.com. Do I still have open invoices?"
rc ask "Which table holds invoice state?" --scenario raw
git push origin dev/<branch>
rc ask "<customer-style question>" --brain-ref dev/<branch>
rc run <run_id> --debug
```

Use `rc-debug` for trace analysis and `brain-publish` after committed local edits.

## Definition Of Done

For a brain change: local checks pass or known laptop infra gaps are named, production confidence is
captured with `rc ask --brain-ref` when needed, and `brain-publish` has either used a public publish
surface or produced a RootCause support request.
