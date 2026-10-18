"""Bounded subprocess execution for fixed host acceptance checks."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryFile
from time import monotonic

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


def creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def run_bounded(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str] | None = None,
    max_output_bytes: int = 65_536,
) -> ProcessResult:
    """Bound elapsed time and retained output; this is not an OS sandbox."""

    if timeout <= 0 or max_output_bytes < 1:
        raise ValueError("process limits must be positive")
    with TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=dict(environment)
                if environment is not None
                else acceptance_environment(),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags(),
            )
        except OSError:
            return ProcessResult(None, "", "unavailable")
        deadline = monotonic() + timeout
        reason = "completed"
        try:
            while process.poll() is None:
                if os.fstat(output.fileno()).st_size > max_output_bytes:
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
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        output.seek(0)
        payload = output.read(max_output_bytes + 1)
        if len(payload) > max_output_bytes:
            reason = "output_limit"
        return ProcessResult(
            process.returncode,
            payload[:max_output_bytes].decode("utf-8", errors="replace"),
            reason,
        )
