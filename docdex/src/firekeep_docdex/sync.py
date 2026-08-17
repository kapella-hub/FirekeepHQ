"""Sync orchestration — the module that decides what to send, what to delete,
and above all what NOT to do.

The rules it exists to enforce:

* **Deletions come only from a completed walk (I4a).** Absence of evidence is
  not deletion. An unmounted volume, an unreadable root, a source over the file
  cap: all produce zero deletes and a loud warning.
* **A removal cannot lose a race with a sync (review #6).** `remove` marks the
  source `pending_delete` FIRST, then takes the lock. A sync already running
  re-reads the registry before every ingest batch and stands down — so content
  a human deleted is never uploaded behind the delete.
* **Private-session mode suspends sync (I3), including a run already in
  flight.** "Fully bypassed" has to include background uploads, so the gate is
  re-checked per batch and not just at startup.
* **An unreachable server changes nothing it did not earn.** The run aborts,
  `last_sync_at` is not stamped, and state records only files that genuinely
  reached the server.

Nothing here may raise into a detached background process: `main` catches
everything and reports an exit code.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import docdex_dir, extract, scan, sources, state, wire

# How many files are ingested between re-checks of the bypass flag and the
# source's pending_delete status. Small enough that a suspension takes effect
# in seconds; large enough that the two cheap local reads are not per-file.
BATCH_SIZE = 10

# A lock older than this is assumed to belong to a process that died. Sync runs
# are minutes, not hours; the window is generous on purpose because breaking a
# LIVE lock is the more expensive mistake.
LOCK_STALE_SECONDS = 3600.0

_SUMMARY_COUNTERS = (
    "ingested", "deleted", "pending_delete", "failed", "truncated",
    "skipped_unsupported", "skipped_oversize",
)

# Per-file outcomes that actually wrote something into the in-memory state.
# "unreachable" is deliberately absent: it records nothing, so a run that only
# ever hit an outage must not persist a state file it did not earn.
_MUTATING_OUTCOMES = frozenset({"ingested", "zero", "failed"})


class LockBusy(Exception):
    """Another process holds this source's lock."""


# --- the per-source lock, shared with remove --------------------------------


def lock_dir() -> Path:
    d = docdex_dir() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def lock_path(source_id: str) -> Path:
    return lock_dir() / f"{source_id}.lock"


@contextmanager
def source_lock(source_id: str, *, stale_after: float = LOCK_STALE_SECONDS) -> Iterator[Path]:
    """Hold the source's lock, or raise LockBusy.

    An O_EXCL create: the atomic test-and-set is the point — two session-start
    hooks firing together must not both walk and ingest the same folder.
    """
    path = lock_path(source_id)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if not _is_stale(path, stale_after):
            raise LockBusy(f"source {source_id} is already syncing")
        # The holder died. Reclaim by removing and retrying ONCE: a second
        # FileExistsError means someone else won the reclaim, and they may
        # proceed.
        try:
            path.unlink()
        except OSError:
            pass
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise LockBusy(f"source {source_id} is already syncing") from None
    try:
        os.write(fd, f"{os.getpid()} {state.now()}\n".encode())
    except OSError:
        pass
    finally:
        os.close(fd)
    try:
        yield path
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _is_stale(path: Path, stale_after: float) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) > stale_after
    except OSError:
        return False


# --- gates ------------------------------------------------------------------


def _bypassed() -> bool:
    """Indirection with a purpose: this is the seam the per-batch suspension
    test drives, and the one place private-session mode is consulted."""
    from firekeep_client import resolver

    return resolver.is_bypassed()


def _make_client() -> wire.Client:
    return wire.Client()


def _unreachable(err: Exception) -> bool:
    """A transport failure with no HTTP status never reached the server:
    connection refused, DNS, TLS, timeout. That is an outage, and the honest
    response is to stop the run rather than mark 5000 files failed."""
    return getattr(err, "status", None) is None


def _gone(err: Exception) -> bool:
    """A 404 on delete means the replica is not there — which is the outcome
    the delete wanted. Anything else tombstones the file forever."""
    return getattr(err, "status", None) == 404


# --- one source -------------------------------------------------------------


def _blank(src: sources.Source) -> dict:
    summary = {
        "source_id": src.id, "path": src.path, "visibility": src.visibility,
        "status": "synced", "walk_completed": False, "warnings": [],
    }
    summary.update({key: 0 for key in _SUMMARY_COUNTERS})
    return summary


def sync_source(source_id: str, *, client: wire.Client) -> dict:
    """Sync one source. Never raises for anything the caller can act on;
    returns an honest summary instead. Raises ValueError only for an unknown
    source id, which is a caller bug."""
    src = sources.get(source_id)
    if src is None:
        raise ValueError(f"unknown source: {source_id}")
    summary = _blank(src)

    if _bypassed():
        summary["status"] = "aborted"
        summary["warnings"].append("private-session mode (bypass) is on — sync suspended")
        return summary

    try:
        with source_lock(src.id):
            if src.status == sources.PENDING_DELETE:
                return _finish_removal(src, summary, client)
            return _sync_locked(src, summary, client)
    except LockBusy:
        summary["status"] = "locked"
        summary["warnings"].append("another sync or removal holds this source")
        return summary


def _sync_locked(src: sources.Source, summary: dict, client: wire.Client) -> dict:
    walk = scan.walk(src.root)
    summary["walk_completed"] = walk.completed
    summary["skipped_unsupported"] = walk.unsupported
    summary["skipped_oversize"] = len(walk.oversize)

    if walk.too_many:
        # Loud, and NOTHING is written: a source that silently indexed the
        # first 5000 of 40000 files would look synced and be wrong forever.
        summary["status"] = "refused"
        summary["warnings"].append(
            f"more than {scan.max_files()} indexable files — the source is REFUSED "
            f"until it is narrowed (raise FIREKEEP_DOCDEX_MAX_FILES to override)"
        )
        return summary

    if not walk.completed:
        summary["warnings"].append(
            f"the folder could not be read ({walk.root_error}) — nothing was "
            f"deleted, because a walk that did not complete is not evidence "
            f"that anything is gone"
        )
    if walk.errors:
        summary["warnings"].append(
            f"{len(walk.errors)} path(s) could not be read and are excluded from "
            f"deletion inference: {', '.join(walk.errors[:5])}"
        )

    current = state.read_state(src.id)
    changed = False
    aborted: str | None = None

    work = [rel for rel, digest in sorted(walk.files.items())
            if state.needs_sync(current.files.get(rel), digest)]

    for start in range(0, len(work), BATCH_SIZE):
        gate = _batch_gate(src.id)
        if gate:
            aborted = gate
            break
        for rel in work[start:start + BATCH_SIZE]:
            outcome = _sync_one(src, rel, walk.files[rel], current, summary, client)
            changed = changed or outcome in _MUTATING_OUTCOMES
            if outcome == "unreachable":
                aborted = "the server is unreachable — sync aborted, nothing further attempted"
                break
        if aborted:
            break

    if aborted is None and walk.completed:
        deleted_changed, aborted = _apply_deletions(src, walk, current, summary, client)
        changed = changed or deleted_changed

    # Derived from state in ONE place rather than incremented along the way:
    # every surviving tombstone is a replica the server has not confirmed gone,
    # whether it failed this run or three runs ago.
    summary["pending_delete"] = sum(1 for f in current.files.values() if f.pending_delete)

    if aborted is not None:
        summary["status"] = "aborted"
        summary["warnings"].append(aborted)
        # An aborted run is not a sync, so `last_sync_at` is NOT stamped. What
        # genuinely reached the server IS recorded — state is a factual claim
        # about the server, and a file that landed did land. When nothing was
        # earned, nothing is written at all, so an unreachable server leaves
        # the state file exactly as it found it.
        if changed:
            state.write_state(src.id, current)
        return summary

    current.last_sync_at = state.now()
    current.last_walk_completed = walk.completed
    state.write_state(src.id, current)
    return summary


def _batch_gate(source_id: str) -> str | None:
    """Re-checked before EVERY batch: the two ways a run must stop mid-flight."""
    if _bypassed():
        return "private-session mode (bypass) turned on mid-run — sync suspended"
    live = sources.get(source_id)
    if live is None or live.status == sources.PENDING_DELETE:
        # The human removed this source while we were uploading it. Stopping
        # here is what keeps the removal from being undone by our own writes.
        return "the source was removed while syncing — stopped before re-uploading it"
    return None


def _sync_one(src, rel: str, digest: str, current, summary: dict, client) -> str:
    text, error = extract.extract(src.root / rel)
    if error is not None:
        state.record_failure(current, rel, digest, error)
        summary["failed"] += 1
        return "failed"

    text, truncated = extract.truncate(text)
    if not text.strip():
        # An honest zero (no OCR, I5). Recorded as SEEN so it is never
        # re-extracted, with no ingested_hash because the server holds nothing.
        state.record_seen_only(current, rel, digest)
        return "zero"

    try:
        client.ingest(src.id, rel, text, visibility=src.visibility,
                      mtime=_mtime(src.root / rel))
    except Exception as e:  # noqa: BLE001 — a per-file failure is data
        if _unreachable(e):
            return "unreachable"
        state.record_failure(current, rel, digest, str(e))
        summary["failed"] += 1
        return "failed"

    state.record_ingested(current, rel, digest, truncated=truncated)
    summary["ingested"] += 1
    summary["truncated"] += 1 if truncated else 0
    return "ingested"


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _apply_deletions(src, walk, current, summary: dict, client) -> tuple[bool, str | None]:
    """Delete replicas of files that are genuinely gone.

    Only reached when the walk COMPLETED. Oversize files and unreadable
    subtrees are excluded: skipped is not deleted, and unreadable is not empty.
    """
    gone = [
        rel for rel in sorted(current.files)
        if rel not in walk.files
        and rel not in walk.oversize
        and not scan.excluded_by_errors(rel, walk.errors)
    ]
    changed = False
    for rel in gone:
        state.mark_pending_delete(current, rel)
        changed = True
        try:
            client.delete_file(src.id, rel)
        except Exception as e:  # noqa: BLE001
            if _unreachable(e):
                return changed, ("the server is unreachable — sync aborted, "
                                 "remaining deletions stay pending")
            if not _gone(e):
                # The tombstone survives in state and `list` shows it, so the
                # replica is retried rather than quietly abandoned. The count
                # is derived from state at the end, never incremented here.
                continue
        state.clear(current, rel)
        summary["deleted"] += 1
    return changed, None


# --- removal ----------------------------------------------------------------


def remove_source(source_id: str, *, client: wire.Client) -> dict:
    """The §2 removal lifecycle: mark → lock → one bulk delete → drop on
    confirmation.

    The mark happens BEFORE the lock deliberately. A sync holding the lock may
    run for minutes; marking first means it sees `pending_delete` at its next
    batch check and stops uploading, instead of racing the removal it is about
    to lose to.
    """
    src = sources.get(source_id)
    if src is None:
        raise ValueError(f"unknown source: {source_id}")
    src = sources.remove_mark(source_id)
    summary = _blank(src)
    try:
        with source_lock(source_id):
            return _finish_removal(src, summary, client)
    except LockBusy:
        summary["status"] = "locked"
        summary["warnings"].append(
            "a sync is running — the source is marked for removal and will be "
            "deleted on the next sync"
        )
        return summary


def _finish_removal(src, summary: dict, client) -> dict:
    """One bounded bulk delete, then drop. Called under the lock, from both
    `remove_source` and a sync that finds a source already pending."""
    removed = 0
    try:
        response = client.delete_source(src.id)
        # The count comes from the SERVER, not from a local guess: one bulk
        # call removes however many replicas were actually there, and state
        # may be stale about that. A 404 reports zero, which is exactly right.
        if isinstance(response, dict) and isinstance(response.get("deleted_sources"), int):
            removed = response["deleted_sources"]
    except Exception as e:  # noqa: BLE001
        if not _gone(e):
            summary["status"] = "remove_pending"
            summary["pending_delete"] = 1
            summary["warnings"].append(
                f"the server did not confirm the deletion ({e}) — the source "
                f"stays pending and is retried on the next sync"
            )
            return summary
        # 404: the server holds nothing under this id (a source removed before
        # it ever synced). That IS the state we wanted.
    sources.drop(src.id)
    state.delete_state(src.id)
    summary["status"] = "removed"
    summary["deleted"] = removed
    return summary


# --- the entrypoint ---------------------------------------------------------


def run_sync(source_id: str | None = None, *, all_sources: bool = False,
             quiet: bool = False, client: wire.Client | None = None) -> dict:
    """Sync one source or every active one.

    `client` is injectable so the wire can be tested; in production it is built
    from `resolver.resolve("cortex")`. Returns
    `{"sources": [...], "ok": bool, "aborted": str | None}`.
    """
    if not all_sources and not source_id:
        raise ValueError("run_sync needs a source id or all_sources=True")

    result: dict = {"sources": [], "ok": True, "aborted": None}

    if _bypassed():
        result["ok"] = False
        result["aborted"] = "private-session mode (bypass) is on — sync suspended"
        return result

    if client is None:
        try:
            client = _make_client()
        except Exception as e:  # noqa: BLE001 — an unconfigured kit is not a crash
            result["ok"] = False
            result["aborted"] = f"cannot reach the Keep: {e}"
            return result

    if source_id:
        targets = [source_id]
        if sources.get(source_id) is None:
            raise ValueError(f"unknown source: {source_id}")
    else:
        targets = [s.id for s in sources.list_sources()]

    for sid in targets:
        summary = sync_source(sid, client=client)
        result["sources"].append(summary)
        if summary["status"] in ("aborted", "refused", "remove_pending"):
            result["ok"] = False
        if summary["failed"]:
            result["ok"] = False
        if summary["status"] == "aborted":
            # Whatever stopped this source — an outage, a bypass — applies to
            # every other source too. Carrying on would just repeat the failure
            # N times.
            result["aborted"] = summary["warnings"][-1] if summary["warnings"] else "aborted"
            break

    if not quiet:
        _print(result)
    return result


def _print(result: dict) -> None:
    for summary in result["sources"]:
        counts = " · ".join(
            f"{key.replace('_', ' ')} {summary[key]}"
            for key in _SUMMARY_COUNTERS if summary[key]
        )
        print(f"{summary['source_id'][:8]}  {summary['status']}  {summary['path']}")
        if counts:
            print(f"    {counts}")
        for warning in summary["warnings"]:
            print(f"    ! {warning}")
    if result["aborted"]:
        print(f"! {result['aborted']}")


def main(argv: list[str] | None = None) -> int:
    """The console entrypoint and the detached-spawn target. Catches
    everything: a traceback out of a background process is a sync that died
    where nobody will ever see it."""
    parser = argparse.ArgumentParser(
        prog="firekeep-docdex sync",
        description="Scan registered folders and sync them into the Keep's corpus.",
    )
    parser.add_argument("--source", help="sync one source by id")
    parser.add_argument("--all", action="store_true", dest="all_sources",
                        help="sync every registered source")
    parser.add_argument("--quiet", action="store_true", help="print nothing")
    args = parser.parse_args(argv)

    if not args.source and not args.all_sources:
        parser.print_usage(sys.stderr)
        return 2
    try:
        result = run_sync(args.source, all_sources=args.all_sources, quiet=args.quiet)
    except Exception as e:  # noqa: BLE001
        from firekeep_client import hooklog

        hooklog.log_failure("docdex", f"sync failed: {e}")
        if not args.quiet:
            print(f"docdex sync failed: {e}", file=sys.stderr)
        return 1
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - exercised via `main`
    sys.exit(main())
