"""Small, typed wrapper around the local ``rc`` CLI for deterministic scripts.

This module is for *local brain development* scripts, where OAuth and project scope already live in
``rc``.  It is deliberately not a production-runtime helper: the production loop has no ``rc``
binary and should use the injected ``lib.db`` / ``lib.fs`` capabilities instead.

The console's machine contract is JSON with ``rows`` as arrays plus an ordered ``columns`` list.  A
truncated query is unsafe to accidentally consume, so it raises by default; pass
``allow_truncated=True`` only when an explicitly partial result is useful.
"""

from __future__ import annotations

import csv
import builtins
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


DEFAULT_DATABASE = "prod"


class RCClientError(RuntimeError):
    """Base class for an ``rc`` failure, retaining its typed CLI exit code."""

    exit_code = 1

    def __init__(self, message: str, *, status: int | None = None, fields: Any = None):
        super().__init__(message)
        self.status = status
        self.fields = fields


class UsageError(RCClientError):
    """Invalid local input or CLI invocation (exit 1)."""

    exit_code = 1


class AuthenticationError(RCClientError):
    """OAuth login/scope failure (exit 2)."""

    exit_code = 2


class TruncatedError(RCClientError):
    """The server returned a partial query result (exit 3)."""

    exit_code = 3


class RemoteCommandError(RCClientError):
    """Workspace bash exited non-zero or timed out (exit 4)."""

    exit_code = 4


class TransportError(RCClientError):
    """Server/network failure (exit 5)."""

    exit_code = 5


_ERROR_TYPES = {
    1: UsageError,
    2: AuthenticationError,
    3: TruncatedError,
    4: RemoteCommandError,
    5: TransportError,
}


@dataclass(frozen=True)
class Result:
    """One console query response; each row aligns positionally with ``columns``."""

    columns: list[str]
    rows: list[list[Any]]
    truncated: bool = False

    def query_to_csv(self, path: str | Path) -> Path:
        """Write this result to ``path`` while preserving duplicate column names and order."""
        destination = Path(path)
        with destination.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(self.columns)
            writer.writerows(self.rows)
        return destination


@dataclass(frozen=True)
class BashResult:
    """Output from the guarded workspace bash plane."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def __iter__(self):
        """Allow ``exit_code, stdout, stderr = bash(...)`` as documented."""
        yield self.exit_code
        yield self.stdout
        yield self.stderr


Runner = Callable[[Sequence[str], float | None], subprocess.CompletedProcess[str]]


def _default_database(database: str | None) -> str:
    return database or os.environ.get("RC_CONSOLE_DATABASE") or os.environ.get("RC_DB_DEFAULT") or DEFAULT_DATABASE


def _default_runner(args: Sequence[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)


def _decode_error(stdout: str, stderr: str, returncode: int) -> RCClientError:
    message = stderr.strip() or stdout.strip() or f"rc exited {returncode}"
    status = None
    fields = None
    try:
        envelope = json.loads(stdout)
        error = envelope.get("error", {}) if isinstance(envelope, dict) else {}
        if isinstance(error, dict):
            message = str(error.get("message") or message)
            status = error.get("status")
            fields = error.get("fields")
    except json.JSONDecodeError:
        pass
    error_type = _ERROR_TYPES.get(returncode, TransportError)
    return error_type(message, status=status, fields=fields)


class Client:
    """Configurable client; module-level functions use a default instance per call."""

    def __init__(
        self,
        *,
        executable: str = "rc",
        database: str | None = None,
        runner: Runner | None = None,
    ):
        self.executable = executable
        self.database = database
        self._runner = runner or _default_runner

    def _run(self, args: Iterable[str], *, timeout: float | None = None) -> dict[str, Any]:
        command = [self.executable, "-o", "json", *args]
        try:
            completed = self._runner(command, timeout)
        except FileNotFoundError as exc:
            raise TransportError(f"rc executable not found: {self.executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RemoteCommandError(f"rc command timed out after {timeout}s") from exc
        if completed.returncode:
            raise _decode_error(completed.stdout, completed.stderr, completed.returncode)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise TransportError("rc returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise TransportError("rc returned a non-object JSON response")
        return payload

    def query(
        self,
        sql: str,
        params: dict[str, str] | Iterable[tuple[str, str]] | None = None,
        all: bool = False,
        *,
        database: str | None = None,
        allow_truncated: bool = False,
    ) -> Result:
        """Execute parameterized SQL through ``rc`` and return lossless rows.

        ``params`` values are deliberately text, matching repeated ``--param key=value``.  Use
        ``all=True`` for a complete export; otherwise a server-declared truncation raises.
        """
        args = ["dev", "console", "database", "query", _default_database(database or self.database), sql]
        if all:
            args.append("--all")
        args.extend(["--format", "json", "--out", "-"])
        pairs = params.items() if isinstance(params, dict) else (params or ())
        for key, value in pairs:
            args.extend(["--param", f"{key}={value}"])
        payload = self._run(args)
        columns = payload.get("columns")
        rows = payload.get("rows")
        truncated = payload.get("truncated", False)
        if not isinstance(columns, list) or not builtins.all(isinstance(column, str) for column in columns):
            raise TransportError("rc query response has invalid columns")
        if not isinstance(rows, list) or not builtins.all(isinstance(row, list) for row in rows):
            raise TransportError("rc query response has invalid rows")
        if not isinstance(truncated, bool):
            raise TransportError("rc query response has invalid truncated flag")
        result = Result(columns=columns, rows=rows, truncated=truncated)
        if result.truncated and not allow_truncated:
            raise TruncatedError("query result was truncated; rerun with all=True or allow_truncated=True")
        return result

    def query_to_csv(self, sql: str, path: str | Path, **kwargs: Any) -> Result:
        """Run ``query`` then write an exact CSV representation to ``path``."""
        result = self.query(sql, **kwargs)
        result.query_to_csv(path)
        return result

    def bash(self, cmd: str, timeout: int | None = None) -> BashResult:
        """Run one workspace command and return its decoded result.

        A remote non-zero exit or timeout is represented by ``RemoteCommandError`` by the CLI and
        is raised here; successful results retain the server's exit metadata.
        """
        args = ["dev", "console", "bash", "run", cmd, "--out", "-"]
        if timeout is not None:
            args.extend(["--timeout", str(timeout)])
        payload = self._run(args, timeout=timeout)
        exit_code = payload.get("exit_code", 0)
        stdout = payload.get("stdout", "")
        stderr = payload.get("stderr", "")
        timed_out = payload.get("timed_out", False)
        if not isinstance(exit_code, int) or not isinstance(stdout, str) or not isinstance(stderr, str) or not isinstance(timed_out, bool):
            raise TransportError("rc bash response has invalid fields")
        return BashResult(exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out)

    def file_get(self, remote: str, local: str | Path) -> Path:
        """Fetch one permitted workspace or ``/tmp`` file to ``local``."""
        destination = Path(local)
        # ``file get --out`` writes bytes locally; do not ask the CLI to also print a JSON body.
        self._run(["dev", "console", "file", "get", remote, "--out", str(destination)])
        return destination


def query(sql: str, params: dict[str, str] | Iterable[tuple[str, str]] | None = None, all: bool = False, **kwargs: Any) -> Result:
    return Client().query(sql, params=params, all=all, **kwargs)


def query_to_csv(sql: str, path: str | Path, **kwargs: Any) -> Result:
    return Client().query_to_csv(sql, path, **kwargs)


def bash(cmd: str, timeout: int | None = None) -> BashResult:
    return Client().bash(cmd, timeout=timeout)


def file_get(remote: str, local: str | Path) -> Path:
    return Client().file_get(remote, local)


__all__ = [
    "AuthenticationError", "BashResult", "Client", "RCClientError", "RemoteCommandError",
    "Result", "TransportError", "TruncatedError", "UsageError", "bash", "file_get", "query",
    "query_to_csv",
]
