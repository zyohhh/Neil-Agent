"""Fail-fast workspace execution ownership for cooperating writable runtimes."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from threading import Lock, get_ident

from .errors import AgentError
from .execution_budget import check_execution_budget
from .file_lock import StateFileLock


class WorkspaceExecutionLock:
    """Reentrant on the owning thread; retain ownership through host persistence."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.resolve(strict=True)
        self._mutex = Lock()
        self._owner: int | None = None
        self._depth = 0
        self._file_lock: StateFileLock | None = None
        self._closed = False

    def close(self) -> None:
        # Never release a running turn's ownership while it may still perform I/O.
        with self._mutex:
            self._closed = True

    @contextmanager
    def scope(self) -> Iterator[None]:
        check_execution_budget()
        with self._mutex:
            if self._closed:
                raise AgentError("工作区执行锁已关闭。")
            if self._owner == get_ident():
                self._depth += 1
            elif self._owner is not None:
                raise AgentError("工作区已有正在执行的任务，请结束后重试。")
            else:
                root = self._workspace / ".neil-agent"
                try:
                    root.mkdir(exist_ok=True)
                    if root.is_symlink() or root.resolve(strict=True) != root or not root.is_dir():
                        raise AgentError("工作区执行锁目录必须是真实目录。")
                    lock = StateFileLock(root / ".execution.lock", AgentError, timeout=0)
                    lock.__enter__()
                except OSError as error:
                    raise AgentError("无法获取工作区执行锁。") from error
                self._owner = get_ident()
                self._depth = 1
                self._file_lock = lock
        try:
            check_execution_budget()
            yield
        finally:
            with self._mutex:
                self._depth -= 1
                if self._depth == 0:
                    held_lock, self._file_lock = self._file_lock, None
                    self._owner = None
                    assert held_lock is not None
                    held_lock.__exit__(None, None, None)
