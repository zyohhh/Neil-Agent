"""Cancellable, bounded host commands with owned descendant lifetimes."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryFile
from time import monotonic
from typing import Literal

from .execution_budget import check_execution_budget
from .process_tree import ProcessTree, WINDOWS_LAUNCHER

_ENVIRONMENT_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "UV_CACHE_DIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)


def acceptance_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Exclude credentials, Python injection and inherited model configuration."""

    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _ENVIRONMENT_NAMES
    }
    environment.update(
        PYTHONUTF8="1", PYTHONNOUSERSITE="1", NO_COLOR="1", UV_NO_PROGRESS="1"
    )
    if extra:
        environment.update(extra)
    return environment


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int | None
    output: str
    reason: str = "completed"
    stderr: str = ""


def creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


@contextmanager
def _owned_process(
    command: Sequence[str], *, cwd: Path, environment: Mapping[str, str],
    output: int, errors: int,
) -> Iterator[subprocess.Popen[bytes]]:
    tree = ProcessTree()
    process = None
    primary_error: BaseException | None = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", WINDOWS_LAUNCHER, *command]
            if os.name == "nt" else list(command),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.PIPE if os.name == "nt" else subprocess.DEVNULL,
            stdout=output,
            stderr=errors,
            creationflags=creation_flags(),
            start_new_session=os.name != "nt",
            shell=False,
        )
        tree.attach(process)
        check_execution_budget()
        if process.stdin is not None:
            process.stdin.write(b"1")
            process.stdin.close()
        yield process
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            try:
                tree.close()
            finally:
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    if process.stdin is not None and not process.stdin.closed:
                        process.stdin.close()
        except OSError:
            if primary_error is None:
                raise
            primary_error.add_note("命令进程树清理失败。")


def run_bounded(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str] | None = None,
    max_output_bytes: int = 65_536,
    merge_stderr: bool = True,
    encoding_errors: Literal["strict", "replace"] = "replace",
) -> ProcessResult:
    """Bound elapsed time and retained output; this is not an OS sandbox."""

    if timeout <= 0 or max_output_bytes < 1:
        raise ValueError("process limits must be positive")
    check_execution_budget()
    with TemporaryFile() as output, TemporaryFile() as errors:
        deadline = monotonic() + timeout
        reason = "completed"
        try:
            with _owned_process(
                command,
                cwd=cwd,
                environment=environment
                if environment is not None
                else acceptance_environment(),
                output=output.fileno(),
                errors=(output if merge_stderr else errors).fileno(),
            ) as process:
                while process.poll() is None:
                    check_execution_budget()
                    if sum(os.fstat(stream.fileno()).st_size for stream in (output, errors)) > max_output_bytes:
                        reason = "output_limit"
                        break
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        reason = "timeout"
                        break
                    try:
                        process.wait(timeout=min(remaining, 0.05))
                    except subprocess.TimeoutExpired:
                        continue
        except OSError:
            return ProcessResult(None, "", "unavailable")
        check_execution_budget()
        output.seek(0)
        payload = output.read(max_output_bytes + 1)
        errors.seek(0)
        error_payload = errors.read(max(1, max_output_bytes + 1 - len(payload)))
        if len(payload) + len(error_payload) > max_output_bytes:
            reason = "output_limit"
        retained = payload[:max_output_bytes]
        return ProcessResult(
            process.returncode,
            retained.decode("utf-8", errors=encoding_errors),
            reason,
            error_payload[:max_output_bytes - len(retained)].decode("utf-8", errors=encoding_errors),
        )
