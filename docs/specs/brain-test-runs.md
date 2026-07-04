# Spec — Brain Test Runs

Historical implementation details moved to code and public docs. Current shipped behavior:

- `rc ask "<question>"` creates a production run.
- `rc ask "<question>" --brain-ref dev/<branch>` creates a test run against a pushed dev branch
  without moving live refs.
- `rc run <id> --full -o json` returns the run/event trace bundle.
- `brain_dump.py` renders the bundle through `rootcause-runtime`'s shared run-dump renderer.
- `rc run <id> --debug` writes the CLI's own decomposed debug files; keep its sections in parity with
  this kit's run-dump renderer when the run bundle changes.

Auth is OAuth via `rc login`. Do not use API keys, private debug scripts, registry DB access, or host
infrastructure from this kit.

See:

- [docs/rc-cli.md](../rc-cli.md)
- [docs/run-trace-model.md](../run-trace-model.md)
- [skills/brain-ask/SKILL.md](../../skills/brain-ask/SKILL.md)
- [skills/rc-debug/SKILL.md](../../skills/rc-debug/SKILL.md)
