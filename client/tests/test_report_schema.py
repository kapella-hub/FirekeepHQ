"""Structural property: no emitted event carries a value outside the fixed
vocabularies, whatever hostile exception text the mapper sees (spec: Testing,
'Structural')."""
import errno
import socket
import ssl
import subprocess

import pytest

from firekeep_client import report
from firekeep_client.transport import TransportError

HOSTILE = "/home/user/secret token=abc123 https://10.0.0.5:8100 C:\\Users\\x"


def test_vocabularies_are_closed_tuples():
    for vocab in (report.KINDS, report.INSTALL_STAGES, report.BOOTSTRAP_STAGES,
                  report.CONNECTIVITY_STAGES, report.RUNTIME_STAGES, report.ERRORS,
                  report.OS_FAMILIES, report.ARCHES, report.PY_BUCKETS,
                  report.RUNTIMES, report.DEX_NAMES, report.BACKENDS):
        assert isinstance(vocab, tuple) and all(isinstance(v, str) for v in vocab)


@pytest.mark.parametrize("exc,expected", [
    (PermissionError(errno.EACCES, HOSTILE), "permission-denied"),
    (OSError(errno.ENOSPC, HOSTILE), "disk-full"),
    (FileNotFoundError(errno.ENOENT, HOSTILE), "not-found"),
    (socket.gaierror(8, HOSTILE), "dns-failure"),
    (ConnectionRefusedError(errno.ECONNREFUSED, HOSTILE), "connection-refused"),
    (OSError(errno.ENETUNREACH, HOSTILE), "network-unreachable"),
    (TimeoutError(HOSTILE), "timeout"),
    (subprocess.TimeoutExpired(cmd=HOSTILE, timeout=1), "timeout"),
    (RuntimeError(HOSTILE), "other"),
    (TransportError(HOSTILE, status=401), "http-401"),
    (TransportError(HOSTILE, status=503), "http-5xx"),
    (TransportError(HOSTILE, category="dns-failure"), "dns-failure"),
    (TransportError(HOSTILE), "other"),
])
def test_map_error_never_reads_text(exc, expected):
    assert report.map_error(exc) == expected
    assert report.map_error(exc) in report.ERRORS


def test_ssl_verify_maps():
    assert report.map_error(ssl.SSLCertVerificationError(1, HOSTILE)) == "tls-verify-failed"


def test_build_event_is_union_strict():
    ev = report.build_event("install", "create-venv",
                            exc=PermissionError(errno.EACCES, HOSTILE), exit_code=1)
    assert ev is not None
    assert set(ev) <= {"id", "kind", "stage", "error", "exit", "os", "arch", "client", "py"}
    assert len(ev["id"]) == 32 and int(ev["id"], 16) >= 0
    assert ev["error"] == "permission-denied" and ev["exit"] == 1
    for field, vocab in (("kind", report.KINDS), ("os", report.OS_FAMILIES),
                         ("arch", report.ARCHES), ("py", report.PY_BUCKETS)):
        assert ev[field] in vocab
    assert HOSTILE not in str(ev)


def test_build_event_drops_off_vocabulary():
    assert report.build_event("install", "not-a-stage") is None
    assert report.build_event("nope", "create-venv") is None
    # union violations: runtime only on render-adapter, dex only on pip-install-dex,
    # backend only on runtime/gateway-call, exit only on install
    assert report.build_event("install", "create-venv", runtime="claude") is None
    assert report.build_event("connectivity", "cortex", exit_code=1) is None
    assert report.build_event("runtime", "gateway-call", backend="not-a-backend") is None
    ok = report.build_event("runtime", "gateway-call", exc=RuntimeError("x"), backend="cortex")
    assert ok is not None and ok["backend"] == "cortex"
    ok2 = report.build_event("install", "render-adapter", error="other", runtime="kiro")
    assert ok2 is not None and ok2["runtime"] == "kiro"


def test_exit_out_of_range_is_omitted():
    ev = report.build_event("install", "create-venv", error="other", exit_code=7000)
    assert ev is not None and "exit" not in ev


def test_transport_error_category_assigned_at_wrap_time():
    """Real WRAPPED failures, not synthetic bare exceptions (spec: 'Transport
    contract')."""
    import urllib.error
    from firekeep_client.transport import _as_transport_error
    wrapped = urllib.error.URLError(socket.gaierror(8, HOSTILE))
    te = _as_transport_error(wrapped, method="GET", url="https://x", timeout=1)
    assert te.category == "dns-failure"
    wrapped2 = urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, HOSTILE))
    assert _as_transport_error(wrapped2, method="GET", url="https://x", timeout=1).category == "connection-refused"
    wrapped3 = urllib.error.URLError(ssl.SSLCertVerificationError(1, HOSTILE))
    assert _as_transport_error(wrapped3, method="GET", url="https://x", timeout=1).category == "tls-verify-failed"
    assert _as_transport_error(TimeoutError(), method="GET", url="https://x", timeout=1).category == "timeout"


@pytest.mark.xfail(reason="cli wiring lands in Task 4", strict=True)
def test_install_stages_cover_every_cmd_install_step():
    """A new install step with no mapping must FAIL here, not land in 'other'
    (spec: Testing, 'Enum exhaustiveness')."""
    import inspect
    import re
    from firekeep_client import cli
    src = inspect.getsource(cli.cmd_install)
    fixed = re.findall(r'^\s*step = "([^"]+)"$', src, re.MULTILINE)
    interpolated = re.findall(r'^\s*step = f"([^"]+)"$', src, re.MULTILINE)
    for literal in fixed:
        assert cli._stage_slug(literal)[0] in report.INSTALL_STAGES, literal
    assert len(interpolated) == 2  # render {name} adapter / pip install {dex} ...
    slug, extra = cli._stage_slug("render claude adapter")
    assert slug == "render-adapter" and extra == {"runtime": "claude"}
    slug, extra = cli._stage_slug("pip install docdex (local checkout dir)")
    assert slug == "pip-install-dex" and extra == {"dex": "docdex"}


def test_runtime_stages_cover_every_hook_core():
    from firekeep_client.hooks import __main__ as hooks_main
    for core in hooks_main._CORE_MODULES:
        assert core.replace("_", "-") in report.RUNTIME_STAGES, core


def test_connectivity_stages_cover_doctor_service_ids():
    for svc in resolver_services():
        assert svc in report.CONNECTIVITY_STAGES


def resolver_services():
    from firekeep_client import resolver
    return resolver.SERVICES
