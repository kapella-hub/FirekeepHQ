"""Windows Job Object: nothing the gateway spawns outlives the gateway.

Every gateway backend is a `firekeep-*.exe` uv trampoline that spawns its own
python.exe, so the process tree under one gateway is two levels deep per
backend. When the host kills the gateway (TerminateProcess: no `finally`, no
`Backend.close()`), Windows does not take the tree with it — on 2026-09-16 the
orphaned shims were traced to a stdin-EOF hang in `shim.serve()` (fixed in
shim.py), but any future hang in ANY backend, or in its grandchild, would leak
the same way. This module is the defence-in-depth half: the gateway puts
ITSELF in a job with KILL_ON_JOB_CLOSE, every descendant inherits the job, and
the kernel closes the job handle — and kills the tree — however the gateway
dies.

Assign-self rather than per-child assignment, because it is one call made
before any backend starts: no window between CreateProcess and assignment in
which a child can spawn an unassigned grandchild, and nothing to repeat when a
backend restarts.

Measured on Windows 11 (26300) with nested jobs, which is the normal case: a
Node host (Claude Code) spawns the gateway into libuv's global job, whose flags
are KILL_ON_JOB_CLOSE | BREAKAWAY_OK | SILENT_BREAKAWAY_OK. Against a parent
job carrying exactly those flags (2026-10-01, ctypes, python 3.14):
  * nesting succeeds, and a plain child lands in OUR job (and the parent's) —
    silent breakaway on the outer job does not let it escape;
  * CREATE_BREAKAWAY_FROM_JOB from inside our job (BREAKAWAY_OK set) succeeds
    even when the outer job forbids breakaway — the child just stays in the
    outer job;
  * CREATE_BREAKAWAY_FROM_JOB from a job WITHOUT BREAKAWAY_OK fails with
    WinError 5. That is the one case `popen_outside_job` retries.

Stdlib + ctypes only: the gateway is on the client's stdlib-only spine.
Opt out with FIREKEEP_NO_JOB_OBJECT=1.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

DISABLE_ENV = "FIREKEEP_NO_JOB_OBJECT"
_FALSEY = ("", "0", "false", "no", "off")

JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_ERROR_ACCESS_DENIED = 5

# The job handle, held for the life of the process and NEVER closed: closing
# the last handle is what fires KILL_ON_JOB_CLOSE, so a close here would kill
# every live backend mid-session. Process exit closes it, which is the point.
_JOB_HANDLE: int | None = None
_STATE: str | None = None


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32():
    """Typed kernel32 — a seam so the unit tests can substitute a fake on any OS."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k.CreateJobObjectW.restype = ctypes.c_void_p
    k.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ]
    k.SetInformationJobObject.restype = ctypes.c_int
    k.GetCurrentProcess.argtypes = []
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k.AssignProcessToJobObject.restype = ctypes.c_int
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.CloseHandle.restype = ctypes.c_int
    return k


def _last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)  # Windows-only in ctypes
    return getter() if getter else 0


def _disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() not in _FALSEY


def contain_process_tree() -> str:
    """Put this process in a kill-on-close job so its descendants die with it.

    Returns "contained", "disabled", "unsupported" (not Windows) or
    "failed: <why>". Never raises: a host whose job forbids nesting must still
    get a working gateway — just without the guarantee. Idempotent.
    """
    global _JOB_HANDLE, _STATE
    if _STATE is not None:
        return _STATE
    if sys.platform != "win32":
        _STATE = "unsupported"
        return _STATE
    if _disabled():
        _STATE = "disabled"
        return _STATE
    try:
        kernel32 = _kernel32()
        # NULL attributes => the handle is NOT inheritable. An inheritable job
        # handle leaked into a backend would keep the job open after the
        # gateway died and silently defeat KILL_ON_JOB_CLOSE.
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            _STATE = f"failed: CreateJobObjectW error {_last_error()}"
        else:
            info = _ExtendedLimitInformation()
            # BREAKAWAY_OK is for launches that are the human's, not ours — a
            # browser opened for a Decision Board, an app opened by Hands. They
            # opt out per launch with CREATE_BREAKAWAY_FROM_JOB; nothing else
            # can leave, because SILENT_BREAKAWAY_OK is deliberately NOT set.
            info.BasicLimitInformation.LimitFlags = (
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK
            )
            if not kernel32.SetInformationJobObject(
                job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info),
            ):
                _STATE = f"failed: SetInformationJobObject error {_last_error()}"
                kernel32.CloseHandle(job)
            elif not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
                # We are not in this job, so closing it kills nothing.
                _STATE = f"failed: AssignProcessToJobObject error {_last_error()}"
                kernel32.CloseHandle(job)
            else:
                _JOB_HANDLE = job
                _STATE = "contained"
    except Exception as exc:  # noqa: BLE001 — never fail gateway startup
        _STATE = f"failed: {exc!r}"
    if _STATE != "contained":
        log.debug("gateway job object not applied: %s", _STATE)
    return _STATE


def popen_outside_job(argv, **kwargs) -> subprocess.Popen:
    """`subprocess.Popen` for a process that must OUTLIVE the gateway.

    Adds CREATE_BREAKAWAY_FROM_JOB on Windows. If the enclosing job forbids
    breakaway (WinError 5 — our own job was not applied and the host's job is
    strict), launches inside the host job instead: the pre-job behaviour, and
    better than no launch. Any other error propagates. Plain Popen elsewhere.
    """
    if sys.platform != "win32":
        return subprocess.Popen(argv, **kwargs)
    base = kwargs.pop("creationflags", 0)
    try:
        return subprocess.Popen(argv, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError as exc:
        if getattr(exc, "winerror", None) != _ERROR_ACCESS_DENIED:
            raise
        log.debug("breakaway denied by the enclosing job; launching inside it")
        return subprocess.Popen(argv, creationflags=base, **kwargs)
