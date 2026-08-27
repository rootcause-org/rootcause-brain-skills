"""Import every brain grounding module in an isolated subprocess.

Run as ``python -m lib.import_smoke <brain_root>``. A module may opt out only with
``# rc: no-import-smoke`` in its header.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import subprocess
import sys
import traceback
from pathlib import Path

OPT_OUT = "# rc: no-import-smoke"


def discover(brain_root: Path) -> list[Path]:
    """Return import-smoke targets in deterministic order."""
    skills = brain_root / "skills"
    if not skills.is_dir():
        raise FileNotFoundError(f"no skills/ under {brain_root}")
    targets: list[Path] = []
    for path in skills.rglob("*.py"):
        rel = path.relative_to(skills)
        parts = rel.parts
        if "actions" in parts or "tests" in parts:
            continue
        if path.name == "conftest.py" or path.name.startswith("test_"):
            continue
        if not any(part in ("scripts", "lib") for part in parts[:-1]):
            continue
        header = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[:10])
        if OPT_OUT in header:
            continue
        targets.append(path)
    return sorted(targets)


def _import_one(path: Path, brain_root: Path) -> int:
    """Child-process entry point; import without giving the module ``__main__`` semantics."""
    sys.path.insert(0, str(path.parent))
    try:
        package_dirs: list[Path] = []
        parent = path.parent
        while (parent / "__init__.py").is_file():
            package_dirs.append(parent)
            parent = parent.parent
        package_dirs.reverse()

        package_name = ""
        if package_dirs:
            digest = hashlib.sha256(str(package_dirs[0]).encode()).hexdigest()[:12]
            package_name = f"_rc_import_smoke_{digest}"
            for index, package_dir in enumerate(package_dirs):
                if index:
                    package_name += f".{package_dir.name}"
                if package_name not in sys.modules:
                    package_spec = importlib.util.spec_from_file_location(
                        package_name,
                        package_dir / "__init__.py",
                        submodule_search_locations=[str(package_dir)],
                    )
                    if package_spec is None or package_spec.loader is None:
                        raise ImportError(f"cannot create package spec for {package_dir}")
                    package = importlib.util.module_from_spec(package_spec)
                    sys.modules[package_name] = package
                    package_spec.loader.exec_module(package)
                if path == package_dir / "__init__.py":
                    return 0

        module_name = f"{package_name}.{path.stem}" if package_name else "_rc_import_smoke_target"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot create import spec for {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    except BaseException:  # report SyntaxError/SystemExit too: both make a host script unusable
        traceback.print_exc()
        return 1
    return 0


def _tail(proc: subprocess.CompletedProcess[str]) -> str:
    lines = [line.strip() for line in (proc.stderr or proc.stdout).splitlines() if line.strip()]
    return lines[-1] if lines else f"exit {proc.returncode}"


def run(brain_root: Path) -> int:
    brain_root = brain_root.expanduser().resolve()
    try:
        targets = discover(brain_root)
    except (OSError, ValueError) as exc:
        print(f"import smoke: FAIL {exc}", file=sys.stderr)
        return 1
    env = dict(os.environ)
    env["RC_LOCAL_BRAIN_RUN"] = "1"
    failures: list[tuple[Path, str]] = []
    for path in targets:
        proc = subprocess.run(
            [sys.executable, "-m", "lib.import_smoke", "--one", str(path), str(brain_root)],
            cwd=brain_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode:
            failures.append((path.relative_to(brain_root), _tail(proc)))
    for path, tail in failures:
        print(f"import smoke FAIL {path}: {tail}", file=sys.stderr)
    print(f"import smoke: checked={len(targets)} failed={len(failures)}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.import_smoke")
    parser.add_argument("--one", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    if args.one:
        if len(args.paths) != 2:
            parser.error("--one requires <module_path> <brain_root>")
        return _import_one(Path(args.paths[0]), Path(args.paths[1]))
    if len(args.paths) != 1:
        parser.error("give one <brain_root>")
    return run(Path(args.paths[0]))


if __name__ == "__main__":
    raise SystemExit(main())
