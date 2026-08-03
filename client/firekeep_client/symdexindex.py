"""Background symdex auto-index.

ON by default; opt out with the `FIREKEEP_NO_AUTO_INDEX` env var or
`[symdex] auto_index = false` in ~/.firekeep/config.

Why this exists: symdex shipped a SessionStart hook that only ever PRINTED
"ACTION REQUIRED: call index_folder". A bash hook has no MCP client, so it could not
index even in principle — it could only ask the agent to. Every session that ignored
the ask (in practice: all of them) left the repo unindexed and every symdex tool
answering "Repository not found", while the hook kept reporting the problem as though
reporting were a fix. This is the same lesson the shim's `X-Session-Id` tap and the
rendered decision-board instruction block already record: a prompt aimed at the model
is a hope; a hook that performs the action is a guarantee. `python -m
firekeep_symdex.reindex` is the callable surface that was missing.

Shape is lifted wholesale from `firekeep_client.autoupdate`, for the same reasons:

  * DETACHED spawn. A cold index of a few hundred files takes 10-30s; the SessionStart
    hook timeout is 15s. Running it inline would trade a missing index for a hung
    session start, which is strictly worse — the briefing is the thing the user is
    actually waiting on.
  * ATOMIC O_EXCL claim. Two windows opening on the same repo together would otherwise
    both spawn an index writing the SAME `<root>/local-<name>.json`, and the loser's
    partial write is what the next session loads.
  * Never raises. An index is an optimisation. Failing to build one must cost a session
    nothing — not a delay, not an error line, not a non-zero exit.

Boundary: this module must NOT import `firekeep_symdex`. The hook cores are stdlib-only
(`transport` + `hooks._mcp`) and symdex is a separate wheel carrying tree-sitter; a
direct import would drag that into every PreToolUse gate on every Edit. The subprocess
IS the seam that keeps the boundary true, which is also why the index-freshness check
here reads the index file off disk rather than asking `IndexStore`.

Unlike autoupdate there is no "applies next session" caveat: the index is plain data
read at tool-call time, so a mid-session index becomes visible to symdex tools as soon
as it lands. The nudge says so.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from firekeep_client import state

_FALSEY = ("", "0", "false", "no", "off")
_DISABLE = ("0", "false", "no", "off")  # explicit disable values (NOT blank)

# Claim-file names are derived from a folder path, which on Windows carries `:` and `\`
# and on POSIX can carry almost anything. Collapse to a filename-safe token.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def is_enabled(cfg) -> bool:
    """Default ON. `FIREKEEP_NO_AUTO_INDEX` (env) wins over the config; `[symdex]
    auto_index = false` disables it persistently. A blank value (`auto_index =`) means
    'unset' -> the default (ON), NOT disabled — only the explicit disable words turn it
    off. Mirrors `autoupdate.is_enabled` so the two opt-outs behave identically."""
    if os.environ.get("FIREKEEP_NO_AUTO_INDEX", "").strip().lower() not in _FALSEY:
        return False
    val = (cfg.get("symdex", "auto_index", fallback="true")
           if cfg.has_section("symdex") else "true").strip().lower()
    return val not in _DISABLE


def index_root() -> Path:
    """Where the stdio symdex keeps its indexes.

    `CODE_INDEX_PATH` is symdex's OWN override (`server.py` reads it and threads it in
    as `storage_path`), so this must honour the same variable — otherwise a user who
    relocates their index has a client that reports "not indexed" forever while symdex
    happily reads a populated index somewhere else."""
    raw = os.environ.get("CODE_INDEX_PATH", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".code-index"


def index_file(folder: Path) -> Path:
    """The index JSON symdex would write for this folder.

    Mirrors `IndexStore._index_path` for `owner="local"`, whose slug is
    f"{owner}-{name}" with `name` the folder BASENAME. Two checkouts of the same repo
    in different parents therefore share one index slot — a real limitation of symdex's
    keying, not of this module, and the reason `is_indexable` insists on a git tree
    rather than indexing whatever directory a session happened to open in."""
    return index_root() / f"local-{folder.name}.json"


def is_indexable(folder: Path) -> bool:
    """A named git working tree. Cheap, no subprocess.

    The `.git` requirement is the guard against indexing `$HOME`, a downloads folder or
    a scratch dir just because a session started there. `.exists()` rather than
    `.is_dir()` on purpose: a linked worktree or submodule has `.git` as a FILE."""
    try:
        return folder.is_dir() and (folder / ".git").exists()
    except OSError:
        return False


def read_indexed_at(idx: Path) -> str | None:
    """The index's `indexed_at` ISO timestamp, or None if absent/unreadable/corrupt.

    Reads the JSON directly rather than via `IndexStore` to hold the import boundary
    (see module docstring). These index files reach tens of MB, so this is deliberately
    the ONE field parsed — callers that need more should spawn a tool call, not widen
    this."""
    try:
        with idx.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    at = data.get("indexed_at")
    return at if isinstance(at, str) and at else None


# ---------------------------------------------------------------------------
# Staleness policy
# ---------------------------------------------------------------------------
def _git_tip_stamp(folder: Path) -> str | None:
    """A token that changes when the checked-out commit changes, or None if it can't
    be determined cheaply.

    Two small reads, no subprocess: `.git/HEAD` is either a raw sha (detached) or
    `ref: refs/heads/<branch>`, in which case the branch tip file's mtime moves on
    every commit to it. Returns None rather than guessing when the layout isn't the
    plain case — a LINKED WORKTREE or submodule has `.git` as a file, and a repo whose
    ref lives in `packed-refs` has no loose tip file. Following either would cost more
    reads and more ways to be wrong than the daily floor it degrades to."""
    git = folder / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        tip = git / head[4:].strip()
        if not tip.is_file():  # packed-refs, or a branch with no commits yet
            return None
    else:
        return head[:12] or None  # detached HEAD: the sha IS the stamp
    try:
        return str(int(tip.stat().st_mtime))
    except OSError:
        return None


def should_index(folder: Path, idx: Path) -> str | None:
    """Decide whether to index `folder` now, and under what dedupe key.

    Return None to skip. Return a STAMP string to index — the stamp is also the
    once-only claim key, so whatever granularity it has IS the cadence.

    Policy: **build unconditionally when absent, then refresh on a new commit or once
    a day, whichever comes first.**

      * No index at all is the urgent case and the only one where the user is strictly
        worse off than before this feature existed: every symdex tool answers
        "Repository not found". Build it, no conditions.
      * With an index present, the stamp is `date.gitref` — so it changes when the
        checked-out commit moves (the signal that source actually changed) AND at least
        once a day (the floor that catches uncommitted work, a failed earlier index,
        and repos whose git layout `_git_tip_stamp` declines to read). Cost is bounded
        at one INCREMENTAL reindex per repo per day plus one per commit, and
        `--incremental` only reparses changed files.

    Deliberately NOT every session: session starts are frequent and bursty (a reopened
    window, a crashed session, three terminals on one repo), and an unconditional
    reindex on each is the eager failure this is meant to avoid.

    Known gap this policy cannot close: it only ever runs at SessionStart, so an index
    still goes stale *during* a long editing session no matter what is returned here.
    That is `watch_folder`'s job — which today cannot see added files (its mtime map is
    built from symbols already in the index, so a new file never enters it).
    """
    if not idx.exists():
        return "bootstrap"
    today = datetime.date.today().isoformat()
    tip = _git_tip_stamp(folder)
    return f"{today}.{tip}" if tip else today


def _claim_path(folder: Path, stamp: str) -> Path:
    """One claim file per (folder, stamp). Both components are sanitised because a
    path and a caller-supplied stamp can both contain separators — an unsanitised
    stamp would let the policy escape the scratch dir."""
    slug = _UNSAFE.sub("_", str(folder))[-80:].strip("_")
    tag = _UNSAFE.sub("_", stamp)[:40].strip("_") or "none"
    return state._scratch_file(f"auto_index.{slug}.{tag}")


def maybe_spawn(cfg, folder: Path, stamp: str) -> bool:
    """Ensure a background index of `folder` is (or has been) launched for `stamp`.

    Returns True when an index is in flight — either this call spawned it OR another
    session already claimed this (folder, stamp) slot. Returns False only when it can't
    run: disabled, interpreter missing, or the spawn itself failed. Never raises.

    The once-per-(folder, stamp) guard is an ATOMIC O_EXCL file claim, exactly as in
    `autoupdate.maybe_spawn`: two session_start hooks racing (two windows opening on
    one repo together) must not both write the same index JSON."""
    try:
        if not is_enabled(cfg):
            return False
        exe = Path(sys.executable)
        if not exe.exists():
            return False
        claim = _claim_path(folder, stamp)
        try:
            # Atomic test-and-set: only the FIRST caller creates the file; a concurrent
            # second caller gets FileExistsError and defers.
            fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            return True  # already claimed for this (folder, stamp) — in flight
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            # cwd is deliberately NOT the target folder: an index run must not hold a
            # handle on a directory the user may delete or switch branches under.
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True  # survives the hook exit
        argv = [
            str(exe), "-m", "firekeep_symdex.reindex", str(folder), "--incremental",
        ]
        try:
            subprocess.Popen(argv, **kwargs)  # noqa: S603 — fixed argv, path not shell-interpolated
        except Exception:  # noqa: BLE001
            # Release the claim so a later session can retry a failed launch.
            try:
                claim.unlink()
            except OSError:
                pass
            return False
        return True
    except Exception:  # noqa: BLE001 — auto-index must never cost a session
        return False


def index_nudge(cfg, payload: dict) -> str:
    """One line describing what was done about the index, or '' when there is nothing
    to say. Called from the session_start core; never raises.

    Deliberately silent in the common cases (disabled, not a repo, policy declined) —
    a line printed on every start is the nag this replaces."""
    try:
        if not is_enabled(cfg):
            return ""
        raw = payload.get("cwd") or os.getcwd()
        folder = Path(raw).expanduser().resolve()
        if not is_indexable(folder):
            return ""
        idx = index_file(folder)
        stamp = should_index(folder, idx)
        if not stamp:
            return ""
        if not maybe_spawn(cfg, folder, stamp):
            return (f"\n\n[firekeep] symdex index missing for '{folder.name}' — "
                    f"run: python -m firekeep_symdex.reindex \"{folder}\"")
        verb = "refreshing" if idx.exists() else "building"
        return (f"\n\n[firekeep] {verb} symdex index for '{folder.name}' in background "
                f"(available to symdex tools shortly; disable with "
                f"`FIREKEEP_NO_AUTO_INDEX=1`)")
    except Exception:  # noqa: BLE001 — the nudge must never cost a session
        return ""
