# /// script
# requires-python = ">=3.11"
# ///
"""Run the brain import smoke in uv or host-faithful docker mode."""

from __future__ import annotations

import argparse
import subprocess
import sys

import brain_env as E


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain_smoke.py", description=__doc__)
    parser.add_argument("--brain", help="brain dir (default: cwd)")
    parser.add_argument("--mode", choices=("uv", "docker"), default="uv")
    parser.add_argument("--image", default=E.DEFAULT_IMAGE)
    parser.add_argument("--mirrors-root")
    parser.add_argument("--mirror", action="append", default=[], metavar="name=path")
    args = parser.parse_args(argv)
    brain_dir = E.resolve_brain_dir(args.brain)
    try:
        mirrors = E.discover_mirrors(brain_dir, args.mirrors_root, args.mirror)
    except (OSError, ValueError) as exc:
        print(f"error: cannot resolve mirrors: {exc}", file=sys.stderr)
        return 1
    if not E.require_mirrors(mirrors):
        return 1
    secrets = E.brain_secrets(brain_dir, required=False)
    if args.mode == "uv":
        child = E.uv_child_env(secrets, [], args.mirrors_root, mirrors)
        print(f"warning: import smoke using uv mode. {E.UV_MODE_CAVEATS}", file=sys.stderr)
        return subprocess.run(
            [*E.uv_base_cmd(), "python", "-m", "lib.import_smoke", str(brain_dir)], env=child
        ).returncode
    if not E.docker_available():
        print("error: docker not available (docker info failed)", file=sys.stderr)
        return 1
    for name, path in mirrors.items():
        if not path.is_dir():
            print(f"warning: mirror {name!r} path missing: {path}", file=sys.stderr)
    print(f"[docker mode] import smoke image={args.image}", file=sys.stderr)
    command = ["python", "-m", "lib.import_smoke", E.BRAIN_MOUNT]
    run_args = E.docker_run_args(
        image=args.image,
        brain_dir=brain_dir,
        mirrors=mirrors,
        env_names=list(secrets),
        command=command,
    )
    return E.run_docker(run_args, secrets)


if __name__ == "__main__":
    raise SystemExit(main())
