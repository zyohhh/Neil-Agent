"""Own the lifetime of a host command and its cooperating descendants."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
from typing import Any


# The Windows launcher must not start the requested command until job assignment.
# -I -S prevents workspace/user Python startup code from running before the gate.
WINDOWS_LAUNCHER = (
    "import os,subprocess,sys\n"
    "if os.read(0,1) != b'1': raise SystemExit(125)\n"
    "raise SystemExit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))\n"
)


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority", ctypes.c_uint32),
        ("scheduling", ctypes.c_uint32),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class ProcessTree:
    """Windows Job Object or POSIX process group; not a security sandbox."""

    def __init__(self) -> None:
        self._job: int | None = None
        self._pid: int | None = None
        self._api: Any = None
        if os.name != "nt":
            return
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        api.CreateJobObjectW.restype = ctypes.c_void_p
        api.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
        ]
        api.SetInformationJobObject.restype = ctypes.c_int
        api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        api.OpenProcess.restype = ctypes.c_void_p
        api.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        api.AssignProcessToJobObject.restype = ctypes.c_int
        api.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        api.TerminateJobObject.restype = ctypes.c_int
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        api.CloseHandle.restype = ctypes.c_int
        self._api = api
        self._job = api.CreateJobObjectW(None, None)
        if not self._job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(
            self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            api.CloseHandle(self._job)
            self._job = None
            raise error

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        self._pid = process.pid
        if self._job is None:
            return
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE. The launcher is still gated.
        handle = self._api.OpenProcess(0x0101, False, process.pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._api.AssignProcessToJobObject(self._job, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._api.CloseHandle(handle)

    def close(self) -> None:
        if self._job is not None:
            handle, self._job = self._job, None
            try:
                if not self._api.TerminateJobObject(handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self._api.CloseHandle(handle)
        elif self._pid is not None and os.name != "nt":
            try:
                getattr(os, "killpg")(self._pid, getattr(signal, "SIGKILL"))
            except ProcessLookupError:
                pass
        self._pid = None
