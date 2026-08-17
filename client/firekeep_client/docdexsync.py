"""Background docdex sync — the documents twin of `symdexindex`.

OFF unless a human asked for it twice: `firekeep dex add docdex` registers the dex,
`firekeep docdex add <folder>` registers a folder. Either one missing and this module
does nothing at all. Once both are true it is ON by default, and opting out is the
same pair of switches symdex and autoupdate use — the `FIREKEEP_NO_AUTO_SYNC` env var
or `[docdex] auto_sync = false` in ~/.firekeep/config.

Why this exists at all: docdex is an ingest client with no MCP server and no resident
daemon, so without a trigger the only sync that ever happens is one a human types.
Named honestly (spec §2, review #7): this is **sync on the next supported session
start**, not a schedule. It fires only on hook-bearing runtimes; an MCP-only host gets
no automatic sync and `firekeep docdex sync` is the documented manual path.

The three constraints are `symdexindex`'s, for the same reasons, and they are what
make a background upload safe to hang off a session-start hook:

  * DETACHED spawn (spec I6). A cold scan of a notes folder — extract every PDF, hash
    every file, upload each — takes far longer than the 15s SessionStart timeout.
    Inline, it would trade a stale index for a hung session start, which is strictly
    worse: the briefing is the thing the user is actually waiting on.
  * ATOMIC O_EXCL claim. Three windows opening together would otherwise each spawn a
    `--all` sync over the same folders. (`firekeep_docdex.sync` also holds a per-source
    lock, which is the backstop; this is the cheap front door that means the losing
    processes are never started rather than started and immediately blocked.)
  * Never raises. A sync is an optimisation. Failing to run one must cost a session
    nothing — not a delay, not an error line, not a non-zero exit.

Boundary: this module must NOT import `firekeep_docdex`. The hook cores are stdlib-only
and docdex carries `pypdf` and `python-docx`; a direct import would drag a PDF parser
into every PreToolUse gate on every Edit. The subprocess IS the seam that keeps the
boundary true, which is also why `sources.json` and the per-source state files are read
off disk here rather than through `firekeep_docdex.sources` / `.state`. The cost of
that seam is duplicated knowledge of two file layouts, so the two readers must agree
about what "active" and "last synced" mean — the places they could drift are commented.

Private-session mode is deliberately NOT checked here: the hook dispatcher
short-circuits `session_start` while bypassed (`hooks/__main__.py`), so this code never
runs at all in a private session. Suspending a run already IN FLIGHT is a different
problem and belongs where the batches are — `firekeep_docdex.sync` re-checks the flag
before every one (I3).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from firekeep_client import dexes, resolver, state

_FALSEY = ("", "0", "false", "no", "off")
_DISABLE = ("0", "false", "no", "off")  # explicit disable values (NOT blank)

DEX = "docdex"
ACTIVE = "active"
DEFAULT_SYNC_INTERVAL_HOURS = 6.0

# The stamp is generated here and is already digit-only, but it is also the
# filename of a claim — keep the sanitiser the policy has to pass through, so a
# future stamp carrying a separator can't let the claim escape the scratch dir.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


# ---------------------------------------------------------------------------
# Where the wheel keeps its files
# ---------------------------------------------------------------------------
def docdex_dir() -> Path:
    """`~/.firekeep/docdex`, derived exactly as `firekeep_docdex.firekeep_home` is.

    From `resolver._config_path()` rather than `Path.home()`: `FIREKEEP_CONFIG`
    relocates the whole kit, and a trigger reading the real home while docdex wrote to
    a relocated one would see 'never synced' forever and spawn on every session.

    Deliberately does NOT create the directory — asking whether a human has registered
    a folder must not leave evidence that they have."""
    return resolver._config_path().parent / DEX


def sources_file() -> Path:
    return docdex_dir() / "sources.json"


def state_file(source_id: str) -> Path:
    return docdex_dir() / "state" / f"{source_id}.json"


def active_source_ids() -> list[str]:
    """Ids of the sources a `--all` sync would actually touch. `[]` for anything
    unreadable — a corrupt registry means 'no sync this session', never a crash.

    A missing `status` counts as active, matching `firekeep_docdex.sources._to_source`.
    That default is the one place these two readers could disagree about which folders
    exist, so it is stated in both."""
    try:
        raw = sources_file().read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [
        sid for sid, entry in data.items()
        if isinstance(entry, dict) and (entry.get("status") or ACTIVE) == ACTIVE
    ]


def read_last_sync(source_id: str) -> float | None:
    """When this source last COMPLETED a sync, as epoch seconds, or None.

    None covers every way a source can have no honest stamp: never synced, an
    unreadable or corrupt state file, and — the case worth naming — a run that
    aborted, because `sync.py` deliberately leaves `last_sync_at` unset rather than
    claiming a sync it did not finish. All four mean the same thing here: due."""
    try:
        raw = state_file(source_id).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    at = data.get("last_sync_at") if isinstance(data, dict) else None
    if not isinstance(at, str) or not at:
        return None
    try:
        # `Z` is spelled out because `fromisoformat` only learned it in 3.11 and the
        # client floor is 3.10; docdex writes `+00:00`, but a hand-edited file is
        # exactly where the other spelling shows up.
        parsed = datetime.datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Reading a naive stamp as LOCAL time — which is what `.timestamp()` does —
        # would shift staleness by the machine's UTC offset, in the direction that
        # SUPPRESSES a due sync for hosts behind UTC. Everything docdex writes is
        # UTC, so say so.
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
def is_enabled(cfg) -> bool:
    """Everything that must be true before a document leaves this machine.

    In order, cheapest and most explicit first: `FIREKEEP_NO_AUTO_SYNC` (env, wins over
    the config, mirroring `symdexindex.is_enabled`), `[docdex] auto_sync = false`, the
    dex being REGISTERED, and at least one active folder. The last two are the ones
    that make this different from symdex's trigger: symdex indexes whatever repo you
    opened, docdex only ever touches folders a human named."""
    if os.environ.get("FIREKEEP_NO_AUTO_SYNC", "").strip().lower() not in _FALSEY:
        return False
    val = (cfg.get(DEX, "auto_sync", fallback="true")
           if cfg.has_section(DEX) else "true").strip().lower()
    if val in _DISABLE:
        return False
    if DEX not in dexes.read_registry():
        return False
    return bool(active_source_ids())


def sync_interval_hours() -> float:
    """`FIREKEEP_DOCDEX_SYNC_INTERVAL_HOURS`, default 6.

    Unparseable or non-positive falls back to the documented default, mirroring
    `firekeep_docdex.env_int`: a typo in an env var must not silently turn a disclosed
    cadence into 'never' (a huge value) or 'every session' (zero)."""
    try:
        value = float(os.environ.get("FIREKEEP_DOCDEX_SYNC_INTERVAL_HOURS", "").strip())
    except (AttributeError, ValueError):
        return DEFAULT_SYNC_INTERVAL_HOURS
    return value if value > 0 else DEFAULT_SYNC_INTERVAL_HOURS


def oldest_sync(source_ids) -> float | None:
    """When every one of these sources was last known synced; None if any never was.

    The OLDEST, not the newest, and that is a decision rather than an accident: the
    spawn is `--all`, so the question this answers is "is ANYTHING stale?". Keyed on
    the newest instead, a folder registered a minute ago would wait a full interval
    because a sibling synced on time — the first sync of a folder a human just added
    is exactly the one worth being prompt about."""
    oldest: float | None = None
    for sid in source_ids:
        at = read_last_sync(sid)
        if at is None:
            return None
        oldest = at if oldest is None else min(oldest, at)
    return oldest


def should_sync(source_ids, *, now: float | None = None) -> str | None:
    """Decide whether to sync now, and under what dedupe key.

    Return None to skip. Return a STAMP string to sync — the stamp is also the
    once-only claim key, so whatever granularity it has IS the cadence.

    The stamp is the interval BUCKET, `floor(now / interval)`, which makes those two
    facts one fact:

      * every session start inside one bucket shares a claim, so three windows opening
        together spawn one sync between them;
      * a sync that never lands retries once per interval rather than once per session.
        This is the case the bucket exists for: an unreachable Keep aborts without
        stamping `last_sync_at`, so staleness alone would say "due" on every single
        session start, forever.
    """
    now = time.time() if now is None else now
    interval = sync_interval_hours() * 3600.0
    at = oldest_sync(source_ids)
    if at is not None and (now - at) < interval:
        return None
    return str(int(now // interval))


def _claim_path(stamp: str) -> Path:
    tag = _UNSAFE.sub("_", stamp)[:40].strip("_") or "none"
    return state._scratch_file(f"docdex_sync.{tag}")


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------
def maybe_spawn(cfg, stamp: str) -> bool:
    """Ensure a background `--all` sync is (or has been) launched for `stamp`.

    Returns True when a sync is in flight — either this call spawned it OR another
    session already claimed this stamp. Returns False only when it can't run: disabled,
    interpreter missing, or the spawn itself failed. Never raises."""
    try:
        if not is_enabled(cfg):
            return False
        exe = Path(sys.executable)
        if not exe.exists():
            return False
        claim = _claim_path(stamp)
        try:
            # Atomic test-and-set: only the FIRST caller creates the file; a concurrent
            # second caller gets FileExistsError and defers.
            fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            return True  # already claimed for this interval — in flight
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            # cwd is deliberately left alone: a sync reads absolute paths out of
            # sources.json and must not hold a handle on the session's workspace.
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True  # survives the hook exit
        argv = [str(exe), "-m", "firekeep_docdex.sync", "--all", "--quiet"]
        try:
            subprocess.Popen(argv, **kwargs)  # noqa: S603 — fixed argv, not shell-interpolated
        except Exception:  # noqa: BLE001
            # Release the claim so a later session can retry a failed launch.
            try:
                claim.unlink()
            except OSError:
                pass
            return False
        return True
    except Exception:  # noqa: BLE001 — a sync must never cost a session
        return False


def sync_nudge(cfg) -> str:
    """One line describing what was done about the documents, or '' when there is
    nothing to say. Called from the session_start core; never raises.

    Deliberately silent in the common cases (no dex, no folders, nothing stale) — a
    line on every start is a nag, and this one would be a nag about somebody's private
    notes. Takes no payload, unlike `symdexindex.index_nudge`: docdex syncs the folders
    a human registered, which have nothing to do with the session's cwd."""
    try:
        if not is_enabled(cfg):
            return ""
        source_ids = active_source_ids()
        stamp = should_sync(source_ids)
        if not stamp:
            return ""
        count = len(source_ids)
        noun = "source" if count == 1 else "sources"
        if not maybe_spawn(cfg, stamp):
            return (f"\n\n[firekeep] docdex sync is due for {count} document {noun} — "
                    f"run: firekeep docdex sync")
        return (f"\n\n[firekeep] syncing {count} document {noun} in the background "
                f"(docdex; disable with `FIREKEEP_NO_AUTO_SYNC=1`)")
    except Exception:  # noqa: BLE001 — the nudge must never cost a session
        return ""
