# Releasing — the single version line

Local and prod must install **identical** `lib` bytes, or "green locally" stops meaning "green in
prod". One tag enforces that. To cut a release, bump **all of these together** to the new `vX.Y.Z`,
commit, then tag + push:

| What | Where | Field |
|---|---|---|
| Engine version + image tag | `skills/brain-dev/scripts/brain_env.py` | `VERSION` (drives `DEFAULT_IMAGE`) |
| `rootcause-runtime` package | `runtime/pyproject.toml` | `version` |
| Claude Code plugin | `plugin.json` + `.claude-plugin/marketplace.json` | `version` |
| Codex plugin | `.codex-plugin/plugin.json` + `.agents/plugins/marketplace.json` | `version` / `ref` |
| Docs install snippets | `README.md`, `docs/onboarding.md`, `docs/migration-rootcause-light.md`, `install.sh` | the `v0.1.0` literals |
| **Prod (separate repo)** | `rootcause-light/runtime/Dockerfile` | the `rootcause-runtime @ git+…@vX.Y.Z` pin + workspace image tag |

Then:

```bash
git tag vX.Y.Z && git push origin vX.Y.Z          # makes the git-pinned runtime spec resolvable
docker build -f docker/Dockerfile -t ghcr.io/rootcause-org/workspace:vX.Y.Z . && docker push ghcr.io/rootcause-org/workspace:vX.Y.Z
```

Then re-point prod and follow [docs/migration-rootcause-light.md](docs/migration-rootcause-light.md).

**Why one line:** the engine resolves `lib` from the sibling `runtime/` when present (offline,
canonical bytes), else `rootcause-runtime @ git+…@vX.Y.Z`. Both must be the same bytes prod's image
bakes. Never float `main` — a push would silently change `lib` under a green local test.
