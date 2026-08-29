"""One-shot legacy profile -> single-connection config migration.

Config loading is a hot concurrent path: a runtime can start four shims, a hook
dispatcher, hook cores, and the sidecar at once.  Migration therefore owns a
beside-config lock, content-addressed backup, and atomic replace; callers only
ever receive the complete old file or the complete new file.
"""
from __future__ import annotations

import configparser
import datetime
import hashlib
import io
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from firekeep_client import resolver, state

MIGRATION_LOCK_STALE_SECONDS = 30.0
MIGRATION_LOCK_POLL_SECONDS = 0.025
# How long a run of CONSECUTIVE PermissionErrors is treated as a Windows
# delete-pending window before it is believed and re-raised. Short: the real
# window is milliseconds, and a genuine EACCES should reach the user fast.
MIGRATION_LOCK_CONTENDED_SECONDS = 2.0
_PORTS_SKELETON_HOST = "127.0.0.1"
_PATHS_SKELETON_BASE = "https://firekeep.office.example"


@dataclass
class _Candidate:
    section: str
    sources: list[str] = field(default_factory=list)
    pin_source: bool = False
    fingerprint: tuple | None = None
    mcp_url: str = ""
    configured: bool = False
    agent_id: str = ""


def lock_path(config_path: Path) -> Path:
    return config_path.with_name(config_path.name + ".migration.lock")


def _parser(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(
        interpolation=None, inline_comment_prefixes=(";", "#")
    )
    try:
        loaded = cfg.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeError) as exc:
        raise resolver.ConfigError(
            f"firekeep config at {path} is not valid INI ({type(exc).__name__})"
        ) from exc
    if not loaded:
        raise resolver.ConfigError(f"firekeep config could not be read at {path}")
    cfg._firekeep_path = path
    return cfg


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # On Windows ``os.kill(pid, 0)`` is NOT the POSIX read-only existence
        # probe: CPython routes unsupported signals through TerminateProcess.
        # A liveness check against our own migration-lock owner can therefore
        # terminate the caller (observed in the concurrency test). Query the
        # process handle and exit code without signalling it instead.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # Access denied still proves a process owns that PID; other errors
            # (notably ERROR_INVALID_PARAMETER for a missing PID) mean dead.
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True  # fail toward preserving a possibly-live owner's lock
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_is_stale(path: Path) -> bool:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    if time.time() - stat.st_mtime <= MIGRATION_LOCK_STALE_SECONDS:
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data["pid"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        # O_CREAT|O_EXCL creates the empty file before the owner can write its
        # pid. An owner killed in that gap leaves exactly this shape.
        return True
    return not _pid_alive(pid)


@contextmanager
def _migration_lock(config_path: Path):
    """Hold the config-migration lock, tolerating Windows' delete-pending state.

    Windows has no atomic unlink. A file deleted while any handle is still open
    enters DELETE_PENDING: its directory entry survives, and CreateFile -- which
    `os.open` uses -- answers ERROR_ACCESS_DENIED. Python raises that as
    **PermissionError, not FileExistsError**, so every waiter racing the owner's
    release escaped this loop and crashed `resolver.load_config()`. POSIX has no
    such state; there a PermissionError is always a genuine EACCES.

    So a denial is retried on a BUDGET rather than swallowed. Retrying forever
    would turn a read-only `~/.firekeep` or a restrictive ACL into a silent hang
    inside `load_config()` -- strictly worse than the crash this fixes -- and the
    budget is also what still surfaces a real EACCES on POSIX.
    """
    path = lock_path(config_path)
    denied_deadline: float | None = None

    def absorb_denial(exc: PermissionError) -> None:
        """Ride out one denial, or believe it once the budget is spent.

        The deadline spans CONSECUTIVE denials only. It is disarmed by an honest
        FileExistsError on a live lock (see below) and by a successful acquire,
        so a transient denial early in a long legitimate wait cannot accumulate
        into a spurious failure minutes later.
        """
        nonlocal denied_deadline
        now = time.monotonic()
        if denied_deadline is None:
            denied_deadline = now + MIGRATION_LOCK_CONTENDED_SECONDS
        elif now >= denied_deadline:
            raise exc
        time.sleep(MIGRATION_LOCK_POLL_SECONDS)

    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except PermissionError as exc:
            absorb_denial(exc)
            continue
        except FileExistsError:
            if _lock_is_stale(path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except PermissionError as exc:
                    # The same delete-pending window, reached from the recovery
                    # side. Deliberately does NOT disarm the deadline: the denial
                    # is what is blocking progress here, so a lock that can never
                    # be removed has to end this loop rather than spin in it.
                    absorb_denial(exc)
                    continue
                else:
                    print(
                        f"firekeep config migration: recovered stale lock {path}",
                        file=sys.stderr,
                    )
                continue
            # An honest, live lock: real contention, not a denial. A live owner
            # may hold it far longer than the denial budget, so this wait stays
            # unbounded -- and observing it proves any earlier denial was
            # transient.
            denied_deadline = None
            time.sleep(MIGRATION_LOCK_POLL_SECONDS)
            continue
        denied_deadline = None
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump({
                    "pid": os.getpid(),
                    "created_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                }, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            yield
        finally:
            try:
                path.unlink()
            except (FileNotFoundError, PermissionError):
                # Best-effort release. A waiter's transient handle (its own
                # os.open, or _lock_is_stale's read) can block this unlink on
                # Windows. The lock then lingers and _lock_is_stale reclaims it
                # after MIGRATION_LOCK_STALE_SECONDS -- recoverable. Crashing the
                # owner AFTER its migration already succeeded is not.
                pass
        return


def _candidate_from_section(cfg: configparser.ConfigParser, path: Path,
                            candidate: _Candidate) -> _Candidate:
    section = candidate.section
    # agent_id is excluded from the fingerprint, but a legacy section without a
    # usable identity was never live and must not become the selected server.
    agent = resolver._require(cfg, "agent_id", section=section, path=path)
    kind = resolver._require(cfg, "kind", section=section, path=path).lower()
    scheme = resolver._require(cfg, "scheme", section=section, path=path).lower()
    verify = resolver._verify_for(cfg, scheme, section=section, path=path)
    api_key = cfg.get(section, "api_key", fallback="").strip()

    if kind == "ports":
        endpoint = resolver._require(cfg, "host", section=section, path=path)
        mcp_url = f"{scheme}://{endpoint}:{resolver.MCP_PORTS['cortex']}/mcp"
        configured = bool(api_key) or endpoint != _PORTS_SKELETON_HOST
    elif kind == "paths":
        endpoint = resolver._require(
            cfg, "base_url", section=section, path=path
        ).rstrip("/")
        if not endpoint.lower().startswith(f"{scheme}://"):
            raise resolver.ConfigError(
                f"firekeep config {path} [{section}]: scheme='{scheme}' does not "
                f"match base_url '{endpoint}'"
            )
        mcp_url = f"{endpoint}/mcp/cortex"
        configured = bool(api_key) or endpoint != _PATHS_SKELETON_BASE
    else:
        raise resolver.ConfigError(
            f"firekeep config {path} [{section}] has unknown kind '{kind}'"
        )

    candidate.fingerprint = (kind, scheme, endpoint, verify, api_key)
    candidate.mcp_url = mcp_url
    candidate.configured = configured
    candidate.agent_id = agent
    return candidate


def _collect_candidates(cfg: configparser.ConfigParser, path: Path):
    candidates: dict[str, _Candidate] = {}
    dangling: list[str] = []
    invalid: list[str] = []

    if not cfg.has_section("active"):
        invalid.append("[active] section is missing")
    else:
        active = cfg.get("active", "profile", fallback="").strip()
        if not active:
            invalid.append("[active] has no non-empty 'profile' value")
        elif not cfg.has_section(active):
            invalid.append(f"[active] names '{active}', which the file does not define")
        else:
            candidates[active] = _Candidate(active, ["[active]"])

    if cfg.has_section("pins"):
        for runtime, section in cfg.items("pins"):
            section = (section or "").strip()
            if not section or not cfg.has_section(section):
                dangling.append(f"[pins] {runtime} -> {section or '<empty>'}")
                continue
            candidate = candidates.setdefault(section, _Candidate(section))
            candidate.sources.append(f"[pins] {runtime}")
            candidate.pin_source = True

    live: list[_Candidate] = []
    for candidate in candidates.values():
        try:
            live.append(_candidate_from_section(cfg, path, candidate))
        except resolver.ConfigError as exc:
            invalid.append(f"[{candidate.section}] is not live: {exc}")
    return live, dangling, invalid


def _zero_message(path: Path, details: list[str]) -> str:
    lines = [
        f"firekeep config migration refused: {path} has no [server] section and",
        "no configured connection to migrate from.",
    ]
    lines.extend(f"  {detail}" for detail in details)
    lines.extend([
        "Nothing was changed. Run: firekeep join <code> (or: firekeep install --host <h>)",
    ])
    return "\n".join(lines)


def _conflict_message(path: Path, candidates: list[_Candidate]) -> str:
    lines = [
        f"firekeep config migration refused: {path} defines more than one",
        "server connection, and this version supports exactly one.",
        "",
    ]
    for candidate in candidates:
        sources = ", ".join(candidate.sources)
        lines.append(
            f"  [{candidate.section}]  {candidate.mcp_url}  (from {sources})"
        )
    alternate = path.with_name("office.conf")
    lines.extend([
        "",
        "Nothing was changed. Join the intended server with `firekeep join <code>`, "
        "or keep both by "
        "giving each its own file:",
        f"  cp {path} {alternate}   # then edit it to the [server] shape",
        f"  FIREKEEP_CONFIG={alternate} firekeep doctor",
    ])
    return "\n".join(lines)


def _section_bytes(raw: bytes, name: str) -> bytes | None:
    """Extract one INI section including its original newlines/comments/spacing."""
    lines = raw.splitlines(keepends=True)
    start = None
    offset = 0
    end = len(raw)
    for line in lines:
        stripped = line.strip()
        is_header = stripped.startswith(b"[") and stripped.endswith(b"]")
        if is_header:
            header = stripped[1:-1].decode("utf-8", errors="replace")
            if start is None and header == name:
                start = offset
            elif start is not None:
                end = offset
                break
        offset += len(line)
    return raw[start:end] if start is not None else None


def _render_migrated(cfg: configparser.ConfigParser, chosen: _Candidate,
                     raw: bytes) -> bytes:
    out = configparser.ConfigParser(interpolation=None)
    out["identity"] = {"agent_id": chosen.agent_id}
    out["server"] = {
        key: value for key, value in cfg.items(chosen.section)
        if key != "agent_id"
    }

    buf = io.StringIO(newline="\n")
    out.write(buf)
    body = buf.getvalue().encode("utf-8")
    dist = _section_bytes(raw, "dist")
    if dist is not None:
        if body and not body.endswith(b"\n\n"):
            body += b"\n"
        body += dist
    return body


def _write_atomic(path: Path, body: bytes) -> None:
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.migrate-", dir=path.parent, delete=False
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        state._private(tmp_path)
        os.replace(tmp_path, path)
        state._private(path)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except (FileNotFoundError, PermissionError):
                # FileNotFoundError is the normal path: os.replace consumed the
                # temp file. PermissionError is Windows -- an indexer or scanner
                # holding the just-written file blocks its deletion. Either way
                # the temp file is garbage; raising HERE would crash a migration
                # that already succeeded, and inside a `finally` it would also
                # mask whatever exception sent us here.
                pass


def _migrate_locked(path: Path) -> configparser.ConfigParser:
    cfg = _parser(path)
    if cfg.has_section("server"):
        return cfg

    live, dangling, invalid = _collect_candidates(cfg, path)
    if any(detail.startswith("[active]") for detail in invalid):
        raise resolver.ConfigMigrationConflict(_zero_message(path, invalid + dangling))

    configured = [candidate for candidate in live if candidate.configured]
    selected = list(live)
    if configured:
        # Explicit approved consequence: an unconfigured [active] may not hide
        # the only configured connection carried by a pin. An unconfigured pin,
        # however, remains a real candidate and can conflict rather than silently
        # repointing that runtime.
        selected = [candidate for candidate in selected if not (
            not candidate.configured
            and candidate.sources == ["[active]"]
        )]

    if not selected:
        raise resolver.ConfigMigrationConflict(
            _zero_message(path, invalid + dangling or ["no live legacy section resolved"])
        )

    by_fingerprint: dict[tuple, list[_Candidate]] = {}
    for candidate in selected:
        by_fingerprint.setdefault(candidate.fingerprint, []).append(candidate)
    if len(by_fingerprint) != 1:
        representatives = [group[0] for group in by_fingerprint.values()]
        raise resolver.ConfigMigrationConflict(_conflict_message(path, representatives))

    group = next(iter(by_fingerprint.values()))
    chosen = next((candidate for candidate in group if candidate.configured), group[0])
    raw = path.read_bytes()
    body = _render_migrated(cfg, chosen, raw)
    digest = hashlib.sha256(raw).hexdigest()[:16]
    backup = path.with_name(f"{path.name}.bak-profiles-{digest}")
    if not backup.exists():
        with backup.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        state._private(backup)
    _write_atomic(path, body)

    print(f"firekeep: migrated {path} to one [server] connection", file=sys.stderr)
    if cfg.has_section("pins"):
        for runtime, section in cfg.items("pins"):
            print(
                f"firekeep config migration: discarded [pins] {runtime} -> "
                f"{(section or '').strip() or '<empty>'}",
                file=sys.stderr,
            )
    return _parser(path)


def migrate_config(path: Path) -> configparser.ConfigParser:
    """Return a single-connection parser, migrating legacy bytes exactly once."""
    path = Path(path)
    with _migration_lock(path):
        return _migrate_locked(path)
