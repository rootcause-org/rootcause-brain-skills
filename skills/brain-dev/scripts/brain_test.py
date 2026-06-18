# /// script
# requires-python = ">=3.11"
# ///
"""Run a brain's pytest tiers the way the workspace container would — offline by default, live on demand.

Brain-dir-relative: `cd` into a `rootcause-brain-<project>` checkout and invoke; operates
on `./skills`. Tiers (from `lib.livecheck`):

  * **offline (default)** — `-m "not live"`: hermetic L1 fixture tests. No DSN, no network.
  * **live** (`--live`)   — `-m live`: L2 schema canary + L3 render-smoke, read-only against the
                            project's real prod DSN (the brain's `./.env`).
  * **gated** (`--require-live`) — `--live` plus fail-if-no-live-test-ran (CI/cron drift alarm).

    uv run brain_test.py                       # offline tier
    uv run brain_test.py --live                # + live tier (read-only prod)
    uv run brain_test.py --require-live         # gated
    uv run brain_test.py --mode docker --live   # faithful: same image prod runs
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import brain_env as E


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain_test.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--brain", help="brain dir (default: cwd)")
    p.add_argument("--mode", choices=("uv", "docker"), default="uv")
    p.add_argument("--image", default=E.DEFAULT_IMAGE, help="workspace image for docker mode")
    p.add_argument("--mirrors-root", help="dir whose immediate subdirs are source mirrors")
    p.add_argument("--mirror", action="append", default=[], metavar="name=path")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--live", action="store_true", help="run the live tier (schema canary + render-smoke)")
    g.add_argument("--require-live", action="store_true",
                   help="--live, and error if no live test ran (no DSN / nothing collected)")
    p.add_argument("pytest_args", nargs="*", help="extra positional args passed through to pytest")
    # Unknown args (pytest flags like -k/-x/--tb) pass through too; kit flags precede them.
    args, extra = p.parse_known_args(sys.argv[1:] if argv is None else argv)
    args.pytest_args = [*args.pytest_args, *extra]

    brain_dir = E.resolve_brain_dir(args.brain)
    skills_dir = brain_dir / "skills"
    if not skills_dir.is_dir():
        print(f"error: no skills/ under {brain_dir} — is this a brain checkout?", file=sys.stderr)
        return 1

    live = args.live or args.require_live
    marker = "live" if live else "not live"
    pytest_args = ["-p", "lib.livecheck", "-m", marker, "-q", *args.pytest_args]
    if args.require_live:
        pytest_args.append("--require-live")

    mirrors = E.discover_mirrors(args.mirrors_root, args.mirror)

    if args.mode == "uv":
        if args.mirror:
            print("warning: --mirror is docker-only; uv mode reads mirrors via --mirrors-root "
                  "(RC_MIRRORS_ROOT). Ignoring the explicit --mirror entries.", file=sys.stderr)
        return _run_uv(brain_dir, skills_dir, live, pytest_args, args.mirrors_root)
    return _run_docker(brain_dir, mirrors, live, pytest_args, args)


def _run_uv(brain_dir: Path, skills_dir: Path, live: bool, pytest_args: list[str],
            mirrors_root: str | None) -> int:
    # The live tier needs the DSN; the offline tier runs without a .env. Script sees ONLY the
    # brain's .env (+ launcher essentials), like prod — never the operator's whole environment.
    secrets = E.brain_secrets(brain_dir, required=live)
    if secrets is None:
        return 1
    child = E.uv_child_env(secrets, [], mirrors_root)
    if not E.preflight_lib_db(child, extra_with=["pytest"]):
        return 1
    print(f"[uv mode] {E.UV_MODE_CAVEATS}", file=sys.stderr)
    cmd = [*E.uv_base_cmd(extra_with=["pytest"]), "pytest", str(skills_dir), *pytest_args]
    return subprocess.run(cmd, env=child).returncode


def _run_docker(brain_dir: Path, mirrors: dict[str, Path], live: bool, pytest_args: list[str],
                args) -> int:
    if not E.docker_available():
        print("error: docker not available (is colima/Docker running?)", file=sys.stderr)
        return 1
    # Inject ONLY the brain's .env (not the host environ) — matches prod, no host-var leakage.
    secrets = E.brain_secrets(brain_dir, required=live)
    if secrets is None:
        return 1
    for name, path in mirrors.items():
        if not path.is_dir():
            print(f"warning: mirror {name!r} path missing: {path}", file=sys.stderr)
    print(f"[docker mode] image={args.image} — egress is OPEN (default bridge), not the prod "
          "default-deny firewall.", file=sys.stderr)
    # pytest over the read-only /brain/skills; lib + the livecheck plugin are baked into the image.
    # `-p no:cacheprovider`: /brain is :ro (EROFS), and the cache is useless for a one-shot --rm run.
    command = ["pytest", "-p", "no:cacheprovider", f"{E.BRAIN_MOUNT}/skills", *pytest_args]
    run_args = E.docker_run_args(
        image=args.image, brain_dir=brain_dir, mirrors=mirrors,
        env_names=list(secrets), command=command,
    )
    return E.run_docker(run_args, secrets)


if __name__ == "__main__":
    raise SystemExit(main())
