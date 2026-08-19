"""Background maildex sync — the mail twin of `docdexsync`.

OFF unless a human asked for it twice: `firekeep dex add maildex` registers the dex,
`firekeep maildex add <host> <username>` registers a mailbox. Either one missing and
this module does nothing at all. Once both are true it is ON by default, and opting
out is the same pair of switches symdex, docdex and autoupdate use — the
`FIREKEEP_NO_AUTO_SYNC` env var or `[maildex] auto_sync = false` in ~/.firekeep/config.

`FIREKEEP_NO_AUTO_SYNC` is deliberately the SAME variable docdexsync reads, not a
per-dex one: it is the switch a person reaches for when they want the machine to stop
uploading their things in the background, and having to discover a second variable
after setting the first is how a "disabled" background sync keeps running. One switch
suspends both.

Why this exists at all: maildex is an ingest client with no MCP server and no resident
daemon, so without a trigger the only sync that ever happens is one a human types.
Named honestly (docdex spec §2, review #7): this is **sync on the next supported
session start**, not a schedule. It fires only on hook-bearing runtimes; an MCP-only
host gets no automatic sync and `firekeep maildex sync` is the documented manual path.

The three constraints are `docdexsync`'s, for the same reasons:

  * DETACHED spawn. A backfill — 90 days of two folders, fetched, parsed and uploaded
    — takes far longer than the 15s SessionStart timeout. Inline, it would trade a
    stale index for a hung session start.
  * ATOMIC O_EXCL claim. Three windows opening together would otherwise each spawn a
    `--all` sync over the same mailboxes, i.e. three IMAP logins racing each other.
    (`firekeep_maildex.sync` also holds a per-account lock, which is the backstop;
    this is the cheap front door.)
  * Never raises. A sync is an optimisation. Failing to run one must cost a session
    nothing — not a delay, not an error line, not a non-zero exit.

Boundary: this module must NOT import `firekeep_maildex`. The hook cores are
stdlib-only, and more to the point maildex is the module that holds a mailbox
password in memory — dragging it into every session-start hook would widen that
blast radius for no gain. The subprocess IS the seam, which is also why
`accounts.json` and the per-account state files are read off disk here rather than
through `firekeep_maildex.accounts` / `.state`. The cost of that seam is duplicated
knowledge of two file layouts, so the two readers must agree about what "active" and
"last synced" mean — the places they could drift are commented.

Private-session mode is deliberately NOT checked here: the hook dispatcher
short-circuits `session_start` while bypassed (`hooks/__main__.py`), so this code never
runs at all in a private session. Suspending a run already IN FLIGHT belongs where the
batches are — `firekeep_maildex.sync` re-checks the flag before every one.
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

DEX = "maildex"
ACTIVE = "active"
DEFAULT_SYNC_INTERVAL_HOURS = 6.0

# The stamp is generated here and is already digit-only, but it is also the
# filename of a claim — keep the sanitiser the policy has to pass through, so a
# future stamp carrying a separator can't let the claim escape the scratch dir.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


# ---------------------------------------------------------------------------
# Where the wheel keeps its files
# ---------------------------------------------------------------------------
def maildex_dir() -> Path:
    """`~/.firekeep/maildex`, derived exactly as `firekeep_maildex`'s home is.

    From `resolver._config_path()` rather than `Path.home()`: `FIREKEEP_CONFIG`
    relocates the whole kit, and a trigger reading the real home while maildex wrote to
    a relocated one would see 'never synced' forever and spawn on every session.

    Deliberately does NOT create the directory — asking whether a human has connected a
    mailbox must not leave evidence that they have."""
    return resolver._config_path().parent / DEX


def accounts_file() -> Path:
    return maildex_dir() / "accounts.json"


def state_file(account_id: str) -> Path:
    return maildex_dir() / "state" / f"{account_id}.json"


def active_account_ids() -> list[str]:
    """Ids of the accounts a `--all` sync would actually touch. `[]` for anything
    unreadable — a corrupt registry means 'no sync this session', never a crash.

    A missing `status` counts as active, matching how the wheel reads the same file
    (and `firekeep_docdex.sources._to_source` before it). That default is the one place
    these two readers could disagree about which mailboxes exist, so it is stated in
    both."""
    try:
        raw = accounts_file().read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [
        aid for aid, entry in data.items()
        if isinstance(entry, dict) and (entry.get("status") or ACTIVE) == ACTIVE
    ]


def read_last_sync(account_id: str) -> float | None:
    """When this account last COMPLETED a sync, as epoch seconds, or None.

    None covers every way an account can have no honest stamp: never synced, an
    unreadable or corrupt state file, and — the case worth naming — a run that
    aborted, because the wheel deliberately leaves `last_sync_at` unset rather than
    claiming a sync it did not finish. All of them mean the same thing here: due."""
    try:
        raw = state_file(account_id).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    at = data.get("last_sync_at") if isinstance(data, dict) else None
    if not isinstance(at, str) or not at:
        return None
    try:
        # `Z` is spelled out because `fromisoformat` only learned it in 3.11 and the
        # client floor is 3.10; the wheel writes `+00:00`, but a hand-edited file is
        # exactly where the other spelling shows up.
        parsed = datetime.datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Reading a naive stamp as LOCAL time — which is what `.timestamp()` does —
        # would shift staleness by the machine's UTC offset, in the direction that
        # SUPPRESSES a due sync for hosts behind UTC. Everything maildex writes is
        # UTC, so say so.
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def read_failure_count(account_id: str) -> int:
    """How many of this account's messages last failed to ingest. 0 when unknown.

    Not used by the trigger — the doctor row calls it, and it lives here because this
    module is where the client's knowledge of maildex's on-disk layout is kept
    deliberately in one place. The wheel records a per-message `error` beside
    `uid`/`ingested_at` (spec §3, state.py); a message carrying one is a message
    recall does not have.

    Tolerant on purpose: a state file whose shape this reader does not recognise must
    degrade to "nothing to report", never to a traceback in `firekeep doctor`."""
    try:
        data = json.loads(state_file(account_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, dict):
        return 0
    return sum(1 for entry in messages.values()
               if isinstance(entry, dict) and entry.get("error"))


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
def is_enabled(cfg) -> bool:
    """Everything that must be true before this machine opens a mailbox on its own.

    In order, cheapest and most explicit first: `FIREKEEP_NO_AUTO_SYNC` (env, wins over
    the config, mirroring `docdexsync.is_enabled` and `symdexindex.is_enabled`),
    `[maildex] auto_sync = false`, the dex being REGISTERED, and at least one active
    account. The last two are what make this different from symdex's trigger: symdex
    indexes whatever repo you opened, maildex only ever touches a mailbox a human
    connected by hand."""
    if os.environ.get("FIREKEEP_NO_AUTO_SYNC", "").strip().lower() not in _FALSEY:
        return False
    val = (cfg.get(DEX, "auto_sync", fallback="true")
           if cfg.has_section(DEX) else "true").strip().lower()
    if val in _DISABLE:
        return False
    if DEX not in dexes.read_registry():
        return False
    return bool(active_account_ids())


def sync_interval_hours() -> float:
    """`FIREKEEP_MAILDEX_SYNC_INTERVAL_HOURS`, default 6 (spec M6).

    Unparseable or non-positive falls back to the documented default: a typo in an env
    var must not silently turn a disclosed cadence into 'never' (a huge value) or
    'every session' (zero)."""
    try:
        value = float(os.environ.get("FIREKEEP_MAILDEX_SYNC_INTERVAL_HOURS", "").strip())
    except (AttributeError, ValueError):
        return DEFAULT_SYNC_INTERVAL_HOURS
    return value if value > 0 else DEFAULT_SYNC_INTERVAL_HOURS


def oldest_sync(account_ids) -> float | None:
    """When every one of these accounts was last known synced; None if any never was.

    The OLDEST, not the newest, and that is a decision rather than an accident: the
    spawn is `--all`, so the question this answers is "is ANYTHING stale?". Keyed on
    the newest instead, a mailbox connected a minute ago would wait a full interval
    because a sibling synced on time — and the first sync of an account a human just
    added is exactly the one worth being prompt about."""
    oldest: float | None = None
    for aid in account_ids:
        at = read_last_sync(aid)
        if at is None:
            return None
        oldest = at if oldest is None else min(oldest, at)
    return oldest


def should_sync(account_ids, *, now: float | None = None) -> str | None:
    """Decide whether to sync now, and under what dedupe key.

    Return None to skip. Return a STAMP string to sync — the stamp is also the
    once-only claim key, so whatever granularity it has IS the cadence.

    The stamp is the interval BUCKET, `floor(now / interval)`, which makes those two
    facts one fact:

      * every session start inside one bucket shares a claim, so three windows opening
        together spawn one sync between them;
      * a sync that never lands retries once per interval rather than once per session.
        This is the case the bucket exists for: an unreachable Keep (or IMAP host)
        aborts without stamping `last_sync_at`, so staleness alone would say "due" on
        every single session start, forever.
    """
    now = time.time() if now is None else now
    interval = sync_interval_hours() * 3600.0
    at = oldest_sync(account_ids)
    if at is not None and (now - at) < interval:
        return None
    return str(int(now // interval))


def _claim_path(stamp: str) -> Path:
    tag = _UNSAFE.sub("_", stamp)[:40].strip("_") or "none"
    return state._scratch_file(f"maildex_sync.{tag}")


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
            # cwd is deliberately left alone: a sync reads its accounts out of
            # accounts.json and must not hold a handle on the session's workspace.
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True  # survives the hook exit
        argv = [str(exe), "-m", "firekeep_maildex.sync", "--all", "--quiet"]
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
    """One line describing what was done about the mail, or '' when there is nothing
    to say. Called from the session_start core; never raises.

    Deliberately silent in the common cases (no dex, no accounts, nothing stale) — a
    line on every start is a nag, and this one would be a nag about somebody's mail.
    Takes no payload, unlike `symdexindex.index_nudge`: maildex syncs the mailboxes a
    human connected, which have nothing to do with the session's cwd."""
    try:
        if not is_enabled(cfg):
            return ""
        account_ids = active_account_ids()
        stamp = should_sync(account_ids)
        if not stamp:
            return ""
        count = len(account_ids)
        noun = "account" if count == 1 else "accounts"
        if not maybe_spawn(cfg, stamp):
            return (f"\n\n[firekeep] maildex sync is due for {count} mail {noun} — "
                    f"run: firekeep maildex sync")
        return (f"\n\n[firekeep] syncing {count} mail {noun} in the background "
                f"(maildex; disable with `FIREKEEP_NO_AUTO_SYNC=1`)")
    except Exception:  # noqa: BLE001 — the nudge must never cost a session
        return ""
