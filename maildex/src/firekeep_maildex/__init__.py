"""firekeep-maildex — the email dex.

An ingest client, not an MCP server: it has no agent-callable tools, so
connecting a mailbox is structurally human-only. Everything it writes lives
under `~/.firekeep/maildex/`; everything it sends goes through the client kit's
resolver and transport so URL building, auth headers and the TLS guard have
exactly one home.

Two invariants are enforced by the SHAPE of this package rather than by care:

* **M2 — nothing here can mutate a mailbox.** `imapio` is the only module that
  touches IMAP, every open is `select(readonly=True)` (EXAMINE) and every fetch
  is `BODY.PEEK[...]`. The server refuses the writes; our discipline is not
  what stands between a bug and a deleted message. SMTP appears nowhere.
* **M3 — the app password is never on this machine's disk.** `accounts.json`
  holds host, user and folders; the secret lives in the Keep's vault and is
  pulled into a local variable for the life of one connection.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__version__ = "0.1.0"

__all__ = [
    "__version__", "env_int", "firekeep_home", "maildex_dir", "read_json", "write_atomic",
]


def env_int(name: str, default: int) -> int:
    """A disclosed cap read from the environment.

    Anything unparseable or non-positive falls back to the documented default:
    a typo in an env var must not silently disable a cap the docs promise.
    """
    raw = os.environ.get(name, "")
    try:
        value = int(raw.strip())
    except (AttributeError, ValueError):
        return default
    return value if value > 0 else default


def firekeep_home() -> Path:
    """The kit home (`~/.firekeep`), derived from the resolver's own config path.

    Deliberately NOT `Path.home() / ".firekeep"`: `FIREKEEP_CONFIG` relocates
    the kit for tests and for side-by-side installs, and maildex state must move
    with it. `_config_path` is the resolver's single source of truth for that
    decision; re-deriving it here would be a drift waiting to happen.
    """
    from firekeep_client import resolver

    return resolver._config_path().parent


def maildex_dir() -> Path:
    """`~/.firekeep/maildex`, created 0700 best-effort."""
    d = firekeep_home() / "maildex"
    d.mkdir(parents=True, exist_ok=True)
    _private(d)
    return d


def _private(p: Path) -> None:
    """Best-effort private perms. Never raises — failing to tighten a mode must
    not fail the write it protects (fail open on hardening, not on data).
    On Windows the parent `~/.firekeep` ACL already restricts these files."""
    try:
        if os.name != "nt":
            os.chmod(p, 0o700 if p.is_dir() else 0o600)
    except OSError:
        pass


def write_atomic(target: Path, payload: Any) -> None:
    """JSON to `target` via a same-directory temp file + `os.replace`.

    A reader sees the old complete file or the new complete file, never a
    partial write — a half-written state file is indistinguishable from
    corruption, and corruption reads as "nothing has ever been indexed", which
    costs a full re-fetch of a mailbox.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    _private(target.parent)
    tmp = target.parent / f"{target.name}.tmp-{os.getpid()}"
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        _private(tmp)
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def read_json(target: Path, *, what: str, default: Any) -> Any:
    """Parse `target`, or return `default` for missing/corrupt/unreadable.

    Corruption is LOGGED and the file is left exactly as it is: a read must
    never overwrite what it could not understand, because the bad file is the
    only evidence of whatever produced it.
    """
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except (OSError, UnicodeError) as e:
        _log(f"{what} unreadable: {e}")
        return default
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        _log(f"{what} is corrupt (left in place): {e}")
        return default
    if not isinstance(parsed, type(default)):
        _log(f"{what} has unexpected shape {type(parsed).__name__}; ignoring")
        return default
    return parsed


def _log(message: str) -> None:
    from firekeep_client import hooklog

    hooklog.log_failure("maildex", message)
