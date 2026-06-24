---
name: brain-dev-upgrade
description: Update or check the installed rootcause brain skills kit on a local brain checkout. Use when a user asks how to get the newest brain-dev, brain-debug, or rc-* skills, asks whether Codex/Claude auto-updates them, asks to run install.sh again, or wants to check the latest released kit version.
---

# brain-dev-upgrade - update the local kit

Use this when the user wants the newest `rootcause-brain-skills` on their laptop.

## Facts

- The skills do **not** auto-update inside already-installed Codex or Claude setups.
- The moving installer URL is:
  `https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/main/install.sh`
- The latest-version endpoint used by the installer is:
  `https://api.github.com/repos/rootcause-org/rootcause-brain-skills/git/matching-refs/tags/v`
- `install.sh` pins the shared kit clone to a released tag; it does not leave the kit floating on
  `main`.

## Human commands

From the brain root, or any subdirectory inside a brain checkout:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/main/install.sh)
```

From anywhere else, pass the brain path:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/main/install.sh) ~/code/rootcause-org/rootcause-brain-<project>
```

Check the latest released tag without installing:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/main/install.sh) --latest-version
```

Check the locally installed tag:

```bash
git -C "${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}" describe --tags --exact-match
```

## Agent workflow

1. Check the latest released tag:
   ```bash
   bash <(curl -fsSL https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/main/install.sh) --latest-version
   ```
2. If the current directory is not inside a brain checkout, locate the intended brain repo or ask for
   the brain path.
3. If the user asks to upgrade, run the moving installer URL yourself.
4. After upgrading, compare the installed tag from the shared clone to the latest tag:
   ```bash
   git -C "${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}" describe --tags --exact-match
   ```
   If they differ, treat the upgrade as failed and surface the mismatch.
5. If the user uses plugin installs instead of local symlinks, tell them the explicit updater:
   - Claude Code: `/plugin marketplace update`
   - Codex: `codex plugin marketplace upgrade`
6. Mention that already-running agent sessions may need a new session to reload changed skill text.
