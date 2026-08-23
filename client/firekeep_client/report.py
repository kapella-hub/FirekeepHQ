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

if os.name == "nt":
    import msvcrt
else:
    msvcrt = None  # POSIX: O_APPEND's seek+write is one atomic syscall; no lock needed

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
            # Prompt printed explicitly (not passed to input()) so it is visible
            # even to a caller/test that replaces input() outright and would
            # otherwise never echo it — the real terminal experience is
            # unchanged since input() still writes nothing further before it reads.
            print(CONSENT_PROMPT, end="", flush=True)
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        record_consent(cfg, answer in ("", "y", "yes"))
        return True
    except Exception:  # noqa: BLE001
        return False


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
    protocol). On POSIX, O_APPEND's seek+write is a single atomic kernel
    syscall, so concurrent appenders can never corrupt each other. On
    Windows the CRT implements O_APPEND as a separate seek-then-write that is
    NOT atomic across processes — measured to silently drop ~13% of lines
    under two concurrent appenders — so there the write is additionally
    serialized with a short, non-blocking msvcrt byte-range lock on byte 0 of
    the file. If the lock can't be acquired within the ~500ms ceiling, the
    event is DROPPED rather than blocked: 'never blocks meaningfully' wins
    over guaranteed delivery."""
    if event is None:
        return
    try:
        path = _spool_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 2 * SPOOL_MAX_BYTES:
            return  # flush is broken entirely; never grow unbounded
        line = json.dumps(event, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            if msvcrt is None:
                os.write(fd, data)
            else:
                os.lseek(fd, 0, os.SEEK_SET)  # locking() locks from the current position
                locked = False
                for _ in range(20):  # ~500ms ceiling (20 * 25ms)
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        time.sleep(0.025)
                if not locked:
                    return  # contended past the ceiling; drop, don't block
                try:
                    os.lseek(fd, 0, os.SEEK_END)
                    os.write(fd, data)
                finally:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
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
    except (OSError, UnicodeDecodeError, ValueError):
        # A decode failure here must not abort flush() before the caller's
        # claim_path.unlink() — an exception propagating out of _read_events
        # would leave the claim file orphaned, unadoptable until it goes
        # stale, and unmerged in the meantime. Return what we have (possibly
        # nothing) and let flush() unlink and move on.
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
