"""The gateway's Windows Job Object: backends never outlive the gateway.

Two layers. The unit tests drive `contain_process_tree()` against a fake
kernel32 so the decision logic (platform gate, opt-out, every failure path,
idempotence) is pinned on every OS. The integration tests are Windows-only and
real: a parent python contains itself, spawns a child that spawns a grandchild
(the shape of every `firekeep-*.exe` uv trampoline), is hard-killed with
TerminateProcess, and both descendants must be gone — and a child launched
through `popen_outside_job` must NOT be.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from firekeep_client import jobobject

CLIENT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_module_state(monkeypatch):
    # conftest opts the whole suite out (a pytest process inside a kill-on-close
    # job is not what any other test means to run under); these tests opt back in.
    monkeypatch.delenv(jobobject.DISABLE_ENV, raising=False)
    monkeypatch.setattr(jobobject, "_JOB_HANDLE", None)
    monkeypatch.setattr(jobobject, "_STATE", None)


class FakeKernel32:
    """Records every call; each step's success is switchable."""

    def __init__(self, *, create=0x1234, set_info=1, assign=1):
        self._create = create
        self._set_info = set_info
        self._assign = assign
        self.calls: list[tuple] = []
        self.limit_flags = None

    def CreateJobObjectW(self, attrs, name):
        self.calls.append(("CreateJobObjectW", attrs, name))
        return self._create

    def SetInformationJobObject(self, job, info_class, info_ref, size):
        self.calls.append(("SetInformationJobObject", job, info_class, size))
        self.limit_flags = info_ref._obj.BasicLimitInformation.LimitFlags
        return self._set_info

    def GetCurrentProcess(self):
        self.calls.append(("GetCurrentProcess",))
        return -1  # the real pseudo-handle

    def AssignProcessToJobObject(self, job, process):
        self.calls.append(("AssignProcessToJobObject", job, process))
        return self._assign

    def CloseHandle(self, handle):
        self.calls.append(("CloseHandle", handle))
        return 1

    def names(self):
        return [call[0] for call in self.calls]


def _install(monkeypatch, fake, platform="win32"):
    monkeypatch.setattr(jobobject.sys, "platform", platform)
    monkeypatch.setattr(jobobject, "_kernel32", lambda: fake)
    return fake


# --- decision logic (ctypes mocked) -------------------------------------------


def test_non_windows_is_a_complete_noop(monkeypatch):
    fake = _install(monkeypatch, FakeKernel32(), platform="linux")
    assert jobobject.contain_process_tree() == "unsupported"
    assert fake.calls == []
    assert jobobject._JOB_HANDLE is None


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_opt_out_env_skips_every_kernel_call(monkeypatch, value):
    fake = _install(monkeypatch, FakeKernel32())
    monkeypatch.setenv(jobobject.DISABLE_ENV, value)
    assert jobobject.contain_process_tree() == "disabled"
    assert fake.calls == []


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_falsey_opt_out_values_still_contain(monkeypatch, value):
    _install(monkeypatch, FakeKernel32())
    monkeypatch.setenv(jobobject.DISABLE_ENV, value)
    assert jobobject.contain_process_tree() == "contained"


def test_success_assigns_self_with_kill_on_close_and_breakaway_ok(monkeypatch):
    fake = _install(monkeypatch, FakeKernel32())
    assert jobobject.contain_process_tree() == "contained"
    # Unnamed, default security => a NON-inheritable handle. An inheritable one
    # leaking into a child would hold the job open past the gateway's death.
    assert fake.calls[0] == ("CreateJobObjectW", None, None)
    set_info = next(c for c in fake.calls if c[0] == "SetInformationJobObject")
    assert set_info[2] == 9  # JobObjectExtendedLimitInformation
    assert set_info[3] == ctypes.sizeof(jobobject._ExtendedLimitInformation)
    assert fake.limit_flags == (
        jobobject.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | jobobject.JOB_OBJECT_LIMIT_BREAKAWAY_OK
    )
    # Assign-SELF: one call on the current-process pseudo-handle, not per child.
    assert ("AssignProcessToJobObject", 0x1234, -1) in fake.calls
    assert fake.names().count("AssignProcessToJobObject") == 1
    # The handle is parked for the life of the process and never closed:
    # closing it is what fires KILL_ON_JOB_CLOSE on the live session.
    assert "CloseHandle" not in fake.names()
    assert jobobject._JOB_HANDLE == 0x1234


def test_second_call_is_idempotent(monkeypatch):
    fake = _install(monkeypatch, FakeKernel32())
    assert jobobject.contain_process_tree() == "contained"
    assert jobobject.contain_process_tree() == "contained"
    assert fake.names().count("CreateJobObjectW") == 1


def test_create_failure_never_raises(monkeypatch):
    fake = _install(monkeypatch, FakeKernel32(create=0))
    state = jobobject.contain_process_tree()
    assert state.startswith("failed")
    assert "AssignProcessToJobObject" not in fake.names()
    assert jobobject._JOB_HANDLE is None


def test_set_information_failure_closes_the_unused_job(monkeypatch):
    fake = _install(monkeypatch, FakeKernel32(set_info=0))
    assert jobobject.contain_process_tree().startswith("failed")
    assert ("CloseHandle", 0x1234) in fake.calls
    assert "AssignProcessToJobObject" not in fake.names()
    assert jobobject._JOB_HANDLE is None


def test_assign_failure_from_a_host_job_is_survivable(monkeypatch):
    """A host whose own job forbids nesting makes AssignProcessToJobObject fail.
    The gateway must start anyway; the job we are NOT in is safe to close."""
    fake = _install(monkeypatch, FakeKernel32(assign=0))
    assert jobobject.contain_process_tree().startswith("failed")
    assert ("CloseHandle", 0x1234) in fake.calls
    assert jobobject._JOB_HANDLE is None


def test_kernel32_load_failure_never_raises(monkeypatch):
    monkeypatch.setattr(jobobject.sys, "platform", "win32")

    def boom():
        raise OSError("no kernel32")

    monkeypatch.setattr(jobobject, "_kernel32", boom)
    assert jobobject.contain_process_tree().startswith("failed")


# --- popen_outside_job --------------------------------------------------------


class _PopenRecorder:
    def __init__(self, fail_first_with=None):
        self.fail_first_with = fail_first_with
        self.calls: list[tuple] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.fail_first_with is not None and len(self.calls) == 1:
            raise self.fail_first_with
        return "proc"


def _winerror(code):
    exc = OSError(code, "simulated")
    exc.winerror = code
    return exc


def test_popen_outside_job_adds_breakaway_and_keeps_caller_flags(monkeypatch):
    rec = _PopenRecorder()
    monkeypatch.setattr(jobobject.sys, "platform", "win32")
    monkeypatch.setattr(jobobject.subprocess, "Popen", rec)
    assert jobobject.popen_outside_job(["x"], creationflags=0x08000000) == "proc"
    assert rec.calls == [
        (["x"], {"creationflags": 0x08000000 | jobobject.CREATE_BREAKAWAY_FROM_JOB})
    ]


def test_popen_outside_job_retries_in_job_when_breakaway_is_denied(monkeypatch):
    """Measured: a process in a job WITHOUT BREAKAWAY_OK (a strict host job, when
    our own assign failed) gets WinError 5 for CREATE_BREAKAWAY_FROM_JOB. A launch
    inside the host job beats no launch."""
    rec = _PopenRecorder(fail_first_with=_winerror(5))
    monkeypatch.setattr(jobobject.sys, "platform", "win32")
    monkeypatch.setattr(jobobject.subprocess, "Popen", rec)
    assert jobobject.popen_outside_job(["x"], creationflags=0x08000000) == "proc"
    assert rec.calls[1] == (["x"], {"creationflags": 0x08000000})


def test_popen_outside_job_propagates_other_errors(monkeypatch):
    rec = _PopenRecorder(fail_first_with=_winerror(2))
    monkeypatch.setattr(jobobject.sys, "platform", "win32")
    monkeypatch.setattr(jobobject.subprocess, "Popen", rec)
    with pytest.raises(OSError):
        jobobject.popen_outside_job(["missing"])
    assert len(rec.calls) == 1


def test_popen_outside_job_off_windows_is_plain_popen(monkeypatch):
    rec = _PopenRecorder()
    monkeypatch.setattr(jobobject.sys, "platform", "linux")
    monkeypatch.setattr(jobobject.subprocess, "Popen", rec)
    jobobject.popen_outside_job(["x"], stdout=1)
    assert rec.calls == [(["x"], {"stdout": 1})]


# --- gateway wiring -----------------------------------------------------------


def test_gateway_run_contains_its_process_tree_before_serving(monkeypatch):
    import io

    from firekeep_client import gateway as gw

    order = []
    monkeypatch.setattr(gw.jobobject, "contain_process_tree",
                        lambda: order.append("contain") or "contained")
    real_gateway = gw.Gateway

    def _gateway():
        order.append("gateway")
        return real_gateway()

    monkeypatch.setattr(gw, "Gateway", _gateway)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert gw.run() == 0
    assert order == ["contain", "gateway"]


# --- real Windows integration -------------------------------------------------

_PARENT = textwrap.dedent("""
    import subprocess, sys, time
    from firekeep_client import jobobject

    state = jobobject.contain_process_tree()
    # child -> grandchild: the uv-trampoline shape (firekeep-shim.exe -> python.exe)
    child_code = (
        "import subprocess, sys, time;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
        "print(p.pid, flush=True); time.sleep(120)"
    )
    argv = [sys.executable, "-c", child_code]
    launch = jobobject.popen_outside_job if sys.argv[1] == "breakaway" else subprocess.Popen
    child = launch(argv, stdout=subprocess.PIPE, text=True)
    grandchild = child.stdout.readline().strip()
    print(state, child.pid, grandchild, flush=True)
    time.sleep(120)
""")

_SYNCHRONIZE = 0x00100000
_PROCESS_TERMINATE = 0x0001
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102


def _kernel():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.WaitForSingleObject.restype = ctypes.c_uint32
    k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    return k


def _spawn_parent(tmp_path, mode):
    script = tmp_path / "parent.py"
    script.write_text(_PARENT, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != jobobject.DISABLE_ENV}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(CLIENT_ROOT), env.get("PYTHONPATH")]))
    parent = subprocess.Popen(
        [sys.executable, str(script), mode], stdout=subprocess.PIPE, text=True, env=env,
    )
    line = parent.stdout.readline().split()
    assert len(line) == 3, f"parent did not report its tree: {line!r}"
    state, child_pid, grandchild_pid = line[0], int(line[1]), int(line[2])
    return parent, state, [child_pid, grandchild_pid]


def _open(k, pids):
    handles = [k.OpenProcess(_SYNCHRONIZE | _PROCESS_TERMINATE, False, pid) for pid in pids]
    assert all(handles), "could not open the descendants before the kill"
    return handles


def _reap(k, parent, handles):
    for handle in handles:
        k.TerminateProcess(handle, 1)
        k.CloseHandle(handle)
    if parent.poll() is None:
        parent.kill()
    parent.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
def test_hard_killed_gateway_takes_child_and_grandchild_with_it(tmp_path):
    k = _kernel()
    parent, state, pids = _spawn_parent(tmp_path, "plain")
    handles = _open(k, pids)
    try:
        parent.kill()  # TerminateProcess: no finally, no atexit, no Backend.close()
        parent.wait(timeout=10)
        for pid, handle in zip(pids, handles):
            assert k.WaitForSingleObject(handle, 5000) == _WAIT_OBJECT_0, (
                f"pid {pid} outlived the hard-killed parent (state={state})"
            )
        assert state == "contained"
    finally:
        _reap(k, parent, handles)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
def test_breakaway_launch_outlives_the_gateway(tmp_path):
    """What a browser opened for a Decision Board, or an app opened by Hands,
    relies on: the job must not take it down with the session."""
    k = _kernel()
    parent, state, pids = _spawn_parent(tmp_path, "breakaway")
    handles = _open(k, pids)
    try:
        assert state == "contained"
        parent.kill()
        parent.wait(timeout=10)
        for pid, handle in zip(pids, handles):
            assert k.WaitForSingleObject(handle, 1500) == _WAIT_TIMEOUT, (
                f"pid {pid} launched outside the job died with the parent"
            )
    finally:
        _reap(k, parent, handles)
