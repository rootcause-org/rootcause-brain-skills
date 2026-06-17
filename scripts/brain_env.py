# /// script
# requires-python = ">=3.11"
# ///
"""Shared engine core for the brain-dev kit — env + PYTHONPATH + lib preflight + the two run modes.

`brain_run.py` and `brain_test.py` are thin front-ends over this module. Everything here is
**brain-dir-relative and `accounts.yml`-free** (SPEC §5): the brain is just the cwd (or an explicit
path), its env is the gitignored plaintext `./.env`, and `lib` comes from the kit's *bundled*
`runtime/` package — the exact bytes pinned by this plugin's tag, which is the same
`rootcause-runtime@<tag>` the prod workspace image installs. So a green uv-mode run is provably the
same `lib` prod runs (SPEC §3.1), with the fidelity gaps called out in `UV_MODE_CAVEATS`.

Two run modes (SPEC §4):
  * **uv**     — fast inner loop: `uv run` with the bundled `lib` + its pinned deps, env from `./.env`.
  * **docker** — faithful pre-push gate: `docker run` the published workspace image, brain + mirrors
                 mounted `:ro`, env injected, isolation matching prod.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# ── version line ────────────────────────────────────────────────────────────────────────────────
# The single version line (SPEC §7): plugin tag == rootcause-runtime pin == workspace image tag ==
# prod Dockerfile pin. Bump all together so local and prod cannot diverge.
VERSION = "0.1.0"

# brain_env.py lives at <plugin>/scripts/ ; the bundled runtime package (holding lib/) is one up.
RUNTIME = (Path(__file__).resolve().parents[1] / "runtime").resolve()

# The published workspace image prod uses (SPEC §4.2). Overridable for a local build or a fork.
DEFAULT_IMAGE = os.environ.get("RC_WORKSPACE_IMAGE", f"ghcr.io/rootcause-org/workspace:v{VERSION}")

# Container constants, mirrored from rootcause-light internal/workspace/docker.go so docker mode is
# byte-faithful to a real run.
CONTAINER_UID = 10001
CONTAINER_GID = 10001
HOME_DIR = "/home/agent"
BRAIN_MOUNT = "/brain"
MIRRORS_MOUNT = "/mirrors"

UV_MODE_CAVEATS = (
    "uv-mode fidelity gap (SPEC §4.1): open internet (a call that passes here can be EGRESS_BLOCKED "
    "in prod), no :ro mounts (no EROFS), no container isolation, deps resolved fresh (not the pinned "
    "image set). A green uv run is NOT a guaranteed-green prod run — gate with `--mode docker` before "
    "pushing."
)


# ── env parsing (mirrors the Go host's secret.parseEnv, plus shell-quote tolerance) ───────────────
def _unquote(value: str) -> str:
    """Strip ONE matched pair of surrounding quotes — how a bash `source` (and most dotenv loaders)
    treat `KEY="value"`. The Go host's writer never quotes, so this is a no-op on a box-emitted
    `.env`; it only saves a hand-edited local `.env` from sending a quoted DSN to psycopg verbatim
    (`missing "=" after …`) and silently skipping every live test."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_env(path: Path) -> dict[str, str]:
    """KEY=VALUE per line; skip blank/`#`; first `=` splits; key trimmed, value `_unquote`d.
    Mirrors internal/secret.parseEnv (the grammar the box stores `.env` in)."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        eq = line.find("=")
        if eq < 0:
            continue
        key = line[:eq].strip()
        if key:
            out[key] = _unquote(line[eq + 1 :])
    return out


def resolve_brain_dir(arg: str | None) -> Path:
    """The brain is the cwd by default (you `cd` into the brain and invoke the skill — SPEC §5);
    an explicit path overrides for multi-brain / scripted use."""
    return Path(arg).expanduser().resolve() if arg else Path.cwd().resolve()


def load_env(brain_dir: Path, *, required: bool) -> dict[str, str] | None:
    """The child env = current env + the brain's `./.env`. Returns None (and the caller errors) when
    `required` and there's no `.env`."""
    env_file = brain_dir / ".env"
    if not env_file.is_file():
        if required:
            print(
                f"error: no .env at {env_file} — the brain's gitignored plaintext .env is the env "
                "source. Operators recover it with rootcause-light's `rc_env.py <project> --pull`.",
                file=sys.stderr,
            )
            return None
        return dict(os.environ)
    child = dict(os.environ)
    child.update(parse_env(env_file))
    return child


def dsn_names(env: dict[str, str]) -> list[str]:
    """The project DSN env vars present (`*_DSN`), excluding the host store — mirrors lib.db.databases."""
    return sorted(k for k, v in env.items() if k.endswith("_DSN") and v and k != "DATABASE_URL")


# ── mirrors (source repos the brain's fs helpers read) ────────────────────────────────────────────
def discover_mirrors(mirrors_root: str | None, explicit: list[str]) -> dict[str, Path]:
    """Map mirror-name -> local path, from `--mirrors-root DIR` (each immediate subdir is a mirror)
    and/or repeated `--mirror name=path`. Explicit wins on a name clash. Missing paths are dropped by
    the caller with a spoken reason (SPEC §9: mirrors may be absent locally — degrade gracefully)."""
    out: dict[str, Path] = {}
    if mirrors_root:
        root = Path(mirrors_root).expanduser().resolve()
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    out[child.name] = child
    for spec in explicit:
        if "=" not in spec:
            print(f"warning: ignoring --mirror {spec!r} (want name=path)", file=sys.stderr)
            continue
        name, _, path = spec.partition("=")
        out[name.strip()] = Path(path.strip()).expanduser().resolve()
    return out


# ── uv mode ───────────────────────────────────────────────────────────────────────────────────────
def uv_child_env(
    base: dict[str, str], extra_pythonpath: list[Path], mirrors_root: str | None
) -> dict[str, str]:
    """The child env for a uv-mode invocation: the script's own dir(s) on PYTHONPATH (for siblings
    like `from ka import …`; `lib` itself arrives as the installed bundled package), and
    `RC_MIRRORS_ROOT` so lib.fs reads a local mirror farm instead of the absent `/mirrors`."""
    child = dict(base)
    paths = [str(p) for p in extra_pythonpath] + ([child["PYTHONPATH"]] if child.get("PYTHONPATH") else [])
    if paths:
        child["PYTHONPATH"] = os.pathsep.join(paths)
    if mirrors_root:
        child["RC_MIRRORS_ROOT"] = str(Path(mirrors_root).expanduser().resolve())
    return child


def uv_base_cmd(extra_with: list[str] | None = None) -> list[str]:
    """`uv run` installing the bundled rootcause-runtime (brings `lib` + its pinned deps from
    runtime/pyproject.toml — one dependency source of truth), plus any extras (e.g. pytest)."""
    cmd = ["uv", "run", "--no-project", "--with", str(RUNTIME)]
    for w in extra_with or []:
        cmd += ["--with", w]
    return cmd


def preflight_lib_db(child_env: dict[str, str], extra_with: list[str] | None = None) -> bool:
    """HARD-FAIL preflight (the footgun guard, SPEC §4.1): prove `import lib.db` resolves in the SAME
    child env before the real script runs. A brain's `ka.py` guards `from lib import db` in
    try/except → `db = None`; a broken import would otherwise fail *silently* at call time. We make
    it loud up front."""
    check = subprocess.run(
        [*uv_base_cmd(extra_with), "python", "-c", "import lib.db"],
        env=child_env, capture_output=True, text=True,
    )
    if check.returncode != 0:
        print(
            "error: `import lib.db` failed in the child env — a brain's try/except guard would "
            f"silently get db=None.\nPYTHONPATH={child_env.get('PYTHONPATH', '')}\n"
            f"{check.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


# ── docker mode ─────────────────────────────────────────────────────────────────────────────────
def docker_run_args(
    *,
    image: str,
    brain_dir: Path,
    mirrors: dict[str, Path],
    env_names: list[str],
    workdir: str = BRAIN_MOUNT,
    command: list[str],
) -> list[str]:
    """`docker run` argv mirroring rootcause-light internal/workspace/docker.go: read-only rootfs,
    all caps dropped, no-new-privileges, tmpfs /tmp + writable agent home, brain + mirrors `:ro`
    (kernel-enforced EROFS), env by `-e KEY` (values ride the docker client's own env, off the argv,
    exactly as prod keeps secrets off argv). One-shot `--rm` — no detached keep-alive needed for the
    pre-push check. Egress is left open (default bridge); the caller says so (SPEC §4.2)."""
    args = [
        "docker", "run", "--rm", "--init",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp",
        "--tmpfs", f"{HOME_DIR}:uid={CONTAINER_UID},gid={CONTAINER_GID}",
        "-v", f"{brain_dir}:{BRAIN_MOUNT}:ro",
    ]
    for name, path in mirrors.items():
        args += ["-v", f"{path}:{MIRRORS_MOUNT}/{name}:ro"]
    for k in sorted(env_names):
        args += ["-e", k]  # value comes from the docker client's process env (off argv)
    args += ["-w", workdir, image, *command]
    return args


def run_docker(args: list[str], env_values: dict[str, str]) -> int:
    """Exec a docker argv built by `docker_run_args`, feeding the `-e KEY` values via the docker
    client's own environment (so they never appear on the command line)."""
    child = dict(os.environ)
    child.update(env_values)
    return subprocess.run(args, env=child).returncode


def docker_available() -> bool:
    try:
        return subprocess.run(["docker", "version"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False
