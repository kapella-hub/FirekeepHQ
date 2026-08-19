"""Watermarks, generations, and the retry split (M7).

The failure this file exists to prevent is the quiet one: a provider rebuilds a
folder, UIDs restart at 1, a watermark of 4000 matches nothing, and the mailbox
is never indexed again — with no error, no warning and no gap in any output.
"""
from __future__ import annotations

from firekeep_maildex import state

ACCOUNT = "a" * 32


def _fresh() -> state.AccountState:
    return state.AccountState()


# --- M7: the generation is part of the identity -----------------------------


def test_the_message_key_carries_the_generation():
    """After a rebuild, UID 7 of generation 900 and UID 7 of generation 901 are
    different messages; one key for both would have the new one inherit the
    old one's recorded failure."""
    assert state.message_key("INBOX", 900, 7) != state.message_key("INBOX", 901, 7)
    assert state.message_key("INBOX", 900, 7) != state.message_key("Sent", 900, 7)


def test_the_first_sight_of_a_folder_is_not_a_rebaseline():
    """There is nothing to invalidate, so it records the generation quietly —
    a "your provider rebuilt this folder" warning on every new mailbox would
    train people to ignore the one that matters."""
    st = _fresh()
    assert state.reconcile(st, "INBOX", 900) is False
    assert st.folders["INBOX"].uidvalidity == 900
    assert st.folders["INBOX"].rebaselined_at is None


def test_the_same_generation_is_not_a_rebaseline():
    st = _fresh()
    state.reconcile(st, "INBOX", 900)
    state.record_ingested(st, "INBOX", 900, 42)
    assert state.reconcile(st, "INBOX", 900) is False
    assert st.folders["INBOX"].last_uid == 42


def test_a_changed_generation_rebaselines_the_folder_from_scratch():
    st = _fresh()
    state.reconcile(st, "INBOX", 900)
    state.record_ingested(st, "INBOX", 900, 4000)
    assert st.folders["INBOX"].last_uid == 4000

    assert state.reconcile(st, "INBOX", 901) is True
    assert st.folders["INBOX"].last_uid == 0
    assert st.folders["INBOX"].uidvalidity == 901
    assert st.folders["INBOX"].rebaselined_at is not None


def test_a_rebaseline_drops_the_old_generations_message_entries():
    """A stale `error` from generation 900 would otherwise put a UID from
    generation 901 in the retry set."""
    st = _fresh()
    state.reconcile(st, "INBOX", 900)
    state.record_failure(st, "INBOX", 900, 7, "503")
    state.reconcile(st, "INBOX", 901)
    assert st.messages == {}
    assert state.retry_uids(st, "INBOX", 901) == []


def test_a_rebaseline_leaves_other_folders_alone():
    st = _fresh()
    state.reconcile(st, "INBOX", 900)
    state.reconcile(st, "Sent", 500)
    state.record_ingested(st, "Sent", 500, 12)
    state.reconcile(st, "INBOX", 901)
    assert st.folders["Sent"].last_uid == 12
    assert state.message_key("Sent", 500, 12) in st.messages


def test_watermarks_of_two_folders_never_mix():
    st = _fresh()
    state.reconcile(st, "INBOX", 900)
    state.reconcile(st, "Sent", 900)
    state.record_ingested(st, "INBOX", 900, 50)
    assert st.folders["Sent"].last_uid == 0


# --- the watermark ----------------------------------------------------------


def test_the_watermark_never_moves_backwards():
    """Out-of-order UIDs in a work set (a retry below the watermark) must not
    rewind it and re-fetch everything after it."""
    st = _fresh()
    state.advance(st, "INBOX", 100)
    state.advance(st, "INBOX", 20)
    assert st.folders["INBOX"].last_uid == 100


def test_every_recorded_outcome_advances_the_watermark():
    for record in (
        lambda st: state.record_ingested(st, "INBOX", 900, 9),
        lambda st: state.record_zero(st, "INBOX", 900, 9),
        lambda st: state.record_failure(st, "INBOX", 900, 9, "boom"),
    ):
        st = _fresh()
        record(st)
        assert st.folders["INBOX"].last_uid == 9


# --- the retry split --------------------------------------------------------


def test_a_failed_message_is_retried_even_though_the_watermark_passed_it():
    """Without this, a single 503 during ingest loses a message permanently:
    the watermark moves past it and no later search names it again."""
    st = _fresh()
    state.record_ingested(st, "INBOX", 900, 5)
    state.record_failure(st, "INBOX", 900, 6, "503 from the corpus")
    state.record_ingested(st, "INBOX", 900, 7)
    assert state.retry_uids(st, "INBOX", 900) == [6]


def test_an_honest_zero_is_never_retried():
    """An image-only message extracts to nothing on every future fetch too.
    Retrying it would re-fetch it forever for the same nothing."""
    st = _fresh()
    state.record_zero(st, "INBOX", 900, 6, note="no indexable text")
    assert state.retry_uids(st, "INBOX", 900) == []
    assert st.messages[state.message_key("INBOX", 900, 6)].note == "no indexable text"


def test_broken_mime_is_terminal_rather_than_retryable():
    st = _fresh()
    state.record_zero(st, "INBOX", 900, 6, note="text/html: UnicodeDecodeError")
    assert st.counts()["unparsed"] == 1
    assert st.counts()["failures"] == 0


def test_a_successful_retry_clears_the_error():
    st = _fresh()
    state.record_failure(st, "INBOX", 900, 6, "503")
    state.record_ingested(st, "INBOX", 900, 6)
    assert state.retry_uids(st, "INBOX", 900) == []


def test_retry_uids_are_scoped_to_one_folder_and_generation():
    st = _fresh()
    state.record_failure(st, "INBOX", 900, 6, "503")
    state.record_failure(st, "Sent", 900, 6, "503")
    state.record_failure(st, "INBOX", 901, 6, "503")
    assert state.retry_uids(st, "INBOX", 900) == [6]


def test_a_failure_keeps_what_the_server_genuinely_still_holds():
    """A retry can then tell "never landed" from "landed, then failed on a
    later attempt"."""
    st = _fresh()
    state.record_ingested(st, "INBOX", 900, 6)
    landed = st.messages[state.message_key("INBOX", 900, 6)].ingested_at
    state.record_failure(st, "INBOX", 900, 6, "503")
    assert st.messages[state.message_key("INBOX", 900, 6)].ingested_at == landed


def test_a_long_error_is_bounded():
    st = _fresh()
    state.record_failure(st, "INBOX", 900, 6, "x" * 5000)
    assert len(st.messages[state.message_key("INBOX", 900, 6)].error) == 500


# --- counts -----------------------------------------------------------------


def test_counts_are_what_list_and_the_doctor_row_read():
    st = _fresh()
    state.reconcile(st, "INBOX", 900)
    state.record_ingested(st, "INBOX", 900, 1)
    state.record_ingested(st, "INBOX", 900, 2, truncated=True)
    state.record_zero(st, "INBOX", 900, 3, note="no indexable text")
    state.record_failure(st, "INBOX", 900, 4, "503")
    assert st.counts() == {"messages": 2, "failures": 1, "truncated": 1,
                           "unparsed": 1, "folders": 1}


# --- persistence ------------------------------------------------------------


def test_state_round_trips_through_disk():
    st = _fresh()
    state.reconcile(st, "INBOX", 900)
    state.record_ingested(st, "INBOX", 900, 7, truncated=True)
    state.record_failure(st, "INBOX", 900, 8, "503")
    st.last_sync_at = state.now()
    state.write_state(ACCOUNT, st)

    back = state.read_state(ACCOUNT)
    assert back.folders["INBOX"].uidvalidity == 900
    assert back.folders["INBOX"].last_uid == 8
    assert back.messages[state.message_key("INBOX", 900, 7)].truncated is True
    assert state.retry_uids(back, "INBOX", 900) == [8]
    assert back.last_sync_at == st.last_sync_at


def test_asking_where_the_state_would_be_does_not_create_it():
    """Otherwise "has this mailbox ever synced?" answers itself wrongly."""
    state.state_path(ACCOUNT)
    assert not state.state_dir().exists()


def test_a_missing_state_file_reads_as_empty():
    assert state.read_state("never-seen").folders == {}


def test_a_corrupt_state_file_costs_a_re_fetch_not_a_phantom_deletion():
    state.write_state(ACCOUNT, _fresh())
    state.state_path(ACCOUNT).write_text("{{{", encoding="utf-8")
    back = state.read_state(ACCOUNT)
    assert back.folders == {} and back.messages == {}
    assert state.state_path(ACCOUNT).read_text(encoding="utf-8") == "{{{"


def test_state_written_by_a_future_version_ignores_fields_it_does_not_know():
    state.write_state(ACCOUNT, _fresh())
    import json
    raw = json.loads(state.state_path(ACCOUNT).read_text(encoding="utf-8"))
    raw["folders"] = {"INBOX": {"uidvalidity": 900, "last_uid": 4, "future": "x"}}
    raw["messages"] = {"INBOX|900|4": {"ingested_at": "now", "unknown": 1}}
    state.state_path(ACCOUNT).write_text(json.dumps(raw), encoding="utf-8")
    back = state.read_state(ACCOUNT)
    assert back.folders["INBOX"].last_uid == 4
    assert back.messages["INBOX|900|4"].ingested_at == "now"


def test_delete_state_is_idempotent():
    state.delete_state(ACCOUNT)
    state.write_state(ACCOUNT, _fresh())
    state.delete_state(ACCOUNT)
    state.delete_state(ACCOUNT)
    assert not state.state_path(ACCOUNT).exists()
