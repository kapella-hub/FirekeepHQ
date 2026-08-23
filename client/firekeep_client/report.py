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
import os
import socket
import ssl
import subprocess
import sys
import uuid

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
        try:
            import platform as _platform
            libc, _ = _platform.libc_ver()
            return "linux-gnu" if libc == "glibc" else "linux-musl"
        except Exception:  # noqa: BLE001 — libc_ver can raise in frozen/container envs
            return ""
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
    try:
        if not cfg.has_section("report"):
            cfg.add_section("report")
        cfg.set("report", "failures", "true" if value else "false")
    except Exception:  # noqa: BLE001
        pass


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
