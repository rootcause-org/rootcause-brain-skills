# /// script
# requires-python = ">=3.11"
# ///
"""Run one brain grounding script the way the workspace container would — fast `uv` or faithful `docker`.

Brain-dir-relative: `cd` into a `rootcause-brain-<project>` checkout and invoke; it operates on `.`
(SPEC §5). Reads `./.env`, resolves `lib` from the kit's bundled runtime (uv) or the published image
(docker). No `accounts.yml`, no project name, no `code_root`.

    uv run brain_run.py skills/records/scripts/lookup_person.py --tenant X --email a@b.com
    uv run brain_run.py -m lib.db --list
    uv run brain_run.py --mode docker skills/records/scripts/lookup_person.py --email a@b.com
    uv run brain_run.py --brief                      # map the brain (env keys, DBs, mirrors, skills)
    uv run brain_run.py --brain ~/code/rootcause-org/rootcause-brain-kampadmin -m lib.db --list

Everything after the script/`-m` is passed through to it. `--` ends kit options if a script arg
collides (e.g. `--mode`)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import brain_env as E


def _brief(brain_dir: Path, env: dict[str, str], mirrors: dict[str, Path]) -> int:
    """Map the brain without running anything (SPEC §5 'brief'): env key NAMES (values redacted),
    the project DBs, the mirrors the runner can see, and the skills + their scripts."""
    print(f"brain: {brain_dir}")
    env_file = brain_dir / ".env"
    file_keys = sorted(E.parse_env(env_file)) if env_file.is_file() else []
    print(f"\n.env keys ({len(file_keys)}):" if file_keys else "\n.env: (none)")
    for k in file_keys:
        print(f"  {k}")
    dbs = E.dsn_names(env)
    print(f"\ndatabases ({len(dbs)}):")
    for d in dbs:
        print(f"  {d}")
    print(f"\nmirrors visible to runner ({len(mirrors)}):" if mirrors
          else "\nmirrors visible to runner: none (fs helpers will report which is missing)")
    for name, path in mirrors.items():
        print(f"  {name} -> {path}")
    skills_dir = brain_dir / "skills"
    print("\nskills:")
    if skills_dir.is_dir():
        for skill in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            scripts = sorted((skill / "scripts").glob("*.py")) if (skill / "scripts").is_dir() else []
            print(f"  {skill.name}/  ({len(scripts)} script(s))")
            for s in scripts:
                print(f"    skills/{skill.name}/scripts/{s.name}")
    else:
        print("  (no skills/ dir — is this a brain checkout?)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(
        prog="brain_run.py", add_help=True, allow_abbrev=False,  # don't swallow a script's --flag
        description="Run a brain grounding script (uv or docker mode).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--brain", help="brain dir (default: cwd)")
    p.add_argument("--mode", choices=("uv", "docker"), default="uv",
                   help="uv = fast inner loop (default); docker = faithful pre-push gate")
    p.add_argument("--image", default=E.DEFAULT_IMAGE, help="workspace image for docker mode")
    p.add_argument("--mirrors-root", help="dir whose immediate subdirs are source mirrors")
    p.add_argument("--mirror", action="append", default=[], metavar="name=path",
                   help="mount one mirror (repeatable)")
    p.add_argument("--brief", action="store_true", help="map the brain and exit (no run)")
    p.add_argument("-m", dest="module", help="run a module instead of a script file (e.g. lib.db)")
    p.add_argument("target", nargs="?", help="brain-relative (or absolute) script path")
    # Unknown args (the script's own `--email a@b.com`, `--list`, …) pass through untouched. Kit flags
    # must precede the script path; use `--` before the path if a script flag name collides with one.
    args, args.rest = p.parse_known_args(argv)

    brain_dir = E.resolve_brain_dir(args.brain)
    if not brain_dir.is_dir():
        print(f"error: brain dir not found: {brain_dir}", file=sys.stderr)
        return 1

    mirrors = E.discover_mirrors(args.mirrors_root, args.mirror)
    # `--brief` only needs env names; never errors on a missing .env.
    env = E.load_env(brain_dir, required=not args.brief)
    if env is None:
        return 1
    if args.brief:
        return _brief(brain_dir, env, mirrors)

    if not args.module and not args.target:
        print("error: give a script path, or -m <module>", file=sys.stderr)
        return 2

    if args.mode == "uv":
        return _run_uv(brain_dir, env, mirrors, args)
    return _run_docker(brain_dir, env, mirrors, args)


def _invocation(brain_dir: Path, args) -> tuple[list[str], Path | None]:
    """Return (python-invocation, script_dir). Module form has no script dir; a script path is
    resolved against the brain (or taken absolute) and its parent supplies siblings."""
    if args.module:
        return ["-m", args.module, *args.rest], None
    target = Path(args.target)
    script = target if target.is_absolute() else (brain_dir / target).resolve()
    if not script.is_file():
        raise FileNotFoundError(script)
    return [str(script), *args.rest], script.parent


def _run_uv(brain_dir: Path, env: dict[str, str], mirrors: dict[str, Path], args) -> int:
    try:
        invocation, script_dir = _invocation(brain_dir, args)
    except FileNotFoundError as e:
        print(f"error: script not found: {e}", file=sys.stderr)
        return 1
    extra_path = [script_dir] if script_dir else []
    # uv mode reads mirrors from a local farm if given; lib.fs falls back to /mirrors otherwise.
    child = E.uv_child_env(env, extra_path, args.mirrors_root)
    if not E.preflight_lib_db(child):
        return 1
    print(f"[uv mode] {E.UV_MODE_CAVEATS}", file=sys.stderr)
    return subprocess.run([*E.uv_base_cmd(), "python", *invocation], env=child).returncode


def _run_docker(brain_dir: Path, env: dict[str, str], mirrors: dict[str, Path], args) -> int:
    if not E.docker_available():
        print("error: docker not available (is colima/Docker running?)", file=sys.stderr)
        return 1
    if args.module:
        command = ["python", "-m", args.module, *args.rest]
    else:
        target = Path(args.target)
        if target.is_absolute():
            print("error: docker mode needs a brain-relative script path (mounted at /brain)",
                  file=sys.stderr)
            return 1
        if not (brain_dir / target).is_file():
            print(f"error: script not found: {brain_dir / target}", file=sys.stderr)
            return 1
        command = ["python", f"{E.BRAIN_MOUNT}/{target.as_posix()}", *args.rest]
    _warn_missing_mirrors(mirrors)
    print(f"[docker mode] image={args.image} — egress is OPEN (default bridge), not the prod "
          "default-deny firewall (SPEC §4.2).", file=sys.stderr)
    run_args = E.docker_run_args(
        image=args.image, brain_dir=brain_dir, mirrors=mirrors,
        env_names=list(env), command=command,
    )
    return E.run_docker(run_args, env)


def _warn_missing_mirrors(mirrors: dict[str, Path]) -> None:
    for name, path in mirrors.items():
        if not path.is_dir():
            print(f"warning: mirror {name!r} path missing: {path} — fs helpers for it will fail",
                  file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
