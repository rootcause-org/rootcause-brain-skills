---
name: brain-dev-upgrade
description: Update or check the installed rootcause brain skills kit and the rootcause `rc` CLI on a local brain checkout. Use when a user asks how to get the newest local-brain-work, brain-ask, brain-git-sync, rc-debug, rc-health, rc-fleet, brain-publish, or other shipped brain skills, asks whether Codex/Claude auto-updates them, asks to run install.sh again, wants to check the latest released kit version, or wants to check/update the rootcause CLI used by the skills.
---

# brain-dev-upgrade - update the local kit + `rc`

Use this when the user wants the newest `rootcause-brain-skills` and matching `rc` CLI on their
laptop.

## Facts

- The skills do **not** auto-update inside already-installed Codex or Claude setups.
- The `rc` CLI is a separate sibling repo (`rootcause-org/rootcause-cli`), not part of this kit.
- The moving installer URL is:
  `https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/main/install.sh`
- The latest-version endpoint used by the installer is:
  `https://api.github.com/repos/rootcause-org/rootcause-brain-skills/git/matching-refs/tags/v`
- `install.sh` pins the shared kit clone to a released tag; it does not leave the kit floating on
  `main`.
- macOS has one canonical `rc`: `brew install rootcause-org/tap/rc`. Do not use `go install` as an
  end-user upgrade path there.
- `rc self doctor` reports the executing binary, PATH selection, install kind, duplicates, and
  remediation. `rc self update --check` is read-only.
- On macOS, `rc self update --migrate` explicitly and idempotently upgrades/installs Homebrew and
  removes only verified legacy Go-installed copies. Linux/WSL/Windows standalone installs update in
  place with `rc self update`.
- A client old enough to lack `rc self update` must run its legacy `rc upgrade` once (or upgrade through
  Homebrew) to reach the current command surface.

## Human commands

Check/update `rc`:

```bash
which -a rc
rc --version
rc self doctor
rc self update --check
rc self update --migrate  # macOS migration/canonicalization
rc self doctor
```

For a healthy existing macOS Homebrew install, plain update is sufficient:

```bash
rc self update
```

If `rc` is missing:

```bash
brew install rootcause-org/tap/rc
```

Then update the local brain skills kit.

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

1. Check the `rc` CLI first because the rc-* skills depend on it:
   ```bash
   which -a rc
   rc --version
   rc self doctor
   rc self update --check
   ```
   If `rc` is missing on macOS, install it with `brew install rootcause-org/tap/rc`.
2. If the user asks to upgrade on macOS, run `rc self update --migrate`, then `hash -r`. On other
   platforms run `rc self update`. For a pre-doctor client, avoid updating a shadowed binary:
   ```bash
   brew update
   brew install rootcause-org/tap/rc || brew upgrade --cask rc
   "$(brew --prefix)/bin/rc" self update --migrate
   hash -r
   ```
   If `self update --migrate` is unknown even on the freshly installed cask, the required CLI release
   has not reached Homebrew yet; stop rather than deleting any binary manually.
3. Check the latest released kit tag:
   ```bash
   bash <(curl -fsSL https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/main/install.sh) --latest-version
   ```
4. If the current directory is not inside a brain checkout, locate the intended brain repo or ask for
   the brain path.
5. If the user asks to upgrade, run the moving installer URL yourself.
6. After upgrading, compare the installed tag from the shared clone to the latest tag:
   ```bash
   git -C "${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}" describe --tags --exact-match
   ```
   If they differ, treat the upgrade as failed and surface the mismatch.
7. If the user uses plugin installs instead of local symlinks, tell them the explicit updater:
   - Claude Code: `/plugin marketplace update`
   - Codex: `codex plugin marketplace upgrade`
8. Verify `which -a rc`, `rc --version`, `rc self update --check`, and `rc self doctor`. Require one
   healthy PATH candidate and matching executing/Homebrew/latest versions.
9. Mention that already-running agent sessions may need a new session to reload changed skill text.
