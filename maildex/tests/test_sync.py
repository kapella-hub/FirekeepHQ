"""Sync orchestration: what it sends, what it refuses to do, and what it says
when it stops.

Two families of test carry most of the weight.

`test_the_password_never_touches_disk` spies on every write the interpreter
makes for the duration of a full sync and asserts the secret appears in none of
them. That is M3 as an observed property rather than a design intention.

The abort family pins the docdex timeout semantics verbatim: a request that
timed out says "timed out", and a server that is genuinely gone says
"unreachable". Docdex learned the difference on a live dogfood sync where a
healthy Keep was reported unreachable; maildex is not going to learn it again.
"""
from __future__ import annotations

import builtins
import datetime
import io
import os

import pytest
from conftest import (PASSWORD, FakeVault, SpyIMAP, TransportFailure,
                      connector_for, make_message)

from firekeep_maildex import accounts, imapio, state, sync, vault, wire


def _run(account, client, spy, fake_vault, **kw):
    return sync.sync_account(account.id, client=client,
                             connector=connector_for(spy), call_tool=fake_vault, **kw)


# --- M3: the password is never written anywhere -----------------------------


def test_the_password_never_touches_disk(account, client, spy, fake_vault, monkeypatch):
    """Every write the interpreter makes during a full sync, inspected.

    The spy wraps `open` rather than any maildex function, so it catches a leak
    through a log, a cache, a temp file or a state field alike — including one
    written by code that does not exist yet.
    """
    written: list[str] = []
    real_open = io.open

    def spying_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        if any(flag in str(mode) for flag in ("w", "a", "+", "x")):
            original_write = handle.write

            def recording_write(data):
                written.append(data if isinstance(data, str)
                               else bytes(data).decode("utf-8", "replace"))
                return original_write(data)

            handle.write = recording_write
        return handle

    # BOTH names: `pathlib` reaches for `io.open` and never sees a replacement
    # of `builtins.open`, so patching only the builtin would miss every
    # `Path.write_text` — which is how this package writes all of its state.
    monkeypatch.setattr(io, "open", spying_open)
    monkeypatch.setattr(builtins, "open", spying_open)
    summary = _run(account, client, spy, fake_vault)
    monkeypatch.undo()

    assert summary["ingested"] == 3
    assert written, "the spy caught no writes at all — it would pass vacuously"
    assert not any(PASSWORD in chunk for chunk in written)

    # ...and the same for everything that ended up on disk, however it got there.
    for path in (account and sync.maildex_dir()).rglob("*"):
        if path.is_file():
            assert PASSWORD not in path.read_text(encoding="utf-8", errors="replace")


def test_the_password_never_reaches_the_wire(account, client, server, spy, fake_vault):
    _run(account, client, spy, fake_vault)
    import json
    assert PASSWORD not in json.dumps(server.posts)


def test_the_password_never_reaches_the_summary(account, client, spy, fake_vault):
    summary = _run(account, client, spy, fake_vault)
    assert PASSWORD not in repr(summary)


def test_the_vault_is_read_once_per_sync(account, client, spy, fake_vault):
    """Never cached across runs — revocation via `vault_delete` has to take
    effect on the next sync, not on the next reboot."""
    _run(account, client, spy, fake_vault)
    _run(account, client, spy, fake_vault)
    retrievals = [c for c in fake_vault.calls if c[1] == "vault_retrieve"]
    assert len(retrievals) == 2


def test_a_missing_vault_secret_aborts_with_words_a_person_can_act_on(
        account, client, spy, fake_vault):
    fake_vault.secrets.clear()
    summary = _run(account, client, spy, fake_vault)
    assert summary["status"] == "aborted"
    assert "app password" in summary["warnings"][0]
    assert summary["ingested"] == 0


def test_a_missing_vault_secret_never_opens_a_connection(
        account, client, spy, fake_vault):
    fake_vault.secrets.clear()
    _run(account, client, spy, fake_vault)
    assert spy.logins == []


# --- the happy path ---------------------------------------------------------


def test_a_first_sync_backfills_every_configured_folder(
        account, client, server, spy, fake_vault):
    summary = _run(account, client, spy, fake_vault)
    assert summary["status"] == "synced"
    assert summary["ingested"] == 3
    assert summary["folders"] == {"INBOX": 2, "Sent": 1}
    assert len(server.posts) == 3


def test_the_backfill_query_uses_the_accounts_frozen_horizon(
        account, client, spy, fake_vault):
    _run(account, client, spy, fake_vault)
    criterion, date = spy.searches[0]
    assert criterion == "SINCE"
    expected = datetime.date.today() - datetime.timedelta(days=90)
    assert date == imapio.imap_date(expected)


def test_the_second_sync_is_incremental_from_the_watermark(
        account, client, server, spy, fake_vault):
    _run(account, client, spy, fake_vault)
    server.posts.clear()
    spy.searches.clear()
    summary = _run(account, client, spy, fake_vault)
    assert summary["ingested"] == 0
    assert server.posts == []
    assert spy.searches[0] == ("UID 3:*",)


def test_new_mail_arriving_between_syncs_is_picked_up(
        account, client, server, spy, fake_vault):
    _run(account, client, spy, fake_vault)
    spy.folders["INBOX"][9] = make_message(subject="Fresh", message_id="<9@x>")
    server.posts.clear()
    summary = _run(account, client, spy, fake_vault)
    assert summary["ingested"] == 1
    assert "Fresh" in server.posts[0]["body"]["content"]


def test_the_ingested_payload_carries_the_messages_own_headers(
        account, client, server, spy, fake_vault):
    _run(account, client, spy, fake_vault)
    body = server.posts[0]["body"]
    assert body["metadata"]["folder"] == "INBOX"
    assert body["metadata"]["subject"] == "One"
    assert body["metadata"]["from"] == "priya@example.com"
    assert body["metadata"]["message_id"] == "<1@x>"
    assert body["visibility"] == "member"
    assert body["metadata"]["untrusted_content"] == "true"


def test_every_payload_of_every_sync_is_member_private(
        account, client, server, spy, fake_vault):
    """M1 across the whole run, not just one call."""
    _run(account, client, spy, fake_vault)
    assert {p["body"]["visibility"] for p in server.posts} == {"member"}


def test_last_sync_at_is_stamped_only_on_a_completed_run(
        account, client, spy, fake_vault):
    _run(account, client, spy, fake_vault)
    assert state.read_state(account.id).last_sync_at is not None


# --- M7 at the sync level ---------------------------------------------------


def test_a_uidvalidity_change_re_indexes_the_folder_from_scratch(
        account, client, server, spy, fake_vault):
    """The silent failure this prevents: UIDs restart at 1, a watermark of 4000
    matches nothing, and the mailbox is never indexed again with no error
    anywhere."""
    _run(account, client, spy, fake_vault)
    server.posts.clear()

    spy.uidvalidity["INBOX"] = 901
    summary = _run(account, client, spy, fake_vault)

    assert summary["rebaselined"] == 1
    assert summary["ingested"] == 2
    assert any("UIDVALIDITY" in w for w in summary["warnings"])
    assert spy.searches[-2][0] == "SINCE"  # a full backfill, not UID n:*


def test_a_rebaselined_folder_ingests_under_new_names(
        account, client, server, spy, fake_vault):
    """Disclosed: the old replicas stay in the corpus under their old names.
    The alternative — reusing the name — would overwrite real mail with
    unrelated mail."""
    _run(account, client, spy, fake_vault)
    before = set(server.ingested_names)
    server.posts.clear()
    spy.uidvalidity["INBOX"] = 901
    _run(account, client, spy, fake_vault)
    assert not (before & set(server.ingested_names))


def test_an_unchanged_uidvalidity_is_not_reported_as_a_rebuild(
        account, client, spy, fake_vault):
    _run(account, client, spy, fake_vault)
    summary = _run(account, client, spy, fake_vault)
    assert summary["rebaselined"] == 0
    assert not any("UIDVALIDITY" in w for w in summary["warnings"])


# --- the caps (M6) ----------------------------------------------------------


def test_the_per_sync_cap_stops_the_run_and_says_so(
        account, client, server, spy, fake_vault, monkeypatch):
    monkeypatch.setenv("FIREKEEP_MAILDEX_MAX_PER_SYNC", "2")
    summary = _run(account, client, spy, fake_vault)
    assert summary["capped"] is True
    assert summary["ingested"] == 2
    assert any("cap" in w for w in summary["warnings"])


def test_a_capped_run_continues_from_the_watermark_next_time(
        account, client, server, spy, fake_vault, monkeypatch):
    monkeypatch.setenv("FIREKEEP_MAILDEX_MAX_PER_SYNC", "1")
    first = _run(account, client, spy, fake_vault)
    second = _run(account, client, spy, fake_vault)
    assert first["ingested"] == 1 and second["ingested"] == 1
    assert server.ingested_names[0] != server.ingested_names[1]


def test_the_per_sync_cap_defaults_to_500():
    assert sync.max_per_sync() == 500


def test_an_oversize_message_is_truncated_and_flagged(
        account, client, server, spy, fake_vault, monkeypatch):
    monkeypatch.setenv("FIREKEEP_MAILDEX_MAX_MESSAGE_KB", "1")
    spy.folders["INBOX"][9] = make_message(plain="y" * 5000, message_id="<9@x>")
    summary = _run(account, client, spy, fake_vault)
    assert summary["truncated"] == 1
    body = [p for p in server.posts if len(p["body"]["content"]) > 500][0]
    assert len(body["body"]["content"].encode("utf-8")) <= 1024
    truncated = [m for m in state.read_state(account.id).messages.values() if m.truncated]
    assert len(truncated) == 1


def test_the_backfill_horizon_bounds_the_first_query(
        client, spy, fake_vault, monkeypatch):
    monkeypatch.setenv("FIREKEEP_MAILDEX_BACKFILL_DAYS", "7")
    acct = accounts.add("imap.example.com", "me@example.com")
    fake_vault.secrets[vault.vault_key(acct.id)] = PASSWORD
    _run(acct, client, spy, fake_vault)
    expected = datetime.date.today() - datetime.timedelta(days=7)
    assert spy.searches[0] == ("SINCE", imapio.imap_date(expected))


def test_attachments_travel_as_names_and_their_content_never_does(
        account, client, server, spy, fake_vault):
    spy.folders["INBOX"][9] = make_message(
        plain="see attached", message_id="<9@x>", attachments=("q3.pdf",))
    _run(account, client, spy, fake_vault)
    body = [p["body"] for p in server.posts if p["body"]["metadata"]["message_id"] == "<9@x>"][0]
    assert body["metadata"]["attachments"] == "q3.pdf"
    assert "binary" not in body["content"]


# --- the abort semantics (copied from docdex, verbatim) ---------------------


def test_a_timed_out_ingest_says_timed_out_not_unreachable(
        account, client, server, spy, fake_vault):
    """Saying "unreachable" when health checks answer in 80ms is a lie, and it
    sends the human to look at their network instead of at their message."""
    server.post_hook = lambda i, u, b: (_ for _ in ()).throw(
        TransportFailure("the request timed out after 180s"))
    summary = _run(account, client, spy, fake_vault)
    assert summary["status"] == "aborted"
    warning = summary["warnings"][-1]
    assert "timed out" in warning and "unreachable" not in warning
    assert "FIREKEEP_MAILDEX_INGEST_TIMEOUT_SECONDS" in warning


def test_a_refused_connection_says_unreachable(
        account, client, server, spy, fake_vault):
    server.post_hook = lambda i, u, b: (_ for _ in ()).throw(
        TransportFailure("connection refused"))
    summary = _run(account, client, spy, fake_vault)
    assert "unreachable" in summary["warnings"][-1]


def test_a_response_with_a_status_is_a_per_message_failure_not_an_abort(
        account, client, server, spy, fake_vault):
    """A 422 on one malformed message must not stop the other 499."""
    def one_bad(index, url, body):
        if index == 0:
            raise TransportFailure("unprocessable", status=422)
        return None

    server.post_hook = one_bad
    summary = _run(account, client, spy, fake_vault)
    assert summary["status"] == "synced"
    assert summary["failed"] == 1 and summary["ingested"] == 2


def test_a_failed_message_is_retried_on_the_next_sync(
        account, client, server, spy, fake_vault):
    def one_bad(index, url, body):
        if index == 0:
            raise TransportFailure("unprocessable", status=503)
        return None

    server.post_hook = one_bad
    _run(account, client, spy, fake_vault)
    server.post_hook = None
    server.posts.clear()
    summary = _run(account, client, spy, fake_vault)
    assert summary["ingested"] == 1
    assert summary["failed"] == 0


def test_an_abort_does_not_stamp_last_sync_at(
        account, client, server, spy, fake_vault):
    server.post_hook = lambda i, u, b: (_ for _ in ()).throw(
        TransportFailure("connection refused"))
    _run(account, client, spy, fake_vault)
    assert state.read_state(account.id).last_sync_at is None


def test_an_abort_keeps_what_genuinely_landed(
        account, client, server, spy, fake_vault):
    """State is a factual claim about the server: a message that landed did
    land, and the next run must not re-send it."""
    def die_on_the_third(index, url, body):
        if index >= 2:
            raise TransportFailure("connection refused")
        return None

    server.post_hook = die_on_the_third
    summary = _run(account, client, spy, fake_vault)
    assert summary["status"] == "aborted"
    assert summary["ingested"] == 2
    assert state.read_state(account.id).counts()["messages"] == 2


def test_an_outage_before_anything_landed_writes_no_state_file(
        account, client, server, spy, fake_vault):
    server.post_hook = lambda i, u, b: (_ for _ in ()).throw(
        TransportFailure("connection refused"))
    _run(account, client, spy, fake_vault)
    assert not state.state_path(account.id).exists()


def test_an_unreachable_mail_server_aborts_without_touching_state(
        account, client, fake_vault):
    spy = SpyIMAP({"INBOX": {}})
    summary = sync.sync_account(
        account.id, client=client, call_tool=fake_vault,
        connector=connector_for(spy, fail=OSError("no route to host")))
    assert summary["status"] == "aborted"
    assert "cannot connect" in summary["warnings"][-1]
    assert not state.state_path(account.id).exists()


def test_a_rejected_password_names_revocation_as_the_likely_cause(
        account, client, spy, fake_vault):
    spy.login = lambda u, p: ("NO", [b"AUTHENTICATIONFAILED"])
    summary = _run(account, client, spy, fake_vault)
    assert summary["status"] == "aborted"
    assert "revoked" in summary["warnings"][-1]


# --- per-folder resilience --------------------------------------------------


def test_one_missing_folder_does_not_cost_the_others(
        account, client, server, spy, fake_vault):
    del spy.folders["Sent"]
    summary = _run(account, client, spy, fake_vault)
    assert summary["status"] == "synced"
    assert summary["ingested"] == 2
    assert any("Sent" in w and "skipped" in w for w in summary["warnings"])


def test_a_failed_search_skips_the_folder_rather_than_the_run(
        account, client, spy, fake_vault):
    spy.search_hook = lambda args: ("NO", [b"SEARCH failed"])
    summary = _run(account, client, spy, fake_vault)
    assert summary["status"] == "synced"
    assert any("search failed" in w for w in summary["warnings"])


def test_a_message_that_vanished_between_search_and_fetch_is_a_failure_not_a_crash(
        account, client, spy, fake_vault):
    spy.fetch_hook = lambda uid: ("OK", [None]) if uid == 1 else None
    summary = _run(account, client, spy, fake_vault)
    assert summary["failed"] == 1
    assert summary["ingested"] == 2


def test_a_message_with_nothing_to_index_is_recorded_and_never_retried(
        account, client, server, spy, fake_vault):
    spy.folders["INBOX"] = {1: make_message(plain="", html=None, message_id="<1@x>")}
    spy.folders["Sent"] = {}
    first = _run(account, client, spy, fake_vault)
    second = _run(account, client, spy, fake_vault)
    assert first["ingested"] == 1  # the headers alone are worth indexing
    assert second["ingested"] == 0


# --- the bypass gate (I3) ---------------------------------------------------


def test_bypass_suspends_the_whole_run(account, client, spy, fake_vault, monkeypatch):
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    summary = sync.sync_account(account.id, client=client,
                                connector=connector_for(spy), call_tool=fake_vault)
    assert summary["status"] == "aborted"
    assert "bypass" in summary["warnings"][0]
    assert spy.logins == []


def test_bypass_turned_on_mid_run_stops_the_next_batch(
        account, client, server, spy, fake_vault, monkeypatch):
    """"Fully bypassed" has to include background uploads already in flight."""
    monkeypatch.setattr(sync, "BATCH_SIZE", 1)
    calls = {"n": 0}

    def bypass_after_the_first_batch():
        # Call 1 is sync_account's own gate, call 2 is the first batch gate;
        # the suspension lands on the second batch, with one message already up.
        calls["n"] += 1
        return calls["n"] > 2

    monkeypatch.setattr(sync, "_bypassed", bypass_after_the_first_batch)
    summary = _run(account, client, spy, fake_vault)
    assert summary["status"] == "aborted"
    assert "mid-run" in summary["warnings"][-1]
    assert summary["ingested"] == 1


def test_run_sync_refuses_to_start_under_bypass(account, client, monkeypatch):
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    result = sync.run_sync(all_accounts=True, quiet=True, client=client)
    assert result["ok"] is False and "bypass" in result["aborted"]


# --- the removal race -------------------------------------------------------


def test_a_removal_marked_mid_run_stops_the_upload(
        account, client, server, spy, fake_vault, monkeypatch):
    """The mark happens before the lock precisely so a running sync sees it and
    stands down instead of re-uploading mail the human asked to be gone."""
    monkeypatch.setattr(sync, "BATCH_SIZE", 1)
    real_gate = sync._batch_gate
    calls = {"n": 0}

    def gate(account_id):
        calls["n"] += 1
        if calls["n"] == 2:
            accounts.remove_mark(account_id)
        return real_gate(account_id)

    monkeypatch.setattr(sync, "_batch_gate", gate)
    summary = _run(account, client, spy, fake_vault)
    assert summary["status"] == "aborted"
    assert "removed while syncing" in summary["warnings"][-1]


def test_remove_deletes_the_replicas_the_secret_and_the_state(
        account, client, server, spy, fake_vault):
    _run(account, client, spy, fake_vault)
    summary = sync.remove_account(account.id, client=client, call_tool=fake_vault)
    assert summary["status"] == "removed"
    assert summary["deleted"] == 3  # the count comes from the SERVER
    assert server.deletes[0]["url"].endswith(f"/corpus/dex-sources/{account.id}")
    assert fake_vault.secrets == {}
    assert accounts.get(account.id) is None
    assert not state.state_path(account.id).exists()


def test_remove_uses_one_bulk_call_not_one_per_message(
        account, client, server, spy, fake_vault):
    _run(account, client, spy, fake_vault)
    sync.remove_account(account.id, client=client, call_tool=fake_vault)
    assert len(server.deletes) == 1


def test_a_server_that_does_not_confirm_leaves_the_account_pending(
        account, client, server, fake_vault):
    server.delete_hook = lambda i, u: (_ for _ in ()).throw(
        TransportFailure("connection refused"))
    summary = sync.remove_account(account.id, client=client, call_tool=fake_vault)
    assert summary["status"] == "remove_pending"
    assert accounts.get(account.id).status == accounts.PENDING_DELETE


def test_a_pending_removal_is_finished_by_the_next_sync(
        account, client, server, spy, fake_vault):
    server.delete_hook = lambda i, u: (_ for _ in ()).throw(
        TransportFailure("connection refused"))
    sync.remove_account(account.id, client=client, call_tool=fake_vault)
    server.delete_hook = None
    summary = _run(account, client, spy, fake_vault)
    assert summary["status"] == "removed"
    assert accounts.get(account.id) is None


def test_a_404_on_delete_is_the_outcome_the_delete_wanted(
        account, client, server, fake_vault):
    """A mailbox removed before it ever synced holds nothing server-side."""
    server.delete_hook = lambda i, u: (_ for _ in ()).throw(
        TransportFailure("not found", status=404))
    summary = sync.remove_account(account.id, client=client, call_tool=fake_vault)
    assert summary["status"] == "removed"
    assert summary["deleted"] == 0


def test_a_vault_that_refuses_the_delete_does_not_block_the_removal(
        account, client, server, fake_vault):
    """Deleting a secret is admin-scoped. The corpus replicas are already gone
    and cannot be un-deleted, so the removal completes — and says exactly what
    the human must now do by hand."""
    fake_vault.refuse_delete = True
    summary = sync.remove_account(account.id, client=client, call_tool=fake_vault)
    assert summary["status"] == "removed"
    assert accounts.get(account.id) is None
    assert any("vault_delete" in w for w in summary["warnings"])
    assert any("revoke it" in w for w in summary["warnings"])


def test_remove_refuses_an_unknown_account(client, fake_vault):
    with pytest.raises(ValueError, match="unknown account"):
        sync.remove_account("nope", client=client, call_tool=fake_vault)


# --- the lock ---------------------------------------------------------------


def test_two_syncs_cannot_run_over_the_same_mailbox(account, client, spy, fake_vault):
    with sync.account_lock(account.id):
        summary = _run(account, client, spy, fake_vault)
    assert summary["status"] == "locked"
    assert spy.logins == []


def test_a_locked_removal_is_marked_and_finished_later(account, client, fake_vault):
    with sync.account_lock(account.id):
        summary = sync.remove_account(account.id, client=client, call_tool=fake_vault)
    assert summary["status"] == "locked"
    assert accounts.get(account.id).status == accounts.PENDING_DELETE


def test_a_stale_lock_is_reclaimed(account, client, spy, fake_vault):
    path = sync.lock_path(account.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("999999 old\n", encoding="utf-8")
    os.utime(path, (0, 0))
    summary = sync.sync_account(account.id, client=client,
                                connector=connector_for(spy), call_tool=fake_vault)
    assert summary["status"] == "synced"


def test_a_live_lock_is_not_reclaimed(account, client, spy, fake_vault):
    path = sync.lock_path(account.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("999999 now\n", encoding="utf-8")
    summary = _run(account, client, spy, fake_vault)
    assert summary["status"] == "locked"


def test_the_lock_is_released_even_when_the_body_raises(account):
    with pytest.raises(RuntimeError):
        with sync.account_lock(account.id):
            raise RuntimeError("boom")
    assert not sync.lock_path(account.id).exists()


# --- staleness (what the session-start trigger reads) -----------------------


def test_a_never_synced_mailbox_is_stale(account):
    assert sync.is_stale(account.id) is True
    assert sync.any_stale() is True


def test_a_just_synced_mailbox_is_not_stale(account, client, spy, fake_vault):
    _run(account, client, spy, fake_vault)
    assert sync.is_stale(account.id) is False
    assert sync.any_stale() is False


def test_staleness_uses_the_documented_interval(account, client, spy, fake_vault,
                                                monkeypatch):
    _run(account, client, spy, fake_vault)
    st = state.read_state(account.id)
    st.last_sync_at = (datetime.datetime.now(datetime.timezone.utc)
                       - datetime.timedelta(hours=7)).isoformat()
    state.write_state(account.id, st)
    assert sync.is_stale(account.id) is True
    monkeypatch.setenv("FIREKEEP_MAILDEX_SYNC_INTERVAL_HOURS", "24")
    assert sync.is_stale(account.id) is False


def test_the_sync_interval_defaults_to_six_hours():
    assert sync.sync_interval_hours() == 6


def test_a_corrupt_timestamp_reads_as_stale(account):
    st = state.AccountState(last_sync_at="not a date")
    state.write_state(account.id, st)
    assert sync.hours_since_sync(account.id) is None
    assert sync.is_stale(account.id) is True


# --- run_sync and the entrypoint --------------------------------------------


def test_run_sync_over_every_account(client, spy, fake_vault):
    first = accounts.add("imap.a.test", "me@example.com")
    second = accounts.add("imap.b.test", "me@example.com")
    for acct in (first, second):
        fake_vault.secrets[vault.vault_key(acct.id)] = PASSWORD
    result = sync.run_sync(all_accounts=True, quiet=True, client=client,
                           connector=connector_for(spy), call_tool=fake_vault)
    assert result["ok"] is True
    assert [s["account_id"] for s in result["accounts"]] == [first.id, second.id]


def test_an_outage_on_the_first_account_stops_the_rest(
        client, server, spy, fake_vault):
    """Carrying on would repeat the same failure N times."""
    for host in ("imap.a.test", "imap.b.test"):
        acct = accounts.add(host, "me@example.com")
        fake_vault.secrets[vault.vault_key(acct.id)] = PASSWORD
    server.post_hook = lambda i, u, b: (_ for _ in ()).throw(
        TransportFailure("connection refused"))
    result = sync.run_sync(all_accounts=True, quiet=True, client=client,
                           connector=connector_for(spy), call_tool=fake_vault)
    assert result["ok"] is False
    assert len(result["accounts"]) == 1


def test_run_sync_refuses_to_guess(client):
    with pytest.raises(ValueError, match="account id or all_accounts"):
        sync.run_sync(client=client)


def test_run_sync_refuses_an_unknown_account(client):
    with pytest.raises(ValueError, match="unknown account"):
        sync.run_sync("nope", client=client)


def test_a_failed_message_makes_the_run_not_ok(
        account, client, server, spy, fake_vault):
    server.post_hook = lambda i, u, b: (_ for _ in ()).throw(
        TransportFailure("unprocessable", status=422))
    result = sync.run_sync(account.id, quiet=True, client=client,
                           connector=connector_for(spy), call_tool=fake_vault)
    assert result["ok"] is False


def test_the_printed_summary_names_the_account_and_the_counts(
        account, client, spy, fake_vault, capsys):
    sync.run_sync(account.id, client=client, connector=connector_for(spy),
                  call_tool=fake_vault)
    out = capsys.readouterr().out
    assert account.id[:8] in out
    assert "synced" in out and "ingested 3" in out
    assert PASSWORD not in out


def test_main_needs_a_target(capsys):
    assert sync.main([]) == 2


def test_main_never_raises_out_of_a_background_process(monkeypatch, capsys):
    """A traceback out of a detached spawn is a sync that died where nobody
    will ever see it."""
    def boom(*a, **k):
        raise RuntimeError("something deep failed")

    monkeypatch.setattr(sync, "run_sync", boom)
    assert sync.main(["--all", "--quiet"]) == 1


def test_main_returns_zero_on_a_clean_run(account, monkeypatch, fake_vault, spy,
                                          endpoint, server):
    monkeypatch.setattr(sync, "_make_client", lambda: wire.Client(
        endpoint, post=server.post, delete=server.delete))
    monkeypatch.setattr(sync, "_bypassed", lambda: False)
    monkeypatch.setattr(
        vault, "retrieve", lambda account_id, call_tool=None: PASSWORD)
    monkeypatch.setattr(
        imapio, "_default_connector", connector_for(spy))
    assert sync.main(["--all", "--quiet"]) == 0


def test_an_unconfigured_kit_is_not_a_crash(account, monkeypatch):
    def no_config():
        raise RuntimeError("this machine is not enrolled")

    monkeypatch.setattr(sync, "_make_client", no_config)
    result = sync.run_sync(all_accounts=True, quiet=True)
    assert result["ok"] is False
    assert "cannot reach the Keep" in result["aborted"]


# --- the ingest timeout -----------------------------------------------------


def test_the_ingest_timeout_defaults_to_180_seconds():
    assert sync.ingest_timeout() == 180


def test_the_ingest_timeout_is_env_overridable(monkeypatch):
    monkeypatch.setenv("FIREKEEP_MAILDEX_INGEST_TIMEOUT_SECONDS", "600")
    assert sync.ingest_timeout() == 600


def test_a_nonsense_timeout_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("FIREKEEP_MAILDEX_INGEST_TIMEOUT_SECONDS", "forever")
    assert sync.ingest_timeout() == 180


def test_the_background_client_carries_the_ingest_timeout(configured):
    assert sync._make_client()._timeout == 180.0


# --- the vault seam is narrow -----------------------------------------------


def test_maildex_calls_no_mcp_tool_beyond_the_three_vault_verbs(
        account, client, spy, fake_vault):
    """The fake raises on anything else, so this is a live assertion for the
    whole sync path rather than a claim about the imports."""
    _run(account, client, spy, fake_vault)
    sync.remove_account(account.id, client=client, call_tool=fake_vault)
    assert {tool for _, tool, _ in fake_vault.calls} <= {
        "vault_retrieve", "vault_store", "vault_delete"}


def test_the_fake_vault_would_notice_an_unexpected_tool():
    """The guard's own guard."""
    with pytest.raises(AssertionError, match="unexpected MCP tool"):
        FakeVault()("cortex", "memory_learn", {})
