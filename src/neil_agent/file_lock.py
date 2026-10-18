"""Bounded kernel locks for workspace-local state transactions."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from .errors import NeilAgentError


class StateFileLock:
    """Serialize cooperating processes; never remove a live lock's inode."""

    def __init__(
        self, path: Path, error_type: type[NeilAgentError], *, timeout: float = 5.0
    ) -> None:
        self._path = path
        self._error_type = error_type
        self._timeout = timeout
        self._descriptor = -1
        self._acquired = False

    def __enter__(self) -> StateFileLock:
        try:
            if self._path.exists() or self._path.is_symlink():
                if self._path.is_symlink() or not stat.S_ISREG(
                    self._path.lstat().st_mode
                ):
                    raise self._error_type("状态锁必须是真实普通文件。")
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            self._descriptor = os.open(self._path, flags, 0o600)
            info = os.fstat(self._descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or self._path.resolve(strict=True) != self._path
                or not os.path.samestat(info, self._path.lstat())
            ):
                raise self._error_type("状态锁必须是真实普通文件。")
            if info.st_size == 0:
                os.write(self._descriptor, b"\0")
                os.fsync(self._descriptor)
            deadline = monotonic() + self._timeout
            while not self._try_lock():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise self._error_type(
                        "本地状态正被其他进程使用，未执行写入，请重试。"
                    )
                sleep(min(0.02, remaining))
            self._acquired = True
            return self
        except BaseException as error:
            self.__exit__(None, None, None)
            if isinstance(error, OSError):
                raise self._error_type("无法获取本地状态锁。") from error
            raise

    def _try_lock(self) -> bool:
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(self._descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    return False
                raise
            return True
        fcntl: Any = __import__("fcntl")
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        # Closing also releases the kernel lock, including after process death.
        if self._descriptor != -1:
            try:
                if self._acquired:
                    os.lseek(self._descriptor, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(self._descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl: Any = __import__("fcntl")
                        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            except OSError as error:
                if isinstance(exc, BaseException):
                    exc.add_note("本地状态锁释放失败。")
                else:
                    raise self._error_type("本地状态锁释放失败。") from error
            finally:
                try:
                    os.close(self._descriptor)
                finally:
                    self._descriptor = -1
                    self._acquired = False
