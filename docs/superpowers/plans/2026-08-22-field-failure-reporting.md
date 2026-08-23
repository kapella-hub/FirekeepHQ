# Field Failure Reporting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enum-only failure telemetry: install/connectivity/runtime failures on user machines reach firekeep.ai, are pulled by the VPS, and land in Sentinel — with tri-state consent and no free text anywhere.

**Architecture:** A new stdlib-only `client/firekeep_client/report.py` (vocabularies, mapper, consent, spool, flush) is wired into five existing chokepoints; both bootstraps gain a consent ask + fire-and-forget POST; a new `failure-report.php` collector (firekeep-site repo) validates enum VALUES under one flock'd section, seals immutable segments, and mails within a budget; a VPS cron pulls sealed segments into a durable inbox and POSTs aggregates to a new authenticated `POST /events` on Sentinel.

**Tech Stack:** Python 3.11+ stdlib (client), POSIX sh + PowerShell (bootstraps), PHP 8 shared hosting (collector), FastMCP/Starlette + pydantic (Sentinel), pytest.

**Spec:** `docs/superpowers/specs/2026-08-22-field-failure-reporting-design.md` — read it before any task; every task cites its sections.

## Global Constraints

- Client code is **stdlib-only** (the hooks import boundary): `report.py` may import only stdlib + `firekeep_client` modules that are already stdlib-only (`resolver`, `transport`, `state`, `hooklog`). No new dependencies anywhere (`client/` is deliberately unlocked; server locks untouched).
- **Never raises, never changes an exit code, never prints a traceback**: every public function in `report.py` and every emit/flush call site follows `_send_doctor_report`'s discipline (cli.py:1730).
- **No free text ever enters an event.** Every field validated against a closed vocabulary client-side (off-vocabulary → event silently dropped) and server-side (→ rejected). `str(exc)` is never consulted for a field value.
- Consent is **tri-state**: `[report] failures` absent = OFF. `FIREKEEP_NO_FAILURE_REPORT` (env) always wins as off; `FIREKEEP_FAILURE_REPORT` (env) is session-scoped opt-in. `resolver.is_bypassed()` (personal mode) silences emit and flush.
- Wire constants (all sides must agree): endpoint `https://firekeep.ai/failure-report.php`; batch `{"events": [...]}` max 64 events; response `{"accepted": [ids], "rejected": [ids], "sealed": n, "active_bytes": n}`; event `id` = 32 lowercase hex chars; spool caps 64 events / 32KB; collector body cap 40KB; seal at 4MB or 6h; sealed cap 256MB; signatures cap 4096; dedup ring 8192 ids; mail budget 5/hour; stale spool claim 600s.
- `install.sh` is POSIX sh (dash-compatible, macOS bash 3.2) — no bashisms. PHP: no `exec`/`shell_exec`/`popen`/`proc_open` (disabled on Hostinger); `flock` and `mail()` available (**re-verify on the host before Task 10**, per spec "Architecture").
- Site repo is a separate checkout: `E:\Documents\Projects\firekeep-site` (no git remote; deploy is the user's established tar-over-SSH flow — never ask how).
- Severity vocabulary on Sentinel is `info|warning|error|critical`; this feature uses only `info`/`warning` (never `error` — `ALERT_SEVERITIES` fan-out).
- Commit after every task; `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Phases

| Phase | Tasks | Ships on its own as |
|---|---|---|
| 1. Client | 1–7 | emit/spool/consent working against a stub collector |
| 2. Bootstraps | 8–9 | consent-before-provisioning + die-POST |
| 3. Site collector | 10–11 | live collection + budgeted mail (before Sentinel exists) |
| 4. Sentinel + VPS | 12–13 | dashboard/agent-visible field failures |
| 5. Docs/contract | 14 | Change Consistency Checklist closed |

---

### Task 1: `report.py` vocabularies, event builder, error mapper; `TransportError.category`

**Files:**
- Create: `client/firekeep_client/report.py`
- Modify: `client/firekeep_client/transport.py` (TransportError + `_as_transport_error`)
- Test: `client/tests/test_report_schema.py`

**Interfaces:**
- Consumes: `transport.TransportError` (gains `category: str | None`).
- Produces (later tasks rely on these exact names): module constants `KINDS`, `INSTALL_STAGES`, `BOOTSTRAP_STAGES`, `CONNECTIVITY_STAGES`, `RUNTIME_STAGES`, `ERRORS`, `OS_FAMILIES`, `ARCHES`, `PY_BUCKETS`, `RUNTIMES`, `DEX_NAMES`, `BACKENDS`, `REPORT_URL`, `SPOOL_MAX_EVENTS`, `SPOOL_MAX_BYTES`; functions `map_error(exc) -> str`, `detect_os() -> str`, `detect_arch() -> str`, `py_bucket() -> str`, `build_event(kind, stage, *, error=None, exc=None, exit_code=None, runtime=None, dex=None, backend=None) -> dict | None`.

- [ ] **Step 1: Write the failing schema tests**

```python
# client/tests/test_report_schema.py
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
```

- [ ] **Step 2: Run to verify failure** — `cd client && python -m pytest tests/test_report_schema.py -v` — Expected: FAIL (`No module named firekeep_client.report` / no `category`).

- [ ] **Step 3: Add `category` to TransportError**

In `client/firekeep_client/transport.py`: add `import errno` and `import socket` to the imports; extend the class and translator:

```python
class TransportError(Exception):
    def __init__(
        self,
        msg,
        *,
        status: int | None = None,
        response_is_json: bool = False,
        category: str | None = None,
    ) -> None:
        super().__init__(msg)
        self.status = status
        self.response_is_json = response_is_json
        # Structured failure class assigned AT WRAP TIME (field-failure spec,
        # "The mapper's input contract"): the report mapper consumes only
        # (category, status) and never re-traverses causes or reads messages.
        self.category = category


_CATEGORY_ERRNOS = {
    errno.ECONNREFUSED: "connection-refused",
    errno.ENETUNREACH: "network-unreachable",
    errno.EHOSTUNREACH: "network-unreachable",
}


def _failure_category(exc: Exception) -> str | None:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "tls-verify-failed"
    if isinstance(reason, socket.gaierror):
        return "dns-failure"
    if isinstance(reason, TimeoutError):
        return "timeout"
    if isinstance(reason, OSError):
        return _CATEGORY_ERRNOS.get(reason.errno)
    return None
```

In `_as_transport_error`, thread it through (HTTPError branch unchanged — `.status` carries the class):

```python
    if isinstance(exc, TimeoutError):
        return TransportError(f"{method} {url} timed out after {timeout}s",
                              category="timeout")
    return TransportError(f"{method} {url} unreachable: {exc.reason}",
                          category=_failure_category(exc))
```

- [ ] **Step 4: Write `report.py` (vocabularies + builder + mapper)**

```python
# client/firekeep_client/report.py
"""Field failure reporting — structured-enum-only telemetry.

Spec: docs/superpowers/specs/2026-08-22-field-failure-reporting-design.md.
Every field of every event is drawn from a closed vocabulary; exception TEXT is
never consulted (only classes, errnos, TransportError.category/.status). Every
public function never raises and never blocks meaningfully — a failed report
must never affect the command that emitted it (same discipline as
cli._send_doctor_report).
"""
from __future__ import annotations

import errno as _errno
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import uuid
from pathlib import Path

from firekeep_client import resolver
from firekeep_client.transport import TransportError

REPORT_URL = "https://firekeep.ai/failure-report.php"
SPOOL_MAX_EVENTS = 64
SPOOL_MAX_BYTES = 32 * 1024
STALE_CLAIM_SECONDS = 600
FLUSH_TIMEOUT = 2.0

KINDS = ("install", "connectivity", "runtime")
INSTALL_STAGES = (
    "bootstrap-home", "configure-config", "create-venv", "pip-install-client",
    "pip-install-dex", "lock-config-perms", "select-version", "render-adapters",
    "render-adapter", "add-to-path", "join-server",
)
BOOTSTRAP_STAGES = (
    "detect-platform", "fetch-manifest", "verify-checksum", "provision-python",
    "create-venv", "install-wheels", "runnable-check", "flip-current", "handoff",
)
CONNECTIVITY_STAGES = ("cortex", "bridge", "sentinel", "relay", "server",
                       "embeddings", "backup")
RUNTIME_STAGES = (
    # hook-core names, hyphenated (exhaustiveness test pins these against
    # hooks.__main__._CORE_MODULES) + the two gateway seams
    "session-start", "prompt", "pre-tool", "post-tool", "stop", "session-end",
    "precompact", "gateway-call", "gateway-dispatch",
)
ERRORS = (
    "permission-denied", "disk-full", "not-found", "dns-failure",
    "connection-refused", "network-unreachable", "tls-verify-failed", "timeout",
    "http-401", "http-403", "http-404", "http-429", "http-5xx",
    "unsupported-platform", "other",
)
OS_FAMILIES = ("darwin", "linux-gnu", "linux-musl", "windows")
ARCHES = ("x86_64", "arm64", "other")
PY_BUCKETS = ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14", "other")
RUNTIMES = ("claude", "codex", "kiro", "opencode", "claude-desktop", "generic")
DEX_NAMES = ("symdex", "docdex", "maildex")
BACKENDS = ("cortex", "bridge", "sentinel", "relay")

_STAGES_BY_KIND = {
    "install": INSTALL_STAGES + BOOTSTRAP_STAGES,
    "connectivity": CONNECTIVITY_STAGES,
    "runtime": RUNTIME_STAGES,
}

_ERRNO_MAP = {
    _errno.EACCES: "permission-denied",
    _errno.EPERM: "permission-denied",
    _errno.ENOSPC: "disk-full",
    _errno.ENOENT: "not-found",
    _errno.ECONNREFUSED: "connection-refused",
    _errno.ENETUNREACH: "network-unreachable",
    _errno.EHOSTUNREACH: "network-unreachable",
}


def map_error(exc) -> str:
    """Exception -> error class. Classes, errnos and TransportError's structured
    (category, status) ONLY — never str(exc). Unrecognised -> 'other'."""
    if isinstance(exc, TransportError):
        if exc.category in ERRORS:
            return exc.category
        if exc.status in (401, 403, 404, 429):
            return f"http-{exc.status}"
        if exc.status is not None and 500 <= exc.status < 600:
            return "http-5xx"
        return "other"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls-verify-failed"
    if isinstance(exc, socket.gaierror):
        return "dns-failure"
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    if isinstance(exc, OSError):
        return _ERRNO_MAP.get(exc.errno, "other")
    return "other"


def detect_os() -> str:
    """'' when the platform is outside the vocabulary — the event is then
    dropped rather than mislabelled (structural, never approximate)."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        import platform as _platform
        libc, _ = _platform.libc_ver()
        return "linux-gnu" if libc == "glibc" else "linux-musl"
    return ""


def detect_arch() -> str:
    import platform as _platform
    machine = _platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return "other"


def py_bucket() -> str:
    v = f"{sys.version_info[0]}.{sys.version_info[1]}"
    return v if v in PY_BUCKETS else "other"


def build_event(kind, stage, *, error=None, exc=None, exit_code=None,
                runtime=None, dex=None, backend=None) -> dict | None:
    """Validated event dict, or None (dropped) on ANY off-vocabulary or
    union-violating input. Strict tagged union per the spec's Event schema."""
    try:
        if kind not in KINDS or stage not in _STAGES_BY_KIND.get(kind, ()):
            return None
        err = error if error is not None else map_error(exc) if exc is not None else None
        if err not in ERRORS:
            return None
        os_family = detect_os()
        if os_family not in OS_FAMILIES:
            return None
        from firekeep_client import __version__
        event = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "stage": stage,
            "error": err,
            "os": os_family,
            "arch": detect_arch(),
            "client": __version__,
            "py": py_bucket(),
        }
        # Tagged union: each extra field only where its capture point produces it.
        if exit_code is not None:
            if kind != "install":
                return None
            if isinstance(exit_code, int) and 0 <= exit_code <= 255:
                event["exit"] = exit_code  # out-of-range: omitted, event kept
        if runtime is not None:
            if stage != "render-adapter" or runtime not in RUNTIMES:
                return None
            event["runtime"] = runtime
        if dex is not None:
            if stage != "pip-install-dex" or dex not in DEX_NAMES:
                return None
            event["dex"] = dex
        if backend is not None:
            if kind != "runtime" or stage != "gateway-call" or backend not in BACKENDS:
                return None
            event["backend"] = backend
        return event
    except Exception:  # noqa: BLE001 — a broken builder must never cost a command
        return None
```

- [ ] **Step 5: Run tests** — `cd client && python -m pytest tests/test_report_schema.py -v` — Expected: PASS. (If `libc_ver` returns `('glibc', v)` differently on your box, fix `detect_os`, not the test.)

- [ ] **Step 6: Exhaustiveness tests** — append to `client/tests/test_report_schema.py`:

```python
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
```

Run: expected FAIL on `cli._stage_slug` (Task 4 adds it) and possibly on `RUNTIME_STAGES` (the literal must match `_CORE_MODULES` exactly — the failure prints the real core names; fix the `RUNTIME_STAGES` literal to `sorted(name.replace("_","-"))` of those keys, keeping `gateway-call`/`gateway-dispatch`). Mark the two failing tests `@pytest.mark.xfail(reason="cli wiring lands in Task 4", strict=True)` for now; Task 4 removes the marks.

- [ ] **Step 7: Commit** — `git add client/firekeep_client/report.py client/firekeep_client/transport.py client/tests/test_report_schema.py && git commit -m "feat(report): event vocabularies, strict union builder, structured error mapper"`

---

### Task 2: Consent — `is_enabled`, `record_consent`, `has_answer`, `ask_consent`

**Files:**
- Modify: `client/firekeep_client/report.py`
- Test: `client/tests/test_report_consent.py`

**Interfaces:**
- Produces: `is_enabled(cfg=None) -> bool` (tri-state; env off > bypass > env on > explicit config true); `has_answer(cfg) -> bool`; `record_consent(cfg, value: bool) -> None` (mutates cfg only — caller persists); `ask_consent(cfg) -> bool` (True iff an answer was recorded; EOF/^C record nothing); `CONSENT_PROMPT` (exact spec wording).
- Consumes: `resolver.is_bypassed()`, `resolver.load_config()`.

- [ ] **Step 1: Failing tests**

```python
# client/tests/test_report_consent.py
"""Tri-state consent (spec Decision 1): unset = OFF, never mirrors
autoupdate.is_enabled's default-ON."""
import builtins
import configparser

import pytest

from firekeep_client import report


def _cfg(text=""):
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("FIREKEEP_NO_FAILURE_REPORT", "FIREKEEP_FAILURE_REPORT",
                "FIREKEEP_REPORT_CONSENT", "FIREKEEP_BYPASS"):
        monkeypatch.delenv(var, raising=False)


def test_unset_means_off():
    assert report.is_enabled(_cfg()) is False
    assert report.is_enabled(_cfg("[report]\n")) is False
    assert report.is_enabled(_cfg("[report]\nfailures =\n")) is False


def test_explicit_true_means_on_and_false_off():
    assert report.is_enabled(_cfg("[report]\nfailures = true\n")) is True
    assert report.is_enabled(_cfg("[report]\nfailures = false\n")) is False


def test_env_off_beats_config_true(monkeypatch):
    monkeypatch.setenv("FIREKEEP_NO_FAILURE_REPORT", "1")
    assert report.is_enabled(_cfg("[report]\nfailures = true\n")) is False


def test_env_on_is_session_opt_in(monkeypatch):
    monkeypatch.setenv("FIREKEEP_FAILURE_REPORT", "1")
    assert report.is_enabled(_cfg()) is True


def test_personal_mode_silences(monkeypatch):
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    assert report.is_enabled(_cfg("[report]\nfailures = true\n")) is False


def test_ask_consent_eof_records_nothing(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda self: True})())
    def raise_eof(prompt=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)
    assert report.ask_consent(cfg) is False
    assert report.has_answer(cfg) is False


def test_ask_consent_enter_is_yes(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")
    assert report.ask_consent(cfg) is True
    assert cfg.get("report", "failures") == "true"


def test_ask_consent_no(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(builtins, "input", lambda prompt="": "n")
    assert report.ask_consent(cfg) is True
    assert cfg.get("report", "failures") == "false"


def test_ask_consent_env_prefill_no_tty(monkeypatch):
    """The bootstrap answers once; the wizard hand-off must not re-ask
    (FIREKEEP_REPORT_CONSENT set by install.sh/install.ps1, Task 8/9)."""
    cfg = _cfg()
    monkeypatch.setenv("FIREKEEP_REPORT_CONSENT", "1")
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda self: False})())
    assert report.ask_consent(cfg) is True
    assert cfg.get("report", "failures") == "true"


def test_ask_consent_never_reasks(monkeypatch):
    cfg = _cfg("[report]\nfailures = false\n")
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail("re-asked"))
    assert report.ask_consent(cfg) is False
```

- [ ] **Step 2: Run to verify failure** — `cd client && python -m pytest tests/test_report_consent.py -v` — Expected: FAIL (missing functions).

- [ ] **Step 3: Implement in `report.py`**

```python
_FALSEY = ("", "0", "false", "no", "off")

CONSENT_PROMPT = (
    "Send anonymous failure reports to firekeep.ai? When an install step fails, a\n"
    "connection to your own Keep fails, or a Firekeep background task errors,\n"
    "Firekeep sends category codes only — what failed, the error class, OS family,\n"
    "versions. Never paths, messages, addresses, or any persistent device, account\n"
    "or session identifier. Ongoing until you turn it off\n"
    "([report] failures = false). [Y/n] "
)


def is_enabled(cfg=None) -> bool:
    """Tri-state consent gate (spec Decision 1). Deliberately does NOT mirror
    autoupdate.is_enabled: a missing [report] section means NOT ENROLLED, so a
    machine that was never shown the prompt (headless install, join-code
    onboarding, every upgrade of the existing base) never reports. Personal
    mode silences everything ('nothing ... sent to the server')."""
    try:
        if os.environ.get("FIREKEEP_NO_FAILURE_REPORT", "").strip().lower() not in _FALSEY:
            return False
        if resolver.is_bypassed():
            return False
        if os.environ.get("FIREKEEP_FAILURE_REPORT", "").strip().lower() not in _FALSEY:
            return True
        if cfg is None:
            cfg = resolver.load_config()
        return cfg.get("report", "failures", fallback="").strip().lower() == "true"
    except Exception:  # noqa: BLE001 — any doubt means OFF (fail closed)
        return False


def has_answer(cfg) -> bool:
    try:
        return cfg.get("report", "failures", fallback="").strip() != ""
    except Exception:  # noqa: BLE001
        return False


def record_consent(cfg, value: bool) -> None:
    if not cfg.has_section("report"):
        cfg.add_section("report")
    cfg.set("report", "failures", "true" if value else "false")


def ask_consent(cfg) -> bool:
    """Ask once; record only a real answer. EOF and Ctrl-C record NOTHING —
    deliberately not wizard.console_ask, whose EOF-takes-the-default would
    silently enroll (spec, 'Where the asks live'). Returns True iff an answer
    was recorded into cfg (caller persists)."""
    try:
        if has_answer(cfg):
            return False
        pre = os.environ.get("FIREKEEP_REPORT_CONSENT", "").strip()
        if pre in ("0", "1"):
            record_consent(cfg, pre == "1")
            return True
        if not sys.stdin.isatty():
            return False
        try:
            answer = input(CONSENT_PROMPT).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        record_consent(cfg, answer in ("", "y", "yes"))
        return True
    except Exception:  # noqa: BLE001
        return False
```

- [ ] **Step 4: Run tests** — `cd client && python -m pytest tests/test_report_consent.py tests/test_report_schema.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** — `git add -A client/firekeep_client/report.py client/tests/test_report_consent.py && git commit -m "feat(report): tri-state consent — unset is OFF, EOF records nothing, personal mode silences"`

---

### Task 3: Spool, `emit`, `flush` — claim-by-rename with crash recovery

**Files:**
- Modify: `client/firekeep_client/report.py`
- Test: `client/tests/test_report_spool.py`

**Interfaces:**
- Produces: `emit(kind, stage, *, error=None, exc=None, exit_code=None, runtime=None, dex=None, backend=None, cfg=None) -> None`; `flush(cfg=None, timeout=FLUSH_TIMEOUT) -> None`; `_report_dir() -> Path` (honours `FIREKEEP_REPORT_DIR` env for tests, else `~/.firekeep`); `_post(url, body, timeout)` (test seam).
- Wire contract consumed by Task 10's PHP: one POST `{"events": [...]}` (≤64), response `{"accepted": [...], "rejected": [...]}`; unacknowledged events merge back; rejected events are dropped (client bug — never retried).

- [ ] **Step 1: Failing tests**

```python
# client/tests/test_report_spool.py
"""Spool protocol (spec, 'Spool concurrency — claim by rename, with crash
recovery'): no rewrite anywhere, stale claims adopted, at-least-once with
ack-based truncation."""
import json
import os
import time

import pytest

from firekeep_client import report
from firekeep_client.transport import TransportError


@pytest.fixture(autouse=True)
def report_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("FIREKEEP_FAILURE_REPORT", "1")  # consent for tests
    monkeypatch.delenv("FIREKEEP_NO_FAILURE_REPORT", raising=False)
    return tmp_path


def _spool(report_dir):
    return report_dir / "report-spool.jsonl"


def _events(report_dir):
    p = _spool(report_dir)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_emit_spools_when_collector_down(report_dir, monkeypatch):
    def refuse(*a, **k):
        raise TransportError("refused", category="connection-refused")
    monkeypatch.setattr(report, "_post", refuse)
    report.emit("install", "create-venv", error="disk-full", exit_code=1)
    evs = _events(report_dir)
    assert len(evs) == 1 and evs[0]["error"] == "disk-full"


def test_emit_never_raises_on_garbage_collector(report_dir, monkeypatch):
    monkeypatch.setattr(report, "_post", lambda *a, **k: "not a dict")
    report.emit("install", "create-venv", error="other")  # must not raise
    assert len(_events(report_dir)) == 1  # unacked -> merged back


def test_flush_truncates_only_acked(report_dir, monkeypatch):
    for stage in ("create-venv", "pip-install-client", "add-to-path"):
        report._append_spool(report.build_event("install", stage, error="other"))
    ids = [e["id"] for e in _events(report_dir)]
    monkeypatch.setattr(report, "_post",
                        lambda url, body, timeout: {"accepted": ids[:2], "rejected": []})
    report.flush()
    left = _events(report_dir)
    assert [e["id"] for e in left] == [ids[2]]


def test_rejected_ids_are_dropped_not_retried(report_dir, monkeypatch):
    report._append_spool(report.build_event("install", "create-venv", error="other"))
    the_id = _events(report_dir)[0]["id"]
    monkeypatch.setattr(report, "_post",
                        lambda url, body, timeout: {"accepted": [], "rejected": [the_id]})
    report.flush()
    assert _events(report_dir) == []


def test_stale_claim_is_adopted(report_dir, monkeypatch):
    stale = report_dir / "report-spool.sending.99999"
    ev = report.build_event("install", "create-venv", error="other")
    stale.write_text(json.dumps(ev) + "\n")
    old = time.time() - report.STALE_CLAIM_SECONDS - 5
    os.utime(stale, (old, old))
    sent = []

    def ack_all(url, body, timeout):
        sent.extend(body["events"])
        return {"accepted": [e["id"] for e in body["events"]], "rejected": []}

    monkeypatch.setattr(report, "_post", ack_all)
    report.flush()
    assert [e["id"] for e in sent] == [ev["id"]]
    assert not stale.exists() and _events(report_dir) == []


def test_fresh_claim_not_adopted(report_dir, monkeypatch):
    fresh = report_dir / "report-spool.sending.88888"
    fresh.write_text(json.dumps(report.build_event("install", "create-venv", error="other")) + "\n")
    monkeypatch.setattr(report, "_post", lambda *a, **k: {"accepted": [], "rejected": []})
    report.flush()
    assert fresh.exists()  # another live flusher owns it


def test_spool_capped_oldest_dropped(report_dir, monkeypatch):
    def refuse(*a, **k):
        raise TransportError("down", category="connection-refused")
    monkeypatch.setattr(report, "_post", refuse)
    for _ in range(report.SPOOL_MAX_EVENTS + 10):
        report._append_spool(report.build_event("install", "create-venv", error="other"))
    report.flush()  # claim -> fail -> merge back trims to cap
    assert len(_events(report_dir)) == report.SPOOL_MAX_EVENTS


def test_local_dedup_24h(report_dir, monkeypatch):
    def refuse(*a, **k):
        raise TransportError("down", category="connection-refused")
    monkeypatch.setattr(report, "_post", refuse)
    report.emit("runtime", "gateway-dispatch", error="other")
    report.emit("runtime", "gateway-dispatch", error="other")  # identical enums
    assert len(_events(report_dir)) == 1


def test_emit_disabled_writes_nothing(report_dir, monkeypatch):
    monkeypatch.delenv("FIREKEEP_FAILURE_REPORT", raising=False)
    report.emit("install", "create-venv", error="other")
    assert not _spool(report_dir).exists()
```

- [ ] **Step 2: Run to verify failure** — `cd client && python -m pytest tests/test_report_spool.py -v` — Expected: FAIL (missing `_post`/`emit`/`flush`).

- [ ] **Step 3: Implement in `report.py`**

```python
def _report_dir() -> Path:
    override = os.environ.get("FIREKEEP_REPORT_DIR", "").strip()
    return Path(override) if override else Path.home() / ".firekeep"


def _spool_path() -> Path:
    return _report_dir() / "report-spool.jsonl"


def _recent_path() -> Path:
    return _report_dir() / "report-recent.json"


def _post(url, body, timeout):
    """Seam for tests. transport.post_json raises TransportError on failure."""
    from firekeep_client.transport import post_json
    return post_json(url, body, headers={}, timeout=timeout)


def _append_spool(event: dict | None) -> None:
    """One O_APPEND single-line write — the only way ANYTHING enters the spool
    (emit and merge-back both use it; there is no whole-file rewrite in the
    protocol, so a concurrent append can never be corrupted)."""
    if event is None:
        return
    try:
        path = _spool_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 2 * SPOOL_MAX_BYTES:
            return  # flush is broken entirely; never grow unbounded
        line = json.dumps(event, separators=(",", ":")) + "\n"
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001
        pass


def _dedup_key(event: dict) -> str:
    return "|".join(str(event.get(k, "")) for k in
                    ("kind", "stage", "error", "runtime", "dex", "backend"))


def _recently_sent(event: dict) -> bool:
    """A hot failing hook must not fill the spool with copies: identical enum
    tuples are emitted at most once per 24h. Best-effort only."""
    try:
        now = time.time()
        path = _recent_path()
        recent = {}
        if path.exists():
            recent = {k: v for k, v in json.loads(path.read_text()).items()
                      if now - v < 86400}
        key = _dedup_key(event)
        if key in recent:
            return True
        recent[key] = now
        path.write_text(json.dumps(recent), encoding="utf-8")
        return False
    except Exception:  # noqa: BLE001
        return False


def emit(kind, stage, *, error=None, exc=None, exit_code=None,
         runtime=None, dex=None, backend=None, cfg=None) -> None:
    """Record a failure. Never raises, never blocks meaningfully (spool append
    + one ~2s flush attempt). Spool FIRST: the highest-value report is an
    install failure on a machine that may have no network right now."""
    try:
        if not is_enabled(cfg):
            return
        event = build_event(kind, stage, error=error, exc=exc, exit_code=exit_code,
                            runtime=runtime, dex=dex, backend=backend)
        if event is None or _recently_sent(event):
            return
        _append_spool(event)
        flush(cfg=cfg)
    except Exception:  # noqa: BLE001
        pass


def _read_events(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if isinstance(ev, dict) and isinstance(ev.get("id"), str):
                    out.append(ev)
            except json.JSONDecodeError:
                continue  # torn line from a crash mid-append: skip, never poison
    except OSError:
        pass
    return out


def _claim(path: Path) -> Path | None:
    """Atomic rename = exactly one owner. Failure means someone else won."""
    target = path.parent / f"report-spool.sending.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    try:
        os.rename(path, target)
        return target
    except OSError:
        return None


def _adoptable_claims() -> list[Path]:
    """Claims whose owner died: older than STALE_CLAIM_SECONDS. Adoption is
    itself by rename, so racing adopters resolve to exactly one winner."""
    out = []
    try:
        now = time.time()
        for candidate in _report_dir().glob("report-spool.sending.*"):
            try:
                if now - candidate.stat().st_mtime > STALE_CLAIM_SECONDS:
                    adopted = _claim(candidate)
                    if adopted is not None:
                        out.append(adopted)
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return out


def _merge_back(events: list[dict]) -> None:
    """Per-line appends of the newest <= cap events — never a rewrite."""
    for event in events[-SPOOL_MAX_EVENTS:]:
        _append_spool(event)


def flush(cfg=None, timeout=FLUSH_TIMEOUT) -> None:
    """Send everything spooled. Never raises. At-least-once: truncation is
    ack-driven; replays are absorbed by the collector's dedup ring."""
    try:
        if not is_enabled(cfg):
            return
        claims = _adoptable_claims()
        spool = _spool_path()
        if spool.exists():
            fresh = _claim(spool)
            if fresh is not None:
                claims.append(fresh)
        for claim_path in claims:
            events = _read_events(claim_path)[-SPOOL_MAX_EVENTS:]
            try:
                claim_path.unlink()
            except OSError:
                pass
            if not events:
                continue
            try:
                resp = _post(REPORT_URL, {"events": events}, timeout)
            except Exception:  # noqa: BLE001 — TransportError, OSError, HTTPException
                _merge_back(events)
                continue
            if not isinstance(resp, dict):
                _merge_back(events)
                continue
            acked = set(resp.get("accepted") or []) | set(resp.get("rejected") or [])
            leftover = [e for e in events if e["id"] not in acked]
            if leftover:
                _merge_back(leftover)
    except Exception:  # noqa: BLE001
        pass
```

Note the unlink-then-merge-back ordering: the claim file is consumed into
memory, deleted, and unsent events re-enter via appends. A crash inside that
window loses at most one batch; the alternative ordering (delete after send)
guarantees whole-batch replay on every crash-after-send. Keep this ordering.

- [ ] **Step 4: Run tests** — `cd client && python -m pytest tests/test_report_spool.py tests/test_report_consent.py tests/test_report_schema.py -v` — Expected: PASS.

- [ ] **Step 5: Two-process flush test** — append to `test_report_spool.py`:

```python
def test_two_process_flush_empties_spool_exactly(report_dir):
    """Racing flushers (spec Testing, 'Spool'): claim-by-rename means at most
    one sender; the spool ends empty with no stranded claim files."""
    import subprocess
    import sys
    for _ in range(10):
        report._append_spool(report.build_event("install", "create-venv", error="other"))
    worker = (
        "from firekeep_client import report\n"
        "report._post = lambda url, body, timeout: "
        "{'accepted': [e['id'] for e in body['events']], 'rejected': []}\n"
        "report.flush()\n"
    )
    env = dict(os.environ, FIREKEEP_REPORT_DIR=str(report_dir),
               FIREKEEP_FAILURE_REPORT="1")
    procs = [subprocess.Popen([sys.executable, "-c", worker], env=env)
             for _ in range(2)]
    for p in procs:
        assert p.wait(30) == 0
    assert _events(report_dir) == []
    assert list(report_dir.glob("report-spool.sending.*")) == []
```

Run: PASS. (Duplication-freedom is structural — only one process can win the
rename — so asserting empty-spool + no-stranded-claims is the honest check
without a shared network fixture.)

- [ ] **Step 6: Commit** — `git add -A client && git commit -m "feat(report): spool with claim-by-rename, stale-claim adoption, ack-driven at-least-once flush"`

---

### Task 4: Wire the capture points — cmd_install, doctor, hooklog, gateway

**Files:**
- Modify: `client/firekeep_client/cli.py` (cmd_install handlers :569-579; `_check_health` :860-878; embeddings/backup catch sites; new `_stage_slug`)
- Modify: `client/firekeep_client/hooklog.py`, `client/firekeep_client/hooks/__main__.py` (:323-325), `client/firekeep_client/gateway.py` (:418-420, :462-463)
- Test: `client/tests/test_report_capture.py`; un-xfail the two tests from Task 1 Step 6

**Interfaces:**
- Produces: `cli._stage_slug(step: str) -> tuple[str, dict]` (slug + union extras); `hooklog.log_failure(hook, message, exc=None)` (existing two-arg call sites keep working).
- Consumes: `report.emit`, `report.map_error` (Tasks 1–3 signatures).

- [ ] **Step 1: Failing tests**

```python
# client/tests/test_report_capture.py
import errno

import pytest

from firekeep_client import cli, hooklog, report
from firekeep_client.transport import TransportError


@pytest.fixture(autouse=True)
def enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("FIREKEEP_FAILURE_REPORT", "1")
    return tmp_path


def _capture_emits(monkeypatch):
    calls = []

    def fake_emit(kind, stage, **kw):
        calls.append((kind, stage, kw))
    monkeypatch.setattr(report, "emit", fake_emit)
    return calls


def test_stage_slug_fixed_and_interpolated():
    assert cli._stage_slug("create venv") == ("create-venv", {})
    assert cli._stage_slug("bootstrap ~/.firekeep") == ("bootstrap-home", {})
    assert cli._stage_slug("render kiro adapter") == ("render-adapter", {"runtime": "kiro"})
    assert cli._stage_slug("pip install maildex (local checkout dir)") == (
        "pip-install-dex", {"dex": "maildex"})
    assert cli._stage_slug("total nonsense") == ("", {})  # unmapped -> build_event drops


class _EP:
    headers = {}
    verify = True

    def __init__(self, svc):
        self.rest_base = f"http://x/{svc}"


def test_check_health_partial_failure_emits_per_service(monkeypatch):
    calls = _capture_emits(monkeypatch)

    def fake_get(url, headers, verify):
        if "cortex" in url:
            raise TransportError("refused", category="connection-refused")
        return {"ok": True}

    monkeypatch.setattr(cli.resolver, "resolve", lambda svc, cfg=None: _EP(svc))
    monkeypatch.setattr(cli, "get_json", fake_get)
    cli._check_health(cfg=None)
    assert [(k, s, kw.get("error")) for k, s, kw in calls] == [
        ("connectivity", "cortex", "connection-refused")]


def test_check_health_all_down_emits_one_server_event(monkeypatch):
    calls = _capture_emits(monkeypatch)

    def refuse(url, headers, verify):
        raise TransportError("refused", category="connection-refused")

    monkeypatch.setattr(cli.resolver, "resolve", lambda svc, cfg=None: _EP(svc))
    monkeypatch.setattr(cli, "get_json", refuse)
    cli._check_health(cfg=None)
    assert [(k, s, kw.get("error")) for k, s, kw in calls] == [
        ("connectivity", "server", "connection-refused")]


def test_log_failure_with_exc_emits_runtime_event(monkeypatch):
    calls = _capture_emits(monkeypatch)
    hooklog.log_failure("session_start", "GET /briefing failed",
                        exc=PermissionError(errno.EACCES, "x"))
    assert len(calls) == 1 and calls[0][:2] == ("runtime", "session-start")


def test_log_failure_without_exc_emits_nothing(monkeypatch):
    calls = _capture_emits(monkeypatch)
    hooklog.log_failure("stop", "just a message")
    assert calls == []
```

- [ ] **Step 2: Run to verify failure** — `cd client && python -m pytest tests/test_report_capture.py -v` — Expected: FAIL (`_stage_slug` missing; no emits).

- [ ] **Step 3: `_stage_slug` + cmd_install wiring in `cli.py`**

Add `from firekeep_client import report` to cli.py's imports, then near `cmd_install`:

```python
_STEP_SLUGS = {
    "bootstrap ~/.firekeep": "bootstrap-home",
    "configure ~/.firekeep/config": "configure-config",
    "create venv": "create-venv",
    "pip install firekeep-client": "pip-install-client",
    "lock down config permissions": "lock-config-perms",
    "select this version (current link)": "select-version",
    "render runtime adapters": "render-adapters",
    "add firekeep to PATH": "add-to-path",
    "join Firekeep server": "join-server",
}


def _stage_slug(step: str) -> tuple[str, dict]:
    """cmd_install `step` string -> (stage slug, union extras). The two
    interpolated steps become a fixed slug + an enum field — never an
    interpolated string (spec, Event schema). Unmapped -> ("", {}), which
    build_event drops; the exhaustiveness test keeps this table current."""
    if step in _STEP_SLUGS:
        return _STEP_SLUGS[step], {}
    if step.startswith("render ") and step.endswith(" adapter"):
        return "render-adapter", {"runtime": step[len("render "):-len(" adapter")]}
    if step.startswith("pip install ") and step.endswith(" (local checkout dir)"):
        return "pip-install-dex", {"dex": step[len("pip install "):-len(" (local checkout dir)")]}
    return "", {}
```

Wire BOTH terminating handlers in `cmd_install` (the `TimeoutExpired` sibling
at :572 would otherwise silently exempt every timeout — external review #1):

```python
    except subprocess.TimeoutExpired as exc:
        slug, extra = _stage_slug(step)
        report.emit("install", slug, exc=exc, exit_code=1, **extra)
        print(f"firekeep: install failed at '{step}': timed out after "
              f"{_INSTALL_TIMEOUT:.0f}s (override with FIREKEEP_INSTALL_TIMEOUT)",
              file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - installer surface, fail loud not raw
        slug, extra = _stage_slug(step)
        report.emit("install", slug, exc=exc, exit_code=1, **extra)
        print(f"firekeep: install failed at '{step}': {exc}", file=sys.stderr)
        return 1
```

- [ ] **Step 4: `_check_health` wiring** — replace the loop body (cli.py:870-878):

```python
    out = []
    failures: list[tuple[str, str]] = []
    for svc in resolver.SERVICES:
        try:
            ep = resolver.resolve(svc, cfg=cfg)
            get_json(f"{ep.rest_base}/health", headers=ep.headers, verify=ep.verify)
            out.append((svc, "ok", ep.rest_base))
        except (TransportError, resolver.ConfigError, OSError) as exc:
            out.append((svc, "fail", f"{_ep_url(svc, cfg)}: {exc}"))
            failures.append((svc, report.map_error(exc)))
    # All services down is ONE fact ("no Keep reachable"), reported as the
    # `server` stage; a partial failure is per-service signal (spec, stage
    # (connectivity)). _ep_url is never read by the report path.
    if failures and len(failures) == len(resolver.SERVICES):
        report.emit("connectivity", "server", error=failures[0][1], cfg=cfg)
    else:
        for svc, category in failures:
            report.emit("connectivity", svc, error=category, cfg=cfg)
    return out
```

- [ ] **Step 5: embeddings + backup rows** — Read `_check_embeddings` (cli.py ~:1000-1020) and `_check_backup` (~:1255-1275) first. At each handler that catches a network/transport exception `exc` and turns it into a warn/fail row, add before the return: `report.emit("connectivity", "embeddings", error=report.map_error(exc), cfg=cfg)` (respectively `"backup"`). If `_check_backup` delegates the network call to `backups.doctor_row`, wire in cli.py at the point the failed row comes back only if the exception object is available there; otherwise wire inside the except block in `backups.doctor_row` with a local import (`from firekeep_client import report`). Never pass the row's detail text anywhere.

- [ ] **Step 6: hooklog seam** — replace `log_failure` in `client/firekeep_client/hooklog.py`:

```python
def log_failure(hook: str, message: str, exc: Exception | None = None) -> None:
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        h = str(hook).replace("\n", " ").replace("\r", " ")[:200]
        m = str(message).replace("\n", " ").replace("\r", " ")[:500]
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} | {h} | {m}\n")
    except Exception:
        pass
    # Field-failure seam (spec, capture point 3): the hook cores already route
    # every caught failure through here — the dispatcher's own handler sees
    # only UNCAUGHT crashes. Class only; the free-text message stays local.
    if exc is not None:
        try:
            from firekeep_client import report
            report.emit("runtime", str(hook).replace("_", "-"), exc=exc)
        except Exception:
            pass
```

Then pass `exc=e` at the dispatcher's crash handler (`hooks/__main__.py:323-325` → `hooklog.log_failure(core_name, f"dispatcher crashed: {e!r}", exc=e)`) and at every `hooklog.log_failure(...)` call in `hooks/session_start.py` that has an exception in scope (grep `log_failure` there — six sites).

- [ ] **Step 7: gateway wiring** — `client/firekeep_client/gateway.py` (add `from firekeep_client import report` to its imports). Per-tool-call handler (:418-420):

```python
            except Exception as exc:
                backend.state = f"unavailable: {exc}"
                report.emit("runtime", "gateway-call", exc=exc, backend=backend.name)
                return self._error(request_id, -32000, f"{backend.name} unavailable: {exc}")
```

Serve loop (:462-463):

```python
            except Exception as exc:
                report.emit("runtime", "gateway-dispatch", exc=exc)
                response = Gateway._error(None, -32603, f"gateway error: {exc}")
```

- [ ] **Step 8: Run everything** — remove the two `xfail` marks from Task 1 Step 6, then `cd client && python -m pytest tests/test_report_capture.py tests/test_report_schema.py -v` — Expected: ALL PASS. If the hook-core exhaustiveness test names `_CORE_MODULES` keys the `RUNTIME_STAGES` literal lacks (or vice versa), fix the literal in `report.py` to exactly those keys hyphenated, keeping `gateway-call`/`gateway-dispatch`.

- [ ] **Step 9: Full client suite** — `cd client && python -m pytest tests/ -x -q` — Expected: no regressions (the `_check_health` shape test at cli.py:861-862 asserts `{svc for svc,_,_ in results} == set(resolver.SERVICES)`; the rewrite preserves the tuple shape).

- [ ] **Step 10: Commit** — `git add -A client && git commit -m "feat(report): wire capture points — install steps, doctor connectivity, hook seam, gateway"`

---

### Task 5: Consent surfaces — install wizard, `--report-failures`, the doctor ask, the design-record comment

**Files:**
- Modify: `client/firekeep_client/wizard.py` (`prompt_config`), `client/firekeep_client/cli.py` (`_apply_flags` :344-366, `cmd_doctor` :2502-2531, install argparse, the design-record comment :1705-1712)
- Test: `client/tests/test_report_consent_surfaces.py`

**Interfaces:**
- Consumes: `report.ask_consent(cfg)`, `report.record_consent(cfg, value)`, `report.has_answer(cfg)` (Task 2).
- Produces: `firekeep install --report-failures` flag; doctor ask on both spellings (`doctor` and its `status` alias — same code path, spec "Where the asks live").

- [ ] **Step 1: Failing tests**

```python
# client/tests/test_report_consent_surfaces.py
import builtins
import configparser

import pytest

from firekeep_client import cli, report


def test_apply_flags_report_failures_writes_true():
    cfg = configparser.ConfigParser()

    class Args:
        agent_id = None
        host = None
        dist_base = None
        report_failures = True
    assert cli._apply_flags(cfg, Args()) is True
    assert cfg.get("report", "failures") == "true"


def test_apply_flags_env_prefill_from_bootstrap(monkeypatch):
    monkeypatch.setenv("FIREKEEP_REPORT_CONSENT", "0")
    cfg = configparser.ConfigParser()

    class Args:
        agent_id = None
        host = None
        dist_base = None
        report_failures = False
    assert cli._apply_flags(cfg, Args()) is True
    assert cfg.get("report", "failures") == "false"


def test_cmd_doctor_asks_once_after_output_and_preserves_exit_code(
        tmp_path, monkeypatch, capsys):
    """Spec, 'Where the asks live': ask AFTER the rows; EOF leaves [report]
    absent and the exit code untouched; a recorded answer is never re-asked."""
    cfg_path = tmp_path / "config"
    cfg_path.write_text("[identity]\nagent_id = t\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_config_path", lambda: cfg_path)
    monkeypatch.setattr(cli, "run_doctor", lambda: [("cortex", "fail", "x")])
    monkeypatch.setattr(cli, "_generic_hint", lambda: None)
    monkeypatch.setattr("sys.stdin",
                        type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)

    def raise_eof(prompt=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)

    class Args:
        report = False
    assert cli.cmd_doctor(Args()) == 1                       # fail row -> rc 1, EOF didn't change it
    assert "[report]" not in cfg_path.read_text()            # EOF recorded nothing
    out = capsys.readouterr().out
    assert out.index("[FAIL] cortex") < out.index("Send anonymous failure reports")

    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")
    assert cli.cmd_doctor(Args()) == 1
    assert "failures = true" in cfg_path.read_text()

    monkeypatch.setattr(builtins, "input",
                        lambda prompt="": pytest.fail("re-asked after answer"))
    assert cli.cmd_doctor(Args()) == 1
```

- [ ] **Step 2: Run to verify failure** — `cd client && python -m pytest tests/test_report_consent_surfaces.py -v` — Expected: FAIL.

- [ ] **Step 3: `_apply_flags`** — append before `return touched` (cli.py:366):

```python
    pre = os.environ.get("FIREKEEP_REPORT_CONSENT", "").strip()
    if getattr(args, "report_failures", False):
        report.record_consent(cfg, True)
        touched = True
    elif pre in ("0", "1"):
        # The bootstrap asked before provisioning (spec decision 6); the
        # non-interactive hand-off must not lose the human's answer.
        report.record_consent(cfg, pre == "1")
        touched = True
    return touched
```

- [ ] **Step 4: wizard integration** — in `wizard.py`, at the END of `prompt_config` (after the existing questions, before its return), add:

```python
    # Field-failure consent (spec decision 1): asked once, only when unanswered.
    # ask_consent uses its own EOF-safe reader — NOT console_ask, whose
    # EOF-takes-the-default would silently enroll.
    from firekeep_client import report
    report.ask_consent(cfg)
```

(`_configure` already writes cfg to disk afterwards — cli.py:418-420 — so no extra persistence.) Also add the argparse flag where the install subparser is built (grep `add_parser("install"` in cli.py):

```python
    p_install.add_argument("--report-failures", action="store_true",
                           dest="report_failures",
                           help="enable anonymous failure reporting without prompting "
                                "(headless/CI opt-in; see firekeep.ai/privacy.html)")
```

- [ ] **Step 5: doctor ask** — in `cmd_doctor` (cli.py), after the `hint` print and BEFORE the `--report` block:

```python
    # One-time consent ask — the migration path for machines installed before
    # this channel existed (spec, 'Where the asks live'). After the rows, never
    # delaying them; EOF/^C record nothing and rc is already decided above.
    # Fires on both spellings (`doctor` and the `status` alias) — one code path.
    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            path = _config_path()
            cfg = resolver.load_config(path)
            if report.ask_consent(cfg):
                with open(path, "w", encoding="utf-8") as handle:
                    cfg.write(handle)
                state._private(path)
    except Exception:  # noqa: BLE001 — the ask must never affect doctor
        pass
```

- [ ] **Step 6: amend the design-record comment** — cli.py:1705-1712. Replace the sentence `There is deliberately NO persisted config toggle (no [telemetry] section) —` (and keep the rest) with:

```
# Design record: firekeep.ai/privacy.html discloses this exact mechanism.
# For DOCTOR --REPORT there is deliberately NO persisted config toggle —
# every send is one explicit act (typing --report on this one command), never
# a standing "always send" setting that could be flipped once and forgotten.
# The separate field-failure channel (report.py) has its own consented
# [report] section and its own disclosure; typing --report never writes it.
```

- [ ] **Step 7: Run tests** — `cd client && python -m pytest tests/test_report_consent_surfaces.py tests/test_report_consent.py -v` — Expected: PASS. Also run the wizard's own tests: `python -m pytest tests/ -k wizard -q`.
- [ ] **Step 8: Commit** — `git add -A client && git commit -m "feat(report): consent surfaces — wizard ask, --report-failures, one-time doctor ask"`

---

### Task 6: Flush points + contract matrix row

**Files:**
- Modify: `client/firekeep_client/cli.py` (`main` :3004-3026), `client/firekeep_client/gateway.py` (`run` :438-456), `client/firekeep_client/hooks/session_start.py` (:163-172), `client/firekeep_client/contract/matrix.py`
- Test: `client/tests/test_report_flush_points.py`

**Interfaces:**
- Consumes: `report.flush(cfg=None)` (never raises, cheap when spool empty/absent — one stat).
- Spec: "Flush points — three, so every runtime has one" + per-runtime honesty row.

- [ ] **Step 1: Failing tests**

```python
# client/tests/test_report_flush_points.py
import pytest

from firekeep_client import cli, report


def test_cli_main_flushes_before_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr(report, "flush", lambda *a, **k: called.append(True))
    monkeypatch.setattr(cli, "run_doctor", lambda: [])
    monkeypatch.setattr(cli, "_generic_hint", lambda: None)
    cli.main(["doctor"])
    assert called  # flush attempted on every CLI invocation


def test_session_start_flushes(monkeypatch, tmp_path):
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr(report, "flush", lambda *a, **k: called.append(True))
    from firekeep_client.hooks import session_start
    # run() is @never_raise({}); a full run needs no server — every step is
    # best-effort. Config may be absent in CI: monkeypatch load_config too.
    import configparser
    monkeypatch.setattr(session_start.resolver, "load_config",
                        lambda *a, **k: configparser.ConfigParser())
    monkeypatch.setattr(session_start.resolver, "agent_id", lambda cfg: "t")
    session_start.run({})
    assert called
```

- [ ] **Step 2: Run to verify failure** — Expected: FAIL (no flush calls).

- [ ] **Step 3: CLI start** — in `cli.main` (cli.py:3016-3023), between `args = parser.parse_args(argv)` and the dispatch:

```python
    # Field-failure spool flush (spec, flush point 1): the commands a
    # failed-install user retries are exactly these. is_enabled + empty-spool
    # make the common case one stat call; never raises, ~2s worst case.
    report.flush()
```

- [ ] **Step 4: gateway start** — in `gateway.run` (gateway.py:438-456), after `pin_import_paths()`:

```python
    # Flush point 2 (spec): the gateway mounts on EVERY runtime — including
    # codex/claude-desktop/generic, which have no hooks — making spool
    # delivery coverage uniform.
    report.flush()
```

- [ ] **Step 5: session_start** — in `session_start.run`, immediately before the `return {...}` (:169):

```python
    # 5. Field-failure spool flush (spec, flush point 3) — same daily pass as
    #    autoupdate/dex syncs; report.flush never raises.
    report.flush(cfg)
```

Add `report` to the `from firekeep_client import (...)` list at :21-24.

- [ ] **Step 6: matrix row** — Read `client/firekeep_client/contract/matrix.py` :100-140 to learn the row shape, then add a row alongside the briefing row (same cell style), cells:
  - claude: `"CLI + gateway + session_start hook"`
  - codex: `"CLI + gateway (no hooks)"`
  - kiro: `"CLI + gateway + agentSpawn hook (delivery unverified)"`
  - opencode: `"CLI + gateway + plugin (first event)"`
  - claude-desktop: `"gateway only (no CLI habit, no hooks)"`
  - generic: `"CLI + gateway (no hooks)"`
  Row label: `failure-report flush` with a one-line note that emit itself always attempts an immediate send. If matrix.py has a doc/code agreement test, run it and satisfy it.

- [ ] **Step 7: Run** — `cd client && python -m pytest tests/test_report_flush_points.py tests/ -k "matrix or flush" -q` then the full suite `python -m pytest tests/ -q` — Expected: PASS, no regressions.
- [ ] **Step 8: Commit** — `git add -A client && git commit -m "feat(report): flush at CLI start, gateway start, session_start; matrix coverage row"`

---

### Task 7: Client integration smoke — end-to-end against a stub collector

**Files:**
- Test: `client/tests/test_report_integration.py`

**Interfaces:** none new — proves Tasks 1–6 compose: a simulated install failure emits a batch a strict validator accepts; bypass and consent gates hold end-to-end.

- [ ] **Step 1: Write the test**

```python
# client/tests/test_report_integration.py
"""End-to-end: emit -> spool -> flush -> HTTP -> strict validation, with the
same vocabulary tables the PHP collector (Task 10) enforces."""
import errno
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from firekeep_client import report


@pytest.fixture
def collector():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.headers.get("Content-Type", "").startswith("application/json")
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            accepted, rejected = [], []
            for ev in body["events"]:
                ok = (ev.get("kind") in report.KINDS
                      and ev.get("error") in report.ERRORS
                      and ev.get("os") in report.OS_FAMILIES
                      and ev.get("arch") in report.ARCHES
                      and ev.get("py") in report.PY_BUCKETS
                      and isinstance(ev.get("id"), str) and len(ev["id"]) == 32)
                (accepted if ok else rejected).append(ev["id"])
                if ok:
                    received.append(ev)
            out = json.dumps({"accepted": accepted, "rejected": rejected,
                              "sealed": 0, "active_bytes": 0}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, received
    server.shutdown()


def test_install_failure_reaches_collector(collector, tmp_path, monkeypatch):
    port, received = collector
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("FIREKEEP_FAILURE_REPORT", "1")
    monkeypatch.setattr(report, "REPORT_URL", f"http://127.0.0.1:{port}/")
    report.emit("install", "create-venv",
                exc=PermissionError(errno.EACCES, "/secret/path"), exit_code=1)
    assert len(received) == 1
    ev = received[0]
    assert ev["stage"] == "create-venv" and ev["error"] == "permission-denied"
    assert "/secret/path" not in json.dumps(ev)
    assert not (tmp_path / "report-spool.jsonl").exists()  # acked -> gone


def test_bypass_sends_nothing(collector, tmp_path, monkeypatch):
    port, received = collector
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("FIREKEEP_FAILURE_REPORT", "1")
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    monkeypatch.setattr(report, "REPORT_URL", f"http://127.0.0.1:{port}/")
    report.emit("install", "create-venv", error="other")
    report.flush()
    assert received == [] and not (tmp_path / "report-spool.jsonl").exists()


def test_no_consent_sends_nothing(collector, tmp_path, monkeypatch):
    port, received = collector
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    monkeypatch.delenv("FIREKEEP_FAILURE_REPORT", raising=False)
    monkeypatch.setattr(report, "REPORT_URL", f"http://127.0.0.1:{port}/")
    report.emit("install", "create-venv", error="other")
    assert received == []
```

- [ ] **Step 2: Run** — `cd client && python -m pytest tests/test_report_integration.py -v` — Expected: PASS (these should pass immediately; a failure here is a real composition bug from Tasks 1–6 — fix it there, not in the test).
- [ ] **Step 3: Full suite** — `cd client && python -m pytest tests/ -q` — Expected: green.
- [ ] **Step 4: Commit** — `git add client/tests/test_report_integration.py && git commit -m "test(report): end-to-end emit->flush->collector integration"`

---

### Task 8: `install.sh` — consent before provisioning, stage tracking, die-POST

**Files:**
- Modify: `client/bootstrap/install.sh` (die() :27, fetch() :58-67, platform detect ~:230-240, version resolve :244-255, provisioning :467-504, hand-off :525-539)

**Interfaces:**
- Produces (Task 9/10 rely on these): env `FIREKEEP_REPORT_CONSENT` = `"0"`/`"1"` exported to the wizard hand-off only when the human ANSWERED (EOF → not exported); POST body identical to the client's single-event batch; `client` value `unknown-bootstrap` before version resolution, `${V}` after; `py` = `${PYTHON_VERSION}` (3.12); stage slugs exactly `report.BOOTSTRAP_STAGES`.
- Spec: decision 6; "stage (install, bootstrap)".

- [ ] **Step 1: Consent ask + reporting state** — insert AFTER the `BASE="${FIREKEEP_DIST_BASE%/}"` line (:41), BEFORE the TLS block (this is before every failure-prone step — spec decision 3's property):

```sh
# --- field-failure consent (asked BEFORE anything can fail; spec decision 6) --
# Tri-state: unanswered (EOF, headless) exports nothing and reports nothing.
# The answer rides FIREKEEP_REPORT_CONSENT into the wizard hand-off so the
# machine is asked exactly once.
REPORT_CONSENT=0
REPORT_STAGE="detect-platform"
REPORT_ERROR="other"
REPORT_OS=""
REPORT_ARCH=""
REPORT_CLIENT="unknown-bootstrap"
if [ -n "${FIREKEEP_NO_FAILURE_REPORT:-}" ]; then
    REPORT_CONSENT=0
elif [ -n "${FIREKEEP_FAILURE_REPORT:-}" ]; then
    REPORT_CONSENT=1
    export FIREKEEP_REPORT_CONSENT=1
elif ( : < /dev/tty ) 2>/dev/null; then
    printf '%s' "Send anonymous failure reports to firekeep.ai? Category codes only — what \
failed, error class, OS family, versions; never paths, messages, addresses, or \
any persistent identifier. [Y/n] " > /dev/tty
    if IFS= read -r report_answer < /dev/tty; then
        case "${report_answer}" in
            ""|y|Y|yes|YES|Yes) REPORT_CONSENT=1; export FIREKEEP_REPORT_CONSENT=1 ;;
            *)                  REPORT_CONSENT=0; export FIREKEEP_REPORT_CONSENT=0 ;;
        esac
    fi   # read failure = EOF: leave unanswered — export NOTHING, report nothing
fi

report_failure() {
    # Enum-only, fire-and-forget (spec decision 6): never affects the exit
    # path, never prints, 2s ceiling. Every value below is a fixed literal or
    # a shell variable this script itself set from a closed set — no command
    # output, path, or error text is ever interpolated.
    [ "${REPORT_CONSENT}" = "1" ] || return 0
    [ -n "${REPORT_OS}" ] || return 0
    rf_id="$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"
    [ ${#rf_id} -eq 32 ] || return 0
    rf_payload="{\"events\":[{\"id\":\"${rf_id}\",\"kind\":\"install\",\"stage\":\"${REPORT_STAGE}\",\"error\":\"${REPORT_ERROR}\",\"os\":\"${REPORT_OS}\",\"arch\":\"${REPORT_ARCH}\",\"client\":\"${REPORT_CLIENT}\",\"py\":\"${PYTHON_VERSION}\"}]}"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 2 -H 'Content-Type: application/json' \
            -d "${rf_payload}" "https://firekeep.ai/failure-report.php" >/dev/null 2>&1 || true
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 2 --header='Content-Type: application/json' \
            --post-data="${rf_payload}" -O /dev/null "https://firekeep.ai/failure-report.php" 2>/dev/null || true
    fi
    return 0
}
```

- [ ] **Step 2: die() reports** — replace `die() { echo "firekeep: $*" >&2; exit 1; }` (:27) with the definition placed AFTER the block above (POSIX sh needs `report_failure` defined before `die` uses it — move die's definition down, or forward-declare by defining both in the new block):

```sh
die() { echo "firekeep: $*" >&2; report_failure; exit 1; }
```

- [ ] **Step 3: OS/arch/stage assignments** — at the existing platform `case` (~:230-240): set `REPORT_OS`/`REPORT_ARCH` from the already-computed `os`/`arch`/`libc` values (map: Darwin→`darwin`; Linux+glibc→`linux-gnu`, else `linux-musl`; x86_64/amd64→`x86_64`, aarch64/arm64→`arm64`, else `other`), and set `REPORT_ERROR="unsupported-platform"` on the line immediately before the `die "unsupported platform ..."` arm. Then thread `REPORT_STAGE` through the script — one assignment immediately before each phase's first `die`-able command:
  - before the `latest.json` fetch (:252): `REPORT_STAGE="fetch-manifest"`; after `V=` resolves (:255): `REPORT_CLIENT="${V}"`
  - before the SHA256SUMS verification block: `REPORT_STAGE="verify-checksum"`
  - before the uv binary fetch/unpack: `REPORT_STAGE="provision-python"`
  - before `uv venv` (:469): `REPORT_STAGE="create-venv"`
  - before the wheel install (:488): `REPORT_STAGE="install-wheels"`
  - before the runnable check (:497): `REPORT_STAGE="runnable-check"`
  - before `point_current` (:504): `REPORT_STAGE="flip-current"`
  - before the wizard hand-off (:525): `REPORT_STAGE="handoff"`
  Reset `REPORT_ERROR="other"` alongside each stage assignment (a stale mapped error from a survived fetch must not mislabel a later die).

- [ ] **Step 4: fetch() error mapping** — in `fetch()` (:58-67), capture curl's exit code before dying:

```sh
fetch() {
    # $1 = url, $2 = dest. curl first (present on macOS), wget fallback.
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1" -o "$2" || {
            rf_rc=$?
            case "${rf_rc}" in
                6) REPORT_ERROR="dns-failure" ;;
                7) REPORT_ERROR="connection-refused" ;;
                28) REPORT_ERROR="timeout" ;;
                35|60) REPORT_ERROR="tls-verify-failed" ;;
            esac
            die "download failed: $1"
        }
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$2" "$1" || die "download failed: $1"
    else
        die "neither curl nor wget is available"
    fi
}
```

- [ ] **Step 5: syntax + smoke** — `sh -n client/bootstrap/install.sh` (Expected: silent) and, if dash is available, `dash -n client/bootstrap/install.sh`. Then run the existing e2e gate if docker is up: `cd client && python -m pytest tests/test_e2e_bootstrap.py -m e2e -q` (headless branch — must still pass untouched; consent block takes the unanswered path with no tty).
- [ ] **Step 6: Commit** — `git add client/bootstrap/install.sh && git commit -m "feat(bootstrap): consent before provisioning + enum-only die report (sh)"`

---

### Task 9: `install.ps1` + cross-language enum test + real-PTY acceptance (sh)

**Files:**
- Modify: `client/bootstrap/install.ps1` (Die function; platform/version/provision/hand-off sections — mirror install.sh's placements: consent near top, stages before each Die-able phase, `$V` assignment after version resolution ~:?, provisioning :493-525)
- Create: `client/tests/test_report_bootstrap_enums.py`, `client/tests/test_bootstrap_consent_pty.py`

**Interfaces:**
- Consumes: `report.BOOTSTRAP_STAGES`, `report.ERRORS`, `report.OS_FAMILIES`, `report.ARCHES` (canonical vocabulary the scripts' literals must stay within — spec, "Cross-language enums").

- [ ] **Step 1: install.ps1 consent + state** — near the top (after the dist-base/`$Base` resolution, before any Die-able work):

```powershell
# --- field-failure consent (spec decision 6): asked before anything can fail --
$ReportConsent = $false
$ReportStage = 'detect-platform'
$ReportError = 'other'
$ReportOs = 'windows'
$ReportArch = switch ($env:PROCESSOR_ARCHITECTURE) {
    'AMD64' { 'x86_64' }
    'ARM64' { 'arm64' }
    default { 'other' }
}
$ReportClient = 'unknown-bootstrap'
if ($env:FIREKEEP_NO_FAILURE_REPORT) {
    # opted out: never ask, never send
} elseif ($env:FIREKEEP_FAILURE_REPORT) {
    $ReportConsent = $true
    $env:FIREKEEP_REPORT_CONSENT = '1'
} elseif ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
    try {
        $answer = Read-Host ("Send anonymous failure reports to firekeep.ai? Category codes only - " +
            "what failed, error class, OS family, versions; never paths, messages, " +
            "addresses, or any persistent identifier [Y/n]")
        if ($answer -eq '' -or $answer -match '^(?i)y(es)?$') {
            $ReportConsent = $true; $env:FIREKEEP_REPORT_CONSENT = '1'
        } else {
            $ReportConsent = $false; $env:FIREKEEP_REPORT_CONSENT = '0'
        }
    } catch {
        # ^C / closed console: unanswered — export nothing, send nothing
    }
}

function Send-FailureReport {
    # Enum-only, fire-and-forget: 2s ceiling, never throws past its catch,
    # never interpolates command output or error text.
    if (-not $ReportConsent -or -not $ReportOs) { return }
    $id = [guid]::NewGuid().ToString('N')
    $payload = @{ events = @(@{
        id = $id; kind = 'install'; stage = $ReportStage; error = $ReportError
        os = $ReportOs; arch = $ReportArch; client = $ReportClient; py = "$PythonVersion"
    }) } | ConvertTo-Json -Depth 4 -Compress
    try {
        Invoke-RestMethod -Uri 'https://firekeep.ai/failure-report.php' -Method Post `
            -ContentType 'application/json' -Body $payload -TimeoutSec 2 | Out-Null
    } catch { }
}
```

- [ ] **Step 2: Die() reports** — find the `function Die` definition; make its first line `Send-FailureReport` (before the write + exit). Move the consent block above it if needed so the function is defined by then.

- [ ] **Step 3: stage threading (ps1)** — assignments mirroring Task 8 Step 3: `$ReportStage = 'fetch-manifest'` before the latest.json fetch and `$ReportClient = $V` after `$V` resolves; `'verify-checksum'` before the SHA256SUMS check; `'provision-python'` before the uv fetch; `'create-venv'` before `& $Uv venv` (:495); `'install-wheels'` before the wheel install (:514); `'runnable-check'` before the firekeep.exe test (:523); `'flip-current'` before the current flip; `'handoff'` before the wizard hand-off. Reset `$ReportError = 'other'` at each stage. If the ps1 fetch helper distinguishes web exceptions, map only what is structural: a caught `[System.Net.WebException]`/HttpRequestException with a timeout status → `'timeout'`; DNS (`NameResolutionFailure`) → `'dns-failure'`; TLS trust failure → `'tls-verify-failed'`; otherwise leave `'other'`.

- [ ] **Step 4: ps1 syntax check** — `pwsh -NoProfile -Command "[void][System.Management.Automation.Language.Parser]::ParseFile('client/bootstrap/install.ps1', [ref]$null, [ref]$errs); if ($errs) { $errs; exit 1 }"` — Expected: exit 0.

- [ ] **Step 5: cross-language enum test**

```python
# client/tests/test_report_bootstrap_enums.py
"""The three implementations (py, sh, ps1) share one vocabulary; a literal in
either bootstrap outside report.py's tables is a silent schema drift (spec,
'Cross-language enums')."""
import re
from pathlib import Path

from firekeep_client import report

BOOTSTRAP = Path(__file__).resolve().parents[1] / "bootstrap"


def _literals(text, patterns):
    out = set()
    for pat in patterns:
        out.update(re.findall(pat, text))
    return out


def test_install_sh_literals_are_canonical():
    text = (BOOTSTRAP / "install.sh").read_text(encoding="utf-8")
    stages = _literals(text, [r'REPORT_STAGE="([a-z-]+)"'])
    assert stages, "install.sh lost its stage assignments"
    assert stages <= set(report.BOOTSTRAP_STAGES), stages - set(report.BOOTSTRAP_STAGES)
    errors = _literals(text, [r'REPORT_ERROR="([a-z0-9-]+)"'])
    assert errors <= set(report.ERRORS), errors - set(report.ERRORS)
    oses = _literals(text, [r'REPORT_OS="([a-z-]+)"'])
    assert oses <= set(report.OS_FAMILIES) | {""}


def test_install_ps1_literals_are_canonical():
    text = (BOOTSTRAP / "install.ps1").read_text(encoding="utf-8")
    stages = _literals(text, [r"\$ReportStage = '([a-z-]+)'"])
    assert stages, "install.ps1 lost its stage assignments"
    assert stages <= set(report.BOOTSTRAP_STAGES), stages - set(report.BOOTSTRAP_STAGES)
    errors = _literals(text, [r"\$ReportError = '([a-z0-9-]+)'"])
    assert errors <= set(report.ERRORS), errors - set(report.ERRORS)


def test_every_bootstrap_stage_is_assigned_somewhere():
    sh = (BOOTSTRAP / "install.sh").read_text(encoding="utf-8")
    ps1 = (BOOTSTRAP / "install.ps1").read_text(encoding="utf-8")
    for stage in report.BOOTSTRAP_STAGES:
        assert f'REPORT_STAGE="{stage}"' in sh, f"install.sh misses {stage}"
        assert f"$ReportStage = '{stage}'" in ps1, f"install.ps1 misses {stage}"
```

Run: `cd client && python -m pytest tests/test_report_bootstrap_enums.py -v`. Adjust the ps1 detect-platform: the initial value `$ReportStage = 'detect-platform'` counts as its assignment (same for sh). Expected: PASS after Tasks 8–9 steps.

- [ ] **Step 6: real-PTY acceptance test (POSIX)**

```python
# client/tests/test_bootstrap_consent_pty.py
"""The consent prompt under a REAL controlling terminal (spec Testing,
'Real-terminal acceptance'). The existing e2e deliberately DETACHES the tty
(test_e2e_bootstrap.py:36), so the interactive branch has zero coverage
without this. POSIX-only: Windows has no stdlib ConPTY binding and the client
test suite is stdlib-only — the ps1 prompt is covered by the literal test
above plus the manual checklist in the plan's final verification.

Exercises ONLY the consent block: the script is cut down to everything up to
(and excluding) the TLS section, plus a probe line — provisioning under a PTY
is the e2e suite's job, not this test's.
"""
import os
import pty
import re
import select
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty only")

BOOTSTRAP = Path(__file__).resolve().parents[1] / "bootstrap" / "install.sh"


def _consent_snippet(tmp_path):
    """install.sh from the consent block start to report_failure's end, with a
    probe that prints the resulting state and exits before any real work."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    start = text.index("# --- field-failure consent")
    end = text.index("# --- TLS trust", start)
    snippet = ("#!/bin/sh\nset -u\nPYTHON_VERSION=3.12\n"
               + text[start:end]
               + '\nprintf "CONSENT=%s EXPORTED=%s\\n" '
                 '"${REPORT_CONSENT}" "${FIREKEEP_REPORT_CONSENT-unset}"\n')
    script = tmp_path / "consent-only.sh"
    script.write_text(snippet, encoding="utf-8")
    script.chmod(0o755)
    return script


def _run_under_pty(script, keys, env=None):
    parent, child = pty.openpty()
    proc = subprocess.Popen(["sh", str(script)], stdin=child, stdout=child,
                            stderr=child, env=dict(os.environ, **(env or {})),
                            close_fds=True, start_new_session=False)
    os.close(child)
    output = b""
    wrote = False
    try:
        while proc.poll() is None or select.select([parent], [], [], 0.1)[0]:
            r, _, _ = select.select([parent], [], [], 2.0)
            if not r:
                if proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(parent, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if not wrote and b"[Y/n]" in output:
                os.write(parent, keys)
                wrote = True
    finally:
        os.close(parent)
        proc.wait(10)
    return output.decode("utf-8", "replace")


def test_enter_records_yes(tmp_path):
    out = _run_under_pty(_consent_snippet(tmp_path), b"\n")
    assert "CONSENT=1 EXPORTED=1" in out


def test_n_records_no(tmp_path):
    out = _run_under_pty(_consent_snippet(tmp_path), b"n\n")
    assert "CONSENT=0 EXPORTED=0" in out


def test_eof_records_nothing(tmp_path):
    # ^D at the prompt: unanswered — nothing exported, nothing reported
    out = _run_under_pty(_consent_snippet(tmp_path), b"\x04")
    assert "CONSENT=0 EXPORTED=unset" in out


def test_env_opt_out_never_asks(tmp_path):
    out = _run_under_pty(_consent_snippet(tmp_path), b"",
                         env={"FIREKEEP_NO_FAILURE_REPORT": "1"})
    assert "[Y/n]" not in out
    assert "CONSENT=0 EXPORTED=unset" in out
```

Run: `cd client && python -m pytest tests/test_bootstrap_consent_pty.py -v` (on this Windows box it skips; CI's Linux job runs it — same split as the e2e suite). If the consent block's start/end anchors moved, fix the anchors, not the assertions.

- [ ] **Step 7: Commit** — `git add -A client && git commit -m "feat(bootstrap): ps1 consent + die report; cross-language enum guard; PTY consent acceptance"`

---

### Task 10: `failure-report.php` — the collector (firekeep-site repo)

**Files (all in `E:\Documents\Projects\firekeep-site`):**
- Create: `failure-report.php`, `scripts/failure-report-stats.sh`
- Create (deploy state seed): document in `README.md` that `domains/firekeep.ai/failure-stats/allowed-versions.txt` must exist on the host (one released version per line; append on every client release — same release step that updates `latest.json`)

**Interfaces:**
- Consumes the wire contract from Task 3 (batch/ack/ids) and the vocabularies from Task 1 — the tables below MUST be byte-identical to `report.py`'s (Task 7's integration test and the client contract are the guard on the client side; keep them in sync by hand here, with a comment naming `report.py` as the source of truth).
- Produces: log lines `{"ts","first","id","e":{...}}` in sealed segments `failures.<UTCstamp>-<gen>.log` (Task 13 consumes); maintenance-ping response (Task 13 step 1).
- Spec sections: Collector, Alerting, "The empty batch is the maintenance ping".

- [ ] **Step 1: Re-verify Hostinger facts on the host** (spec, Architecture): ssh in (the stats scripts show the endpoint) and confirm: `php -r 'var_dump(function_exists("mail"), function_exists("flock"));'` → both true; `php -i | grep disable_functions` → contains exec/shell_exec/popen/proc_open; `which crontab` → absent. If any differs, STOP and revise the spec's Alerting section before building.

- [ ] **Step 2: Write `failure-report.php`**

```php
<?php
/**
 * Field-failure collector. Spec: Firekeep repo,
 * docs/superpowers/specs/2026-08-22-field-failure-reporting-design.md.
 *
 * Discipline inherited from doctor-report.php (adversarial review 2026-08-20):
 * POST-only, application/json required (CORS-preflight defense), no IP/UA/
 * cookie/identifier ever written, /D on every regex, arrays never id-keyed
 * maps. Hardened beyond it because THIS endpoint has three things doctor's
 * does not: mutable shared state, an outbound mail side effect, and a
 * downstream consumer — hence ONE flock'd critical section, temp+rename state
 * writes, a bounded dedup ring, sealed immutable segments, and a mail budget.
 *
 * Vocabularies mirror client/firekeep_client/report.py (source of truth).
 * VALUES are validated, not shapes: an unrecognised value REJECTS the event.
 * The empty batch {"events":[]} is the maintenance ping: runs seal+digest
 * checks under the lock, appends nothing, returns health.
 */

header('Cache-Control: no-store');
header('Content-Type: application/json');

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') { http_response_code(405); exit; }
$ct = $_SERVER['CONTENT_TYPE'] ?? $_SERVER['HTTP_CONTENT_TYPE'] ?? '';
if (stripos($ct, 'application/json') !== 0) { http_response_code(415); exit; }

const MAX_BODY_BYTES = 40960;          // envelope over the client's 32KB spool cap
const MAX_EVENTS = 64;
const SEAL_AT_BYTES = 4 * 1024 * 1024;
const SEAL_AT_AGE = 21600;             // 6h
const SEALED_CAP_BYTES = 256 * 1024 * 1024;
const SIGNATURES_CAP = 4096;
const RING_CAP = 8192;
const MAIL_BUDGET = 5;                 // immediate novelty mails per rolling hour
const MAIL_TO = 'field-failures@firekeep.ai';  // fixed recipient, fixed subject — never report-derived

const KINDS = ['install', 'connectivity', 'runtime'];
const STAGES = [
    'install' => ['bootstrap-home', 'configure-config', 'create-venv',
        'pip-install-client', 'pip-install-dex', 'lock-config-perms',
        'select-version', 'render-adapters', 'render-adapter', 'add-to-path',
        'join-server',
        // bootstrap stages (same kind):
        'detect-platform', 'fetch-manifest', 'verify-checksum',
        'provision-python', 'create-venv', 'install-wheels', 'runnable-check',
        'flip-current', 'handoff'],
    'connectivity' => ['cortex', 'bridge', 'sentinel', 'relay', 'server',
        'embeddings', 'backup'],
    'runtime' => ['session-start', 'prompt', 'pre-tool', 'post-tool', 'stop',
        'session-end', 'precompact', 'gateway-call', 'gateway-dispatch'],
];
const ERRORS = ['permission-denied', 'disk-full', 'not-found', 'dns-failure',
    'connection-refused', 'network-unreachable', 'tls-verify-failed', 'timeout',
    'http-401', 'http-403', 'http-404', 'http-429', 'http-5xx',
    'unsupported-platform', 'other'];
const OS_FAMILIES = ['darwin', 'linux-gnu', 'linux-musl', 'windows'];
const ARCHES = ['x86_64', 'arm64', 'other'];
const PY_BUCKETS = ['3.9', '3.10', '3.11', '3.12', '3.13', '3.14', 'other'];
const RUNTIMES = ['claude', 'codex', 'kiro', 'opencode', 'claude-desktop', 'generic'];
const DEX_NAMES = ['symdex', 'docdex', 'maildex'];
const BACKENDS = ['cortex', 'bridge', 'sentinel', 'relay'];
// unknown-bootstrap: legal ONLY for install + the two pre-version stages.
const PRE_VERSION_STAGES = ['detect-platform', 'fetch-manifest'];

$raw = file_get_contents('php://input', false, null, 0, MAX_BODY_BYTES + 1);
if ($raw === false || strlen($raw) > MAX_BODY_BYTES) { http_response_code(400); exit; }
$data = json_decode($raw, true);
if (!is_array($data) || !isset($data['events']) || !is_array($data['events'])
    || count($data['events']) > MAX_EVENTS) { http_response_code(400); exit; }

$dir = dirname(__DIR__) . '/failure-stats';
if (!is_dir($dir)) { @mkdir($dir, 0700, true); }

function load_allowed_versions(string $dir): array {
    $path = $dir . '/allowed-versions.txt';
    if (!is_file($path)) { return []; }   // absent file = only unknown-bootstrap passes
    $out = [];
    foreach (file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
        $line = trim($line);
        if (preg_match('/^\d+\.\d+\.\d+$/D', $line)) { $out[$line] = true; }
    }
    return $out;
}

function validate_event($ev, array $allowed): bool {
    if (!is_array($ev)) { return false; }
    // exact allowed key set per the tagged union — unexpected keys reject
    $base = ['id', 'kind', 'stage', 'error', 'os', 'arch', 'client', 'py'];
    $extra = ['exit', 'runtime', 'dex', 'backend'];
    foreach (array_keys($ev) as $k) {
        if (!in_array($k, $base, true) && !in_array($k, $extra, true)) { return false; }
    }
    foreach ($base as $k) {
        if (!isset($ev[$k])) { return false; }
    }
    if (!is_string($ev['id']) || !preg_match('/^[0-9a-f]{32}$/D', $ev['id'])) { return false; }
    if (!in_array($ev['kind'], KINDS, true)) { return false; }
    if (!is_string($ev['stage'])
        || !in_array($ev['stage'], STAGES[$ev['kind']], true)) { return false; }
    if (!in_array($ev['error'], ERRORS, true)) { return false; }
    if (!in_array($ev['os'], OS_FAMILIES, true)) { return false; }
    if (!in_array($ev['arch'], ARCHES, true)) { return false; }
    if (!in_array($ev['py'], PY_BUCKETS, true)) { return false; }
    if (!is_string($ev['client'])) { return false; }
    if ($ev['client'] === 'unknown-bootstrap') {
        if ($ev['kind'] !== 'install'
            || !in_array($ev['stage'], PRE_VERSION_STAGES, true)) { return false; }
    } elseif (!preg_match('/^\d+\.\d+\.\d+$/D', $ev['client'])
        || !isset($allowed[$ev['client']])) { return false; }
    // tagged union
    if (isset($ev['exit'])
        && ($ev['kind'] !== 'install' || !is_int($ev['exit'])
            || $ev['exit'] < 0 || $ev['exit'] > 255)) { return false; }
    if (isset($ev['runtime'])
        && ($ev['stage'] !== 'render-adapter'
            || !in_array($ev['runtime'], RUNTIMES, true))) { return false; }
    if (isset($ev['dex'])
        && ($ev['stage'] !== 'pip-install-dex'
            || !in_array($ev['dex'], DEX_NAMES, true))) { return false; }
    if (isset($ev['backend'])
        && ($ev['kind'] !== 'runtime' || $ev['stage'] !== 'gateway-call'
            || !in_array($ev['backend'], BACKENDS, true))) { return false; }
    return true;
}

function read_state(string $dir): array {
    $default = ['sigs' => [], 'meta' => [
        'generation' => 0, 'last_digest_ts' => 0,
        'mail_window_start' => 0, 'mail_count' => 0,
        'suppressed_new' => 0, 'dropped_segments' => 0,
        'digest_events' => 0]];
    $raw = @file_get_contents($dir . '/signatures.json');
    if ($raw === false) { return $default; }
    $state = json_decode($raw, true);
    // Corrupt/partial state rebuilds EMPTY — safe because the mail budget
    // bounds the "everything looks new" consequence (spec, Collector).
    if (!is_array($state) || !isset($state['sigs']) || !is_array($state['sigs'])
        || !isset($state['meta']) || !is_array($state['meta'])) { return $default; }
    $state['meta'] += $default['meta'];
    return $state;
}

function write_state(string $dir, array $state): void {
    // temp + rename: file_put_contents truncates BEFORE it locks, and a
    // concurrent reader of a half-written file reclassifies everything as
    // new — the exact mail-storm decision 4 exists to prevent.
    $tmp = $dir . '/signatures.json.tmp';
    if (@file_put_contents($tmp, json_encode($state)) !== false) {
        @rename($tmp, $dir . '/signatures.json');
    }
}

function mail_line(string $text): string {
    // Mail composition is its own attack surface (spec, Alerting): body only,
    // CR/LF stripped from anything that ever touches it.
    return str_replace(["\r", "\n"], ' ', $text);
}

function send_mail(string $subject, string $body): bool {
    // Fixed recipient, FIXED subject string chosen by us — report-derived
    // values appear only inside the body, each line CR/LF-stripped.
    return @mail(MAIL_TO, $subject, $body, 'From: noreply@firekeep.ai');
}

$accepted = [];
$rejected = [];
$allowed = load_allowed_versions($dir);
$now = time();

$lock = @fopen($dir . '/.lock', 'c');
if ($lock === false || !flock($lock, LOCK_EX)) { http_response_code(503); exit; }
try {
    $state = read_state($dir);
    $meta =& $state['meta'];

    // dedup ring: last RING_CAP accepted ids, order of arrival
    $ringPath = $dir . '/recent-ids.log';
    $ring = [];
    if (is_file($ringPath)) {
        foreach (file($ringPath, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $rid) {
            $ring[$rid] = true;
        }
    }

    $logPath = $dir . '/failures.log';
    $newMailLines = [];

    foreach ($data['events'] as $ev) {
        if (!validate_event($ev, $allowed)) {
            if (is_array($ev) && isset($ev['id']) && is_string($ev['id'])
                && preg_match('/^[0-9a-f]{32}$/D', $ev['id'])) {
                $rejected[] = $ev['id'];
            }
            continue;   // rejected, not logged (spec: the single most important rule)
        }
        if (isset($ring[$ev['id']])) {
            $accepted[] = $ev['id'];   // replay: acked, not re-logged, not re-counted
            continue;
        }
        $sig = hash('sha256', implode('|', [$ev['kind'], $ev['stage'], $ev['error'],
                                            $ev['os'], $ev['client']]));
        $first = !isset($state['sigs'][$sig]);
        if ($first) {
            $state['sigs'][$sig] = ['n' => 0, 'first' => $now, 'mailed' => false,
                'k' => implode('|', [$ev['kind'], $ev['stage'], $ev['error'],
                                     $ev['os'], $ev['client']])];
            if (count($state['sigs']) > SIGNATURES_CAP) {
                uasort($state['sigs'], fn($a, $b) => $a['first'] <=> $b['first']);
                $state['sigs'] = array_slice($state['sigs'], count($state['sigs']) - SIGNATURES_CAP,
                                             null, true);
            }
        }
        $state['sigs'][$sig]['n']++;
        $meta['digest_events']++;

        // Signature recorded durably BEFORE the mail attempt ("seen"
        // semantics): a failed mail() leaves it seen-but-unmailed; the digest
        // sweeps unmailed novelties (at-least-once via digest, no retry loop).
        if ($first) {
            if ($now - $meta['mail_window_start'] > 3600) {
                $meta['mail_window_start'] = $now;
                $meta['mail_count'] = 0;
            }
            if ($meta['mail_count'] < MAIL_BUDGET) {
                $meta['mail_count']++;
                if (send_mail('firekeep field failure: new signature',
                              mail_line($state['sigs'][$sig]['k']) . "\n")) {
                    $state['sigs'][$sig]['mailed'] = true;
                }
            } else {
                $meta['suppressed_new']++;
            }
        }

        $line = json_encode(['ts' => gmdate('Y-m-d\TH:i:s\Z'), 'first' => $first,
                             'id' => $ev['id'], 'e' => $ev]) . "\n";
        @file_put_contents($logPath, $line, FILE_APPEND);   // we hold THE lock
        $ring[$ev['id']] = true;
        $accepted[] = $ev['id'];
    }

    // ring trim + persist (append-only file rewritten only when over 2x cap)
    if (count($ring) > 2 * RING_CAP) { $ring = array_slice($ring, -RING_CAP, null, true); }
    @file_put_contents($ringPath, implode("\n", array_keys($ring)) . "\n");

    // seal check (size OR age) — also reached by the empty-batch ping
    $sealed = 0;
    clearstatcache();
    if (is_file($logPath)) {
        $size = (int) @filesize($logPath);
        $age = $now - (int) @filemtime($logPath);
        $firstLineOld = false;
        if ($size > 0 && $age >= 0) {
            $fh = @fopen($logPath, 'r');
            if ($fh) {
                $firstLine = json_decode((string) fgets($fh), true);
                fclose($fh);
                $firstTs = is_array($firstLine) ? strtotime($firstLine['ts'] ?? '') : false;
                $firstLineOld = $firstTs !== false && ($now - $firstTs) > SEAL_AT_AGE;
            }
        }
        if ($size > SEAL_AT_BYTES || ($size > 0 && $firstLineOld)) {
            $meta['generation']++;
            $name = sprintf('%s/failures.%s-%d.log', $dir, gmdate('Ymd\THis\Z'),
                            $meta['generation']);
            @rename($logPath, $name);
            $sealed = 1;
        }
    }
    // sealed cap: oldest dropped, counted for the digest
    $segments = glob($dir . '/failures.*.log') ?: [];
    sort($segments);
    $total = 0;
    foreach ($segments as $seg) { $total += (int) @filesize($seg); }
    while ($total > SEALED_CAP_BYTES && count($segments) > 0) {
        $oldest = array_shift($segments);
        $total -= (int) @filesize($oldest);
        @unlink($oldest);
        $meta['dropped_segments']++;
    }

    // digest: >24h and anything happened; stamp ONLY after mail() true
    if ($now - $meta['last_digest_ts'] > 86400
        && ($meta['digest_events'] > 0 || $meta['suppressed_new'] > 0
            || $meta['dropped_segments'] > 0)) {
        $lines = ["events since last digest: " . $meta['digest_events'],
                  "new signatures suppressed by mail budget: " . $meta['suppressed_new'],
                  "sealed segments dropped (VPS not pulling): " . $meta['dropped_segments']];
        foreach ($state['sigs'] as $s) {
            if (!$s['mailed'] && $s['first'] > $meta['last_digest_ts']) {
                $lines[] = 'unmailed new: ' . mail_line($s['k']) . ' (n=' . $s['n'] . ')';
            }
        }
        if (send_mail('firekeep field failures: daily digest',
                      implode("\n", $lines) . "\n")) {
            $meta['last_digest_ts'] = $now;
            $meta['digest_events'] = 0;
            $meta['suppressed_new'] = 0;
            $meta['dropped_segments'] = 0;
        }
    }

    write_state($dir, $state);
    $activeBytes = is_file($logPath) ? (int) @filesize($logPath) : 0;
} finally {
    flock($lock, LOCK_UN);
    fclose($lock);
}

http_response_code(200);
echo json_encode(['accepted' => $accepted, 'rejected' => $rejected,
                  'sealed' => $sealed, 'active_bytes' => $activeBytes]);
```

- [ ] **Step 3: Lint** — `php -l failure-report.php` if PHP is installed locally; otherwise paste-check on the host after deploy (`php -l` over ssh). Fix any parse error before proceeding.

- [ ] **Step 4: Local behavioural test (if `php` available)** — `php -S 127.0.0.1:8899` in the site repo, then from the Firekeep repo run the Task 7 integration test pointed at it: `cd client && FIREKEEP_REPORT_URL_OVERRIDE=http://127.0.0.1:8899/failure-report.php python - <<'EOF'` style manual probes, or simpler, curl probes: (a) GET → 405; (b) POST without JSON content type → 415; (c) `{"events":[]}` → 200 with `accepted:[]` and `sealed`/`active_bytes` keys; (d) one valid event (client value must be in a local `failure-stats/allowed-versions.txt` you create beside the webroot parent — note `dirname(__DIR__)` means the stats dir lands OUTSIDE the served dir, mirror that locally); (e) same event again → accepted (dedup ring), log contains ONE line; (f) `client: "9.9.9"` not in allowlist → rejected; (g) `runtime` on `create-venv` → rejected. If no local PHP: these seven probes run against the LIVE endpoint right after deploy, before announcing anything (the established doctor-report.php practice — live-tested 2026-08-20).

- [ ] **Step 5: stats script** — `scripts/failure-report-stats.sh`, mirroring `scripts/doctor-report-stats.sh`'s ssh pattern (endpoint + path style from that file), reading `domains/firekeep.ai/failure-stats/failures.log` + sealed segments, printing counts by `kind|stage|error|os|client` (jq if available, awk fallback), `--raw` to dump lines.

- [ ] **Step 6: README + allowlist seed** — add to the site repo README: the `failure-stats/allowed-versions.txt` contract (one `x.y.z` per line; the client release process appends the new version — add this to the release checklist in the Firekeep repo's `docs/RELEASE-SIGNING.md` or wherever the release steps live, Task 14 covers the cross-repo doc). Seed the file on the host at deploy time with every version in `dist` (at minimum the current `1.5.2`).

- [ ] **Step 7: Commit (site repo)** — `git -C E:\Documents\Projects\firekeep-site add failure-report.php scripts/failure-report-stats.sh README.md && git -C E:\Documents\Projects\firekeep-site commit -m "feat: field-failure collector — enum-value validation, locked state, sealed segments, budgeted mail"`

---

### Task 11: `privacy.html` — every sentence the spec enumerates

**Files (site repo):**
- Modify: `privacy.html` (:66-90, :123-127)

**Interfaces:** none — text only, but the spec's Privacy section enumerates EXACTLY what changes; do all five, not one.

- [ ] **Step 1: The doctor bullet (line 88)** — two sentences change:
  - `Running firekeep doctor checks your installation and never leaves your machine.` → `Running firekeep doctor checks your installation locally; nothing is sent unless you have separately enabled failure reporting (below) — a failed connectivity check then sends its category codes — or you type the --report flag.`
  - `This never happens unless you type the flag.` → `The doctor report itself never happens unless you type the flag.`

- [ ] **Step 2: New bullet after it** — insert (matching the page's `<li><strong>` bullet markup):

```html
<li><strong>Anonymous failure reports, only if you said yes.</strong>
If you enabled failure reporting (a one-time install or doctor question, or
<code>--report-failures</code>; off if never answered), the Firekeep client
sends a report when an install step, a connection to your own Keep, or a
background task fails. A report contains only fixed category codes: what
failed (e.g. <code>create-venv</code>), the error class (e.g.
<code>permission-denied</code>), OS family, CPU architecture, client and
Python versions, and a random per-event delivery code that exists only so a
resent report is not double-counted. Never paths, messages, addresses,
tracebacks, or any persistent device, account or session identifier. The
collector records a server-side timestamp per report. Turn it off any time
with <code>[report] failures = false</code> in <code>~/.firekeep/config</code>
(the <code>FIREKEEP_NO_FAILURE_REPORT</code> environment variable also
disables it for a session). As with the collectors above, this is a statement
about what our application code stores — no IP address, cookie, or identifier
that ties separate reports together — not about the hosting layer, which
processes ordinary request data as described above. The category combination
is low-cardinality, not necessarily indistinguishable from every other
machine's; it is simply never linked to one by anything we store.</li>
```

- [ ] **Step 3: The collector enumeration (line 90)** — `Neither the download counter nor the install-health report writes an IP address...` → `None of the download counter, the install-health report, nor the failure-report collector writes an IP address...` (keep the rest of the sentence verbatim).

- [ ] **Step 4: Scope section (lines 73-78)** — widen: after the sentence scoping the notice to the public website, add: `It also covers the two telemetry channels the installed Firekeep client can send to this site — the opt-in doctor report and the consented failure reports — which are documented in full below.`

- [ ] **Step 5: Effective date (line 68 area)** — bump `Effective August 9, 2026` to the deploy date; the page itself promises a revised date on material changes (:123-127).

- [ ] **Step 6: Commit (site repo)** — `git -C E:\Documents\Projects\firekeep-site add privacy.html && git -C E:\Documents\Projects\firekeep-site commit -m "docs: privacy — failure-report channel disclosed; doctor bullet rescoped; scope + date updated"`

- [ ] **Step 7: Deploy + live verification** — the user deploys with their established flow (never ask how; do not attempt it yourself unless they say to). After deploy, run Task 10 Step 4's seven curl probes against `https://firekeep.ai/failure-report.php` and load `https://firekeep.ai/privacy.html` to confirm the new bullet renders. A real end-to-end: on this machine `FIREKEEP_FAILURE_REPORT=1 firekeep doctor` with the Keep stopped → one connectivity event visible via `scripts/failure-report-stats.sh --raw`.

---

### Task 12: Sentinel `POST /events` — wire the vestigial `EventIngest`

**Files:**
- Modify: `sentinel/app/models.py` (:6-12), `sentinel/app/mcp_server.py` (add route beside GET /events :341)
- Test: `sentinel/tests/test_post_events.py`

**Interfaces:**
- Consumes: `EventIngest` (currently dead code — nothing validates intake through it), `push_event` (store.py:71 — READ ITS SIGNATURE FIRST and mirror the call the MCP tool makes at mcp_server.py:262-270), `build_auth_middleware` (already wraps custom routes; `/events` is not in `skip_paths`, so X-API-Key applies whenever `AUTH_ENABLED=true`).
- Produces: `POST /events` accepting one `EventIngest` object or a list of them; `202 {"stored": n}`; `422` with detail on any validation failure — **never a catch-all that degrades to a default** (the known pydantic-Literal gotcha: a swallowed mismatch surfaces as dozens of tests failing on the safe value).
- Spec: VPS ingest step 6.

- [ ] **Step 1: Failing tests**

```python
# sentinel/tests/test_post_events.py
"""POST /events: the EventIngest model finally wired in, Literal-tight
severity, 4xx on mismatch (never a degrade-to-default)."""
import pytest
from httpx import ASGITransport, AsyncClient

# Follow the existing sentinel test bootstrap: copy the app/fixture setup from
# sentinel/tests/test_mcp_tools.py (fake redis fixture + app import) — reuse
# its fixtures if they are module-importable, duplicate minimally if not.


@pytest.mark.anyio
async def test_post_single_event_stores(app_client):
    resp = await app_client.post("/events", json={
        "source": "firekeep.ai/failure-report",
        "event_type": "install-failure",
        "summary": "install failure: create-venv permission-denied linux-gnu 1.5.2 (n=3)",
        "severity": "warning",
        "details": {"kind": "install", "stage": "create-venv",
                    "error": "permission-denied", "os": "linux-gnu",
                    "arch": "x86_64", "client": "1.5.2", "py": "3.11",
                    "first": True, "count": 3,
                    "batch": "failures.20260822T120000Z-1.log|abc123"},
    })
    assert resp.status_code == 202
    assert resp.json() == {"stored": 1}


@pytest.mark.anyio
async def test_post_batch_stores_all(app_client):
    events = [{"source": "firekeep.ai/failure-report",
               "event_type": "connectivity-failure",
               "summary": f"s{i}", "severity": "info", "details": {}}
              for i in range(3)]
    resp = await app_client.post("/events", json=events)
    assert resp.status_code == 202 and resp.json() == {"stored": 3}


@pytest.mark.anyio
async def test_invalid_severity_is_422_not_degraded(app_client):
    resp = await app_client.post("/events", json={
        "source": "x", "event_type": "y", "summary": "z", "severity": "warn"})
    assert resp.status_code == 422
    assert "severity" in resp.text


@pytest.mark.anyio
async def test_invalid_json_is_400(app_client):
    resp = await app_client.post("/events", content=b"not json",
                                 headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
```

- [ ] **Step 2: Run to verify failure** — `cd sentinel && python -m pytest tests/test_post_events.py -v` — Expected: FAIL (404 route missing / fixture to build). Build the `app_client` fixture from `test_mcp_tools.py`'s pattern first if the collection itself errors.

- [ ] **Step 3: Tighten `EventIngest`** — `sentinel/app/models.py`:

```python
from typing import Literal


class EventIngest(BaseModel):
    source: str = Field(..., max_length=500)
    event_type: str = Field(..., max_length=200)
    summary: str = Field(..., max_length=10000)
    details: dict = {}
    severity: Literal["info", "warning", "error", "critical"] = "info"
    tags: list[str] = []
```

(EventRecord subclasses it; stored events already use only these four values — run sentinel's full suite to confirm nothing constructed a different one.)

- [ ] **Step 4: The route** — in `sentinel/app/mcp_server.py`, beside the GET /events custom route (:341), same decorator style:

```python
@mcp.custom_route("/events", methods=["POST"])
async def post_events(request):
    """Authenticated ingest for the VPS failure puller (field-failure spec,
    'VPS ingest'). Validation errors return 4xx — NEVER swallowed into a
    default (the Literal-degrade gotcha this codebase has been bitten by)."""
    from pydantic import ValidationError
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body, not a server error
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    items = body if isinstance(body, list) else [body]
    if len(items) > 1000:
        return JSONResponse({"error": "too many events (max 1000)"}, status_code=400)
    parsed = []
    for item in items:
        try:
            parsed.append(EventIngest(**item) if isinstance(item, dict)
                          else None)
        except ValidationError as exc:
            return JSONResponse({"error": str(exc)[:2000]}, status_code=422)
        if parsed[-1] is None:
            return JSONResponse({"error": "each event must be an object"},
                                status_code=422)
    for ev in parsed:
        # Mirror sentinel_push_event's store call (mcp_server.py:262-270),
        # passing details through — adapt to push_event's actual signature.
        await push_event(source=ev.source, event_type=ev.event_type,
                         summary=ev.summary, severity=ev.severity,
                         details=ev.details, tags=ev.tags)
    return JSONResponse({"stored": len(parsed)}, status_code=202)
```

Import `EventIngest` at the top of mcp_server.py. If `push_event`'s real signature differs (read store.py:71-120 first), adapt the call — and if it does not accept `details` today, extend it the way `sentinel_push_event` would need anyway (the spec requires dimensions to land structurally in details, mcp extra observation 4).

- [ ] **Step 5: Run** — `cd sentinel && python -m pytest tests/ -q` — Expected: new tests PASS, zero regressions (watch `test_mcp_tools.py`'s severity tests — the Literal must not change the MCP tool's error-dict behaviour at :261-262, which validates BEFORE constructing the model).

- [ ] **Step 6: Commit** — `git add sentinel && git commit -m "feat(sentinel): authenticated POST /events wiring EventIngest with Literal severity"`

---

### Task 13: VPS ingest — puller with durable inbox + aggregation

**Files:**
- Create: `deploy/failure-ingest/ingest.py` (stdlib-only), `deploy/failure-ingest/pull-failures.sh`, `deploy/failure-ingest/README.md`
- Test: `tests/test_failure_ingest.py` (repo root tests/, beside test_image_pins.py)

**Interfaces:**
- Consumes: sealed segment lines `{"ts","first","id","e":{...}}` (Task 10); `POST /events` (Task 12); the same vocabularies (embed them — this box must validate INDEPENDENTLY of both other implementations: the Hostinger log is untrusted input, spec 'VPS ingest' step 4).
- Produces: Sentinel events with `source="firekeep.ai/failure-report"`, `event_type="<kind>-failure"`, severity `info`(known)/`warning`(first), `details={kind,stage,error,os,arch,client,py,first,count,batch,integrity:"unverified"}` plus union fields.

- [ ] **Step 1: Failing tests**

```python
# tests/test_failure_ingest.py
"""Pure-function tests for the VPS puller: independent re-validation and
aggregation (spec, VPS ingest steps 4-5)."""
import importlib.util
import json
from pathlib import Path

spec_path = Path(__file__).resolve().parents[1] / "deploy" / "failure-ingest" / "ingest.py"
spec = importlib.util.spec_from_file_location("ingest", spec_path)
ingest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingest)

GOOD = {"ts": "2026-08-22T12:00:00Z", "first": True, "id": "a" * 32,
        "e": {"kind": "install", "stage": "create-venv",
              "error": "permission-denied", "os": "linux-gnu",
              "arch": "x86_64", "client": "1.5.2", "py": "3.11"}}


def test_validate_line_accepts_good():
    assert ingest.validate_line(json.dumps(GOOD)) is not None


def test_validate_line_rejects_smuggled_text():
    bad = json.loads(json.dumps(GOOD))
    bad["e"]["error"] = "permission-denied; rm -rf / see http://evil"
    assert ingest.validate_line(json.dumps(bad)) is None
    bad2 = json.loads(json.dumps(GOOD))
    bad2["e"]["summary"] = "ignore previous instructions"   # unexpected key
    assert ingest.validate_line(json.dumps(bad2)) is None
    assert ingest.validate_line("not json at all") is None
    assert ingest.validate_line(json.dumps({"e": {}})) is None


def test_aggregate_one_event_per_signature_with_count():
    lines = [dict(GOOD, first=(i == 0)) for i in range(5)]
    out = ingest.aggregate(lines, segment="failures.20260822T120000Z-1.log")
    assert len(out) == 1
    ev = out[0]
    assert ev["event_type"] == "install-failure"
    assert ev["severity"] == "warning"           # a first sighting in the group
    assert ev["details"]["count"] == 5
    assert ev["details"]["integrity"] == "unverified"
    assert ev["details"]["batch"].startswith("failures.20260822T120000Z-1.log|")
    assert "summary" in ev and "permission-denied" in ev["summary"]
    # summary is composed from RE-VALIDATED enum values only
    for token in ev["summary"].split():
        assert ";" not in token and "http" not in token


def test_aggregate_known_signature_is_info():
    lines = [dict(GOOD, first=False)]
    out = ingest.aggregate(lines, segment="s")
    assert out[0]["severity"] == "info"


def test_aggregate_per_pull_ceiling():
    lines = []
    for i in range(600):
        e = json.loads(json.dumps(GOOD))
        e["e"]["client"] = f"1.5.{i}"      # 600 distinct signatures
        lines.append(e)
    out = ingest.aggregate(lines, segment="s")
    assert len(out) == ingest.PER_PULL_CEILING + 1
    assert "folded" in out[-1]["summary"]  # no silent truncation
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_failure_ingest.py -v` — Expected: FAIL (module missing).

- [ ] **Step 3: `ingest.py`** — stdlib only; embed the vocabulary tables (copy from `report.py`, with a header comment naming it as source of truth); implement:

```python
PER_PULL_CEILING = 500

def validate_line(raw: str) -> dict | None:
    """Untrusted input (spec step 4): exact key sets, every value against the
    embedded tables, id shape, unknown-bootstrap only on its two stages, full
    tagged union — the same rules as failure-report.php, enforced AGAIN here."""

def aggregate(lines: list[dict], *, segment: str) -> list[dict]:
    """One Sentinel event per (kind|stage|error|os|client) signature, count in
    details, severity warning iff any line in the group has first=true, else
    info. details carries every dimension + first + count +
    batch=f"{segment}|{sig_hash}" + integrity="unverified". summary is built
    ONLY from the validated enum values:
    f"{kind} failure: {stage} {error} {os} {client} (n={count})".
    Over PER_PULL_CEILING signatures: emit the first 500 plus ONE summary
    event ("... {n} further signatures folded") — never silent truncation."""

def post_events(events, sentinel_url, api_key, timeout=10):
    """urllib POST to {sentinel_url}/events with X-API-Key when set; raises on
    non-202 so the caller does NOT move the segment to done/."""

def process_inbox(inbox: Path, done: Path, sentinel_url: str, api_key: str) -> int:
    """For each failures.*.log in inbox (sorted): validate lines, aggregate,
    post, then move to done/ — move ONLY after a 202 (VPS→Sentinel hop is
    at-least-once; the deterministic details.batch key lets consumers collapse
    replays, spec step 3)."""

def main():
    """argparse: --inbox --done --sentinel --api-key-env NAME [--dry-run].
    Also: when the inbox has been empty and the last successful collector ping
    marker (a timestamp file the shell wrapper touches) is older than 7 days,
    post ONE warning event: source firekeep.ai/failure-report, event_type
    "collector-watchdog", summary "no successful collector ping for 7 days"."""
```

Write these four functions fully (the test file above pins validate_line/aggregate behaviour; post_events/process_inbox follow transport-free urllib patterns — `urllib.request.Request` with json body, checking `resp.status == 202`).

- [ ] **Step 4: `pull-failures.sh`** — the cron entrypoint on the VPS host:

```sh
#!/bin/sh
# Field-failure pull: ping -> fetch sealed segments to a durable inbox ->
# delete remote after verified local write -> ingest.py -> done/.
# Cron: */30 * * * * /opt/firekeep/failure-ingest/pull-failures.sh
set -eu
HOST="u784952002@82.180.175.177"      # the documented Hostinger ssh endpoint
PORT=65002
REMOTE="domains/firekeep.ai/failure-stats"
BASE="/var/lib/firekeep/failure-ingest"
INBOX="${BASE}/inbox"; DONE="${BASE}/done"; mkdir -p "${INBOX}" "${DONE}"

# 1. maintenance ping (spec: seals age-ripe segments under the PHP lock and
#    gives the watchdog an unambiguous signal). Touch the marker only on 200.
if curl -fsS --max-time 10 -H 'Content-Type: application/json' \
     -d '{"events":[]}' https://firekeep.ai/failure-report.php >/dev/null; then
    touch "${BASE}/last-ping-ok"
fi

# 2. fetch each sealed segment fully, verify byte count, delete remote only
#    after the local copy is durable (crash between = harmless refetch).
for seg in $(ssh -p "${PORT}" "${HOST}" "ls ${REMOTE}/failures.*.log 2>/dev/null" || true); do
    name="$(basename "${seg}")"
    scp -P "${PORT}" -q "${HOST}:${seg}" "${INBOX}/${name}.part"
    remote_size="$(ssh -p "${PORT}" "${HOST}" "wc -c < ${seg}")"
    local_size="$(wc -c < "${INBOX}/${name}.part")"
    [ "${remote_size}" = "${local_size}" ] || { rm -f "${INBOX}/${name}.part"; continue; }
    sync "${INBOX}/${name}.part" 2>/dev/null || sync
    mv "${INBOX}/${name}.part" "${INBOX}/${name}"
    ssh -p "${PORT}" "${HOST}" "rm ${REMOTE}/${seg##*/}" || true
done

# 3. re-validate, aggregate, POST to Sentinel; segments move to done/ on 202.
exec python3 /opt/firekeep/failure-ingest/ingest.py \
    --inbox "${INBOX}" --done "${DONE}" \
    --sentinel "http://100.91.3.51:8060" --api-key-env FIREKEEP_INTERNAL_KEY
```

- [ ] **Step 5: README.md** — install steps (copy dir to `/opt/firekeep/failure-ingest` on the VPS host, chmod +x, the cron line, where `FIREKEEP_INTERNAL_KEY` comes from — the deployment's `.env` — and the note that with `AUTH_ENABLED=false` the tailnet bind is the boundary and the key header is ignored); the watchdog semantics; retention pointers (inbox/done are the VPS's raw retention — add a `find "${DONE}" -mtime +14 -delete` line to the cron script's tail).

- [ ] **Step 6: Run tests** — `python -m pytest tests/test_failure_ingest.py -v` — Expected: PASS. `sh -n deploy/failure-ingest/pull-failures.sh` — silent.
- [ ] **Step 7: Commit** — `git add deploy/failure-ingest tests/test_failure_ingest.py && git commit -m "feat(ingest): VPS failure puller — durable inbox, independent re-validation, aggregated Sentinel POST"`

---

### Task 14: Docs — the Change Consistency Checklist instances

**Files:**
- Modify: `docs/guides/client-kit.md`, `CLAUDE.md` (root), `docs/THREAT-MODEL.md`, the release checklist doc (grep `docs/` for the client release steps — `RELEASE-SIGNING.md` or the release workflow docs), `dashboard/index.html`
- Test: existing doc/default-agreement tests (`cd client && python -m pytest tests/ -k doc -q` and any guide tests)

- [ ] **Step 1: `docs/guides/client-kit.md`** — new `## Field failure reporting (client kit — firekeep_client.report)` section: the tri-state consent model (unset=OFF, the three ask surfaces, `FIREKEEP_NO_FAILURE_REPORT` / `FIREKEEP_FAILURE_REPORT` / `--report-failures`), the three flush points and the per-runtime coverage (mirror the matrix row exactly — the doc-agreement tests assert doc/code parity), the spool location + caps, personal-mode silence, and the spec pointer.
- [ ] **Step 2: root `CLAUDE.md`** — one paragraph under the client-kit bullet list area: the channel exists, consent is tri-state, `report.py` is the module, the guide has the detail. Keep it to ~4 lines (CLAUDE.md is always-loaded context; the guide carries the weight).
- [ ] **Step 3: `docs/THREAT-MODEL.md`** — add the two new surfaces: the unauthenticated public collector (mitigations: enum-value validation, allowlist, mail budget, locked state, sealed caps; residual: low-integrity signal — labelled `integrity:"unverified"` downstream) and the outbound mail composition (fixed headers, body-only report values, CR/LF stripped). Cross-reference the spec's Review record.
- [ ] **Step 4: Release checklist** — wherever the client release steps live, add: "append the new version to `failure-stats/allowed-versions.txt` on the site host (deploy flow) — a release whose version is missing there has its failure reports rejected."
- [ ] **Step 5: `dashboard/index.html`** — in the Events page (:947 area), no new view (spec: follow-up), but add the source labelling: where event rows render, if `event.source === 'firekeep.ai/failure-report'` append a muted `unverified field telemetry` badge next to the source text (the details already carry `integrity: "unverified"`).
- [ ] **Step 6: Run** — `cd client && python -m pytest tests/ -q` (doc-agreement tests), then the repo-wide quick suites: `python -m pytest tests/ -q` at root.
- [ ] **Step 7: Commit** — `git add -A && git commit -m "docs(report): client-kit guide section, threat-model surfaces, release checklist, dashboard labelling"`

---

## Self-review record (plan author, against the spec)

- **Spec coverage:** Decisions 1-7 → Tasks 2/5 (consent), 1 (schema/closed fields), 5 (opt-out at prompt), 10 (alert budget), 10+13 (sealed segments/pull), 8-9 (bootstrap), 3+10 (nonce/at-least-once). Client section → 1-7. Collector → 10. Alerting → 10. VPS ingest → 13 (+12). Dashboard → 14.5. Privacy → 11. Retention → 10 (caps) + 13 (done/ cleanup + Sentinel's existing trim_by_age — no task needed). Testing section: every bullet has a home except "real-PTY on Windows/ConPTY" — **consciously narrowed** (stdlib-only test suite; no ConPTY binding): ps1 prompt covered by the literal test + Task 9 Step 4 parse check + the final manual verification below. Consistency checklist → 6 (matrix), 5 (cli comment), 14 (the rest).
- **Type consistency:** `report.emit(kind, stage, *, error, exc, exit_code, runtime, dex, backend, cfg)` used identically in Tasks 3-6; `_stage_slug -> (str, dict)` in 4 and Task 1's exhaustiveness test; wire keys `accepted/rejected/sealed/active_bytes` in 3, 7, 10; log-line keys `ts/first/id/e` in 10 and 13; `FIREKEEP_REPORT_CONSENT` produced in 8/9, consumed in 2 (`ask_consent`) and 5 (`_apply_flags`).
- **Known judgement calls an executor must NOT "fix" silently:** unlink-before-merge ordering in `flush` (Task 3 note); all-services-down emits ONE `server` event (Task 4); EOF semantics differ from `console_ask` on purpose (Task 2/5); severity never `error` (Tasks 12-13).

## Final verification (after all tasks)

1. Full suites: `cd client && pytest tests/ -q`; `cd sentinel && pytest tests/ -q`; root `pytest tests/ -q`.
2. Manual Windows prompt check (the consciously narrowed PTY gap): in a real PowerShell console run `.\client\bootstrap\install.ps1` far enough to see the consent prompt on a scratch machine/VM, answer `n`, confirm `%USERPROFILE%\.firekeep\config` gains `failures = false` after hand-off; Ctrl-C at the prompt leaves `[report]` absent.
3. Live end-to-end after site deploy (Task 11 Step 7's probes + one real `firekeep doctor` failure event visible in stats).
4. After VPS install: `mcp__firekeep__sentinel_get_events` shows the aggregated event; dashboard Events page shows the unverified badge.

## Execution handoff

Two options: **1. Subagent-Driven (recommended)** — fresh subagent per task with review between tasks (superpowers:subagent-driven-development). **2. Inline** — batch execution with checkpoints (superpowers:executing-plans). Tasks 10-11 touch the OTHER repo (`E:\Documents\Projects\firekeep-site`) — point the executing agent's cwd there explicitly. Deploys (site + VPS) are the user's call; stop and ask before any deploy step.
