"""Sync orchestration — the module that decides what to fetch, what to send,
and above all what NOT to do.

The rules it exists to enforce:

* **The app password lives in one local variable for the life of one connection
  (M3).** It is pulled from the vault at the top of `_sync_locked`, handed to
  `imapio.session`, and dropped in a `finally`. It is never an attribute, never
  a default argument, never part of a summary, and never in an error string —
  the `finally` matters because an exception's traceback holds every frame's
  locals alive as long as the exception does.
* **Nothing here can change a mailbox (M2).** Every read goes through
  `imapio.Session`, whose whole design is that there is no other verb.
* **A removal cannot lose a race with a sync.** `remove` marks the account
  `pending_delete` FIRST, then takes the lock. A sync already running re-reads
  the registry before every batch, sees the flag, and stops — so mail a human
  asked to be gone is never re-uploaded behind the delete.
* **Private-session mode suspends sync (I3), including a run already in
  flight.** "Fully bypassed" has to include background uploads, so the gate is
  re-checked per batch and not just at startup.
* **An unreachable server changes nothing it did not earn.** The run aborts,
  `last_sync_at` is not stamped, and state records only messages that genuinely
  reached the server.

Nothing here may raise into a detached background process: `main` catches
everything and reports an exit code.
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import accounts, env_int, imapio, maildex_dir, parse, state, vault, wire

# How many messages are ingested between re-checks of the bypass flag and the
# account's pending_delete status. Small enough that a suspension takes effect
# in seconds; large enough that the two cheap local reads are not per-message.
BATCH_SIZE = 10

# A lock older than this is assumed to belong to a process that died. Sync runs
# are minutes, not hours; the window is generous on purpose because breaking a
# LIVE lock is the more expensive mistake.
LOCK_STALE_SECONDS = 3600.0

DEFAULT_MAX_PER_SYNC = 500
DEFAULT_SYNC_INTERVAL_HOURS = 6
DEFAULT_INGEST_TIMEOUT_SECONDS = 180

_SUMMARY_COUNTERS = ("ingested", "empty", "failed", "truncated", "unparsed",
                     "rebaselined", "deleted")

# Per-message outcomes that actually wrote something into the in-memory state.
# A server-loss abort (_ServerLost) is deliberately not an outcome: it records
# nothing, so a run that only ever hit an outage must not persist a state file
# it did not earn.
_MUTATING_OUTCOMES = frozenset({"ingested", "empty", "failed"})


class LockBusy(Exception):
    """Another process holds this account's lock."""


# --- the disclosed caps (M6) ------------------------------------------------


def max_per_sync() -> int:
    """`FIREKEEP_MAILDEX_MAX_PER_SYNC`, default 500 messages."""
    return env_int("FIREKEEP_MAILDEX_MAX_PER_SYNC", DEFAULT_MAX_PER_SYNC)


def sync_interval_hours() -> int:
    """`FIREKEEP_MAILDEX_SYNC_INTERVAL_HOURS`, default 6.

    The staleness threshold the session-start trigger reads — not a schedule.
    Maildex has no daemon and no OS timer; a mailbox goes stale and the next
    supported session start notices.
    """
    return env_int("FIREKEEP_MAILDEX_SYNC_INTERVAL_HOURS", DEFAULT_SYNC_INTERVAL_HOURS)


def ingest_timeout() -> int:
    """`FIREKEEP_MAILDEX_INGEST_TIMEOUT_SECONDS`, default 180.

    The transport's 10s default is tuned for the hook path and is simply wrong
    for corpus ingest: the server chunks and EMBEDS the content synchronously,
    and a long thread near the 200KB cap on a CPU-embedding Keep takes well over
    10s. Docdex learned this on its first real dogfood sync — a perfectly
    healthy server reported "unreachable" because one large ingest outlived the
    default — and the fix is copied here rather than re-learned.
    """
    return env_int("FIREKEEP_MAILDEX_INGEST_TIMEOUT_SECONDS", DEFAULT_INGEST_TIMEOUT_SECONDS)


def _make_client() -> wire.Client:
    return wire.Client(timeout=float(ingest_timeout()))


# --- the per-account lock, shared with remove -------------------------------


def lock_dir() -> Path:
    d = maildex_dir() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def lock_path(account_id: str) -> Path:
    return lock_dir() / f"{account_id}.lock"


@contextmanager
def account_lock(account_id: str, *, stale_after: float = LOCK_STALE_SECONDS) -> Iterator[Path]:
    """Hold the account's lock, or raise LockBusy.

    An O_EXCL create: the atomic test-and-set is the point — two session-start
    hooks firing together must not both open the same mailbox and ingest it
    twice.
    """
    path = lock_path(account_id)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if not _is_stale(path, stale_after):
            raise LockBusy(f"account {account_id} is already syncing")
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
            raise LockBusy(f"account {account_id} is already syncing") from None
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


class _ServerLost(RuntimeError):
    """The SERVER (not the message) is the problem — stop the run.

    Carries the honest, already-worded abort message. Raised instead of
    threading outcome tokens because the message depends on WHICH way the
    server was lost, and only the code holding the exception knows."""


def _abort_reason(err: Exception, *, deleting: bool = False) -> str | None:
    """The run-stopping message for a transport failure, or None to treat it
    as a per-message failure.

    A response WITH an HTTP status reached the server: that is a per-message
    problem (recorded, retried through `retry_uids`), never an abort. No status
    splits two honest ways:

      * "timed out" — the request outlived even the generous ingest timeout.
        Could be a very long thread on a busy server, could be an outage; we
        cannot tell from here, and saying "unreachable" when health checks
        answer in 80ms is a lie. Still an abort — a hung server must cost one
        timeout, not one per message.
      * anything else — refused, DNS, TLS: the server is genuinely unreachable,
        and stopping beats marking 500 messages failed."""
    if getattr(err, "status", None) is not None:
        return None
    tail = ("the removal stays pending" if deleting
            else "what landed is kept; the rest retries next sync")
    if "timed out" in str(err):
        return (f"a request timed out after {ingest_timeout()}s — a long message "
                f"on a busy server, or an outage — sync aborted, {tail} "
                f"(raise FIREKEEP_MAILDEX_INGEST_TIMEOUT_SECONDS if your mail is large)")
    return f"the server is unreachable — sync aborted, {tail}"


def _gone(err: Exception) -> bool:
    """A 404 on delete means the replicas are not there — which is the outcome
    the delete wanted. Anything else leaves the account pending."""
    return getattr(err, "status", None) == 404


# --- staleness (what the session-start trigger reads) -----------------------


def hours_since_sync(account_id: str) -> float | None:
    """Hours since this account last completed a sync, or None if it never has."""
    st = state.read_state(account_id)
    if not st.last_sync_at:
        return None
    try:
        when = datetime.datetime.fromisoformat(st.last_sync_at)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    delta = (datetime.datetime.now(datetime.timezone.utc) - when).total_seconds() / 3600.0
    return max(0.0, delta)


def is_stale(account_id: str) -> bool:
    """Whether this account is due a sync. Never synced counts as stale — that
    is the state a freshly added mailbox is in, and the one where a background
    sync is most obviously wanted."""
    hours = hours_since_sync(account_id)
    return hours is None or hours >= sync_interval_hours()


def any_stale() -> bool:
    """Whether ANY registered account is due. The trigger's gate."""
    return any(is_stale(a.id) for a in accounts.list_accounts()
               if a.status == accounts.ACTIVE)


# --- one account ------------------------------------------------------------


def _blank(account: accounts.Account) -> dict:
    summary = {
        "account_id": account.id,
        "username": account.username,
        "host": account.host,
        "status": "synced",
        "capped": False,
        "folders": {},
        "warnings": [],
    }
    summary.update({key: 0 for key in _SUMMARY_COUNTERS})
    return summary


def sync_account(account_id: str, *, client: wire.Client, connector=None,
                 call_tool=None) -> dict:
    """Sync one account. Never raises for anything the caller can act on;
    returns an honest summary instead. Raises ValueError only for an unknown
    account id, which is a caller bug."""
    account = accounts.get(account_id)
    if account is None:
        raise ValueError(f"unknown account: {account_id}")
    summary = _blank(account)

    if _bypassed():
        summary["status"] = "aborted"
        summary["warnings"].append("private-session mode (bypass) is on — sync suspended")
        return summary

    try:
        with account_lock(account.id):
            if account.status == accounts.PENDING_DELETE:
                return _finish_removal(account, summary, client, call_tool)
            return _sync_locked(account, summary, client, connector, call_tool)
    except LockBusy:
        summary["status"] = "locked"
        summary["warnings"].append("another sync or removal holds this account")
        return summary


def _sync_locked(account, summary: dict, client, connector, call_tool) -> dict:
    try:
        password = vault.retrieve(account.id, call_tool=call_tool)
    except vault.VaultError as e:
        summary["status"] = "aborted"
        summary["warnings"].append(f"the app password could not be read: {e}")
        return summary

    current = state.read_state(account.id)
    changed = False
    aborted: str | None = None
    try:
        try:
            with imapio.session(account.host, account.port, account.username,
                                password, connector=connector) as sess:
                changed, aborted = _sync_folders(account, sess, current, summary, client)
        except imapio.AuthError as e:
            aborted = (f"{e} — the app password may have been revoked; "
                       f"re-add the mailbox to store a new one")
        except imapio.ImapError as e:
            aborted = f"{e} — sync aborted, nothing was changed at the provider"
    finally:
        # M3. Dropping the name matters because a traceback keeps every frame's
        # locals alive for as long as the exception object lives, and this
        # function's locals are exactly where the secret is.
        password = None
        del password

    if aborted is not None:
        summary["status"] = "aborted"
        summary["warnings"].append(aborted)
        # An aborted run is not a sync, so `last_sync_at` is NOT stamped. What
        # genuinely reached the server IS recorded — state is a factual claim
        # about the server, and a message that landed did land. When nothing
        # was earned, nothing is written at all.
        if changed:
            state.write_state(account.id, current)
        return summary

    current.last_sync_at = state.now()
    state.write_state(account.id, current)
    return summary


def _sync_folders(account, sess, current, summary: dict, client) -> tuple[bool, str | None]:
    """Walk the account's folders under one connection. Returns
    `(state_changed, abort_reason)`."""
    budget = max_per_sync()
    changed = False
    since = datetime.date.today() - datetime.timedelta(days=account.backfill_days)

    for folder in account.folders:
        if budget <= 0:
            summary["capped"] = True
            break
        try:
            uidvalidity = sess.examine(folder)
        except imapio.ImapError as e:
            # One folder a person mistyped, or one the provider renamed, must
            # not cost the mailbox its other folders.
            summary["warnings"].append(f"{folder}: skipped ({e})")
            continue

        if state.reconcile(current, folder, uidvalidity):
            changed = True
            summary["rebaselined"] += 1
            summary["warnings"].append(
                f"{folder}: the provider rebuilt this folder (UIDVALIDITY changed) — "
                f"it is being re-indexed from scratch, and its previous replicas "
                f"stay in the corpus under their old names"
            )

        fs = state.folder_state(current, folder)
        try:
            fresh = (sess.search_since(since) if fs.last_uid == 0
                     else sess.search_after(fs.last_uid))
        except imapio.ImapError as e:
            summary["warnings"].append(f"{folder}: search failed ({e})")
            continue

        work = sorted(set(fresh) | set(state.retry_uids(current, folder, uidvalidity)))
        if len(work) > budget:
            work = work[:budget]
            summary["capped"] = True
        budget -= len(work)
        summary["folders"][folder] = len(work)

        folder_changed, aborted = _sync_folder(
            account, sess, folder, uidvalidity, work, current, summary, client
        )
        changed = changed or folder_changed
        if aborted is not None:
            return changed, aborted

    if summary["capped"]:
        summary["warnings"].append(
            f"stopped at the {max_per_sync()}-message cap for one run — the rest "
            f"continues from the watermark on the next sync "
            f"(raise FIREKEEP_MAILDEX_MAX_PER_SYNC to fetch more at once)"
        )
    return changed, None


def _sync_folder(account, sess, folder, uidvalidity, work, current, summary,
                 client) -> tuple[bool, str | None]:
    changed = False
    for start in range(0, len(work), BATCH_SIZE):
        gate = _batch_gate(account.id)
        if gate:
            return changed, gate
        for uid in work[start:start + BATCH_SIZE]:
            try:
                outcome = _sync_one(account, sess, folder, uidvalidity, uid,
                                    current, summary, client)
            except _ServerLost as lost:
                return changed, str(lost)
            changed = changed or outcome in _MUTATING_OUTCOMES
    return changed, None


def _batch_gate(account_id: str) -> str | None:
    """Re-checked before EVERY batch: the two ways a run must stop mid-flight."""
    if _bypassed():
        return "private-session mode (bypass) turned on mid-run — sync suspended"
    live = accounts.get(account_id)
    if live is None or live.status == accounts.PENDING_DELETE:
        # The human removed this mailbox while we were uploading it. Stopping
        # here is what keeps the removal from being undone by our own writes.
        return "the account was removed while syncing — stopped before re-uploading it"
    return None


def _sync_one(account, sess, folder, uidvalidity, uid, current, summary, client) -> str:
    try:
        raw = sess.fetch(uid)
    except imapio.ImapError as e:
        # Retryable: a dropped connection or a UID that moved. Recorded with an
        # `error`, which is what puts it back in the next run's work set.
        state.record_failure(current, folder, uidvalidity, uid, str(e))
        summary["failed"] += 1
        return "failed"

    message = parse.parse_message(raw)
    text, truncated = parse.truncate(message.render())

    if not text.strip():
        # Nothing to index: an image-only message, or one whose every part was
        # undecodable. Terminal, not retryable — the same bytes parse the same
        # way in six hours.
        state.record_zero(current, folder, uidvalidity, uid,
                          note=message.error or "no indexable text")
        summary["empty"] += 1
        if message.error:
            summary["unparsed"] += 1
        return "empty"

    try:
        client.ingest(
            account.id, folder, uidvalidity, uid,
            text=text,
            subject=message.headers.get("subject", ""),
            sender=message.headers.get("from", ""),
            date=message.headers.get("date", ""),
            message_id=message.message_id,
            attachments=message.attachments,
        )
    except Exception as e:  # noqa: BLE001 — a per-message failure is data
        reason = _abort_reason(e)
        if reason is not None:
            raise _ServerLost(reason) from e
        state.record_failure(current, folder, uidvalidity, uid, str(e))
        summary["failed"] += 1
        return "failed"

    state.record_ingested(current, folder, uidvalidity, uid,
                          truncated=truncated, note=message.error)
    summary["ingested"] += 1
    summary["truncated"] += 1 if truncated else 0
    if message.error:
        summary["unparsed"] += 1
    return "ingested"


# --- removal ----------------------------------------------------------------


def remove_account(account_id: str, *, client: wire.Client, call_tool=None) -> dict:
    """The M5 removal lifecycle: mark → lock → one bulk delete → forget the
    vault secret → drop on confirmation.

    The mark happens BEFORE the lock deliberately. A sync holding the lock may
    run for minutes; marking first means it sees `pending_delete` at its next
    batch check and stops uploading, instead of racing the removal it is about
    to lose to.
    """
    account = accounts.get(account_id)
    if account is None:
        raise ValueError(f"unknown account: {account_id}")
    account = accounts.remove_mark(account_id)
    summary = _blank(account)
    try:
        with account_lock(account_id):
            return _finish_removal(account, summary, client, call_tool)
    except LockBusy:
        summary["status"] = "locked"
        summary["warnings"].append(
            "a sync is running — the mailbox is marked for removal and will be "
            "deleted on the next sync"
        )
        return summary


def _finish_removal(account, summary: dict, client, call_tool) -> dict:
    """One bounded bulk delete, the vault secret, then drop. Called under the
    lock, from both `remove_account` and a sync that finds an account already
    pending."""
    removed = 0
    try:
        response = client.delete_account(account.id)
        # The count comes from the SERVER, not from a local guess: one bulk
        # call removes however many replicas were actually there, and state
        # may be stale about that. A 404 reports zero, which is exactly right.
        if isinstance(response, dict) and isinstance(response.get("deleted_sources"), int):
            removed = response["deleted_sources"]
    except Exception as e:  # noqa: BLE001
        if not _gone(e):
            summary["status"] = "remove_pending"
            summary["warnings"].append(
                f"the server did not confirm the deletion ({e}) — the mailbox "
                f"stays pending and is retried on the next sync"
            )
            return summary
        # 404: the server holds nothing under this id (a mailbox removed before
        # it ever synced). That IS the state we wanted.

    # Only after the replicas are confirmed gone. A stranded vault secret is a
    # live credential nobody is watching, so it is deleted here rather than
    # left for a human to remember — but a Keep that refuses (deleting a secret
    # is admin-scoped) must not block the removal it cannot undo.
    try:
        vault.delete(account.id, call_tool=call_tool)
    except vault.VaultError as e:
        summary["warnings"].append(
            f"the stored app password could not be deleted ({e}) — remove it "
            f"with `vault_delete {vault.vault_key(account.id)}`, and revoke it "
            f"at your mail provider"
        )

    accounts.drop(account.id)
    state.delete_state(account.id)
    summary["status"] = "removed"
    summary["deleted"] = removed
    return summary


# --- the entrypoint ---------------------------------------------------------


def run_sync(account_id: str | None = None, *, all_accounts: bool = False,
             quiet: bool = False, client: wire.Client | None = None,
             connector=None, call_tool=None) -> dict:
    """Sync one account or every active one.

    `client`, `connector` and `call_tool` are injectable so the wire, the IMAP
    conversation and the vault can be tested offline; in production they are
    built from the resolver. Returns
    `{"accounts": [...], "ok": bool, "aborted": str | None}`.
    """
    if not all_accounts and not account_id:
        raise ValueError("run_sync needs an account id or all_accounts=True")

    result: dict = {"accounts": [], "ok": True, "aborted": None}

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

    if account_id:
        if accounts.get(account_id) is None:
            raise ValueError(f"unknown account: {account_id}")
        targets = [account_id]
    else:
        targets = [a.id for a in accounts.list_accounts()]

    for aid in targets:
        summary = sync_account(aid, client=client, connector=connector, call_tool=call_tool)
        result["accounts"].append(summary)
        if summary["status"] in ("aborted", "remove_pending") or summary["failed"]:
            result["ok"] = False
        if summary["status"] == "aborted":
            # Whatever stopped this account — an outage, a bypass — applies to
            # every other account too. Carrying on would just repeat the failure
            # N times. A per-mailbox problem (a bad password) is the exception
            # that proves it: it is worth one more attempt at most, and one
            # aborted summary is what the human needs to read either way.
            result["aborted"] = summary["warnings"][-1] if summary["warnings"] else "aborted"
            break

    if not quiet:
        _print(result)
    return result


def _print(result: dict) -> None:
    for summary in result["accounts"]:
        counts = " · ".join(
            f"{key} {summary[key]}" for key in _SUMMARY_COUNTERS if summary[key]
        )
        print(f"{summary['account_id'][:8]}  {summary['status']}  "
              f"{summary['username']} at {summary['host']}")
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
        prog="firekeep-maildex sync",
        description="Read registered mailboxes and index recent mail into the Keep's corpus.",
    )
    parser.add_argument("--account", help="sync one account by id")
    parser.add_argument("--all", action="store_true", dest="all_accounts",
                        help="sync every registered account")
    parser.add_argument("--quiet", action="store_true", help="print nothing")
    args = parser.parse_args(argv)

    if not args.account and not args.all_accounts:
        parser.print_usage(sys.stderr)
        return 2
    try:
        result = run_sync(args.account, all_accounts=args.all_accounts, quiet=args.quiet)
    except Exception as e:  # noqa: BLE001
        from firekeep_client import hooklog

        hooklog.log_failure("maildex", f"sync failed: {e}")
        if not args.quiet:
            print(f"maildex sync failed: {e}", file=sys.stderr)
        return 1
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - exercised via `main`
    sys.exit(main())
