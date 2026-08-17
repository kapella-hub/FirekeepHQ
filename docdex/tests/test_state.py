"""Per-source state. The seen/ingested hash SPLIT is the contract here."""
from __future__ import annotations

import stat
import sys

import pytest

from firekeep_docdex import state

SID = "a" * 32
DIGEST = "b" * 64
OTHER = "c" * 64


def test_state_lives_under_the_isolated_firekeep_home(firekeep_home):
    assert state.state_path(SID) == firekeep_home / "docdex" / "state" / f"{SID}.json"


def test_missing_state_reads_as_empty(firekeep_home):
    s = state.read_state(SID)
    assert s.files == {}
    assert s.last_sync_at is None


def test_round_trip(firekeep_home):
    s = state.read_state(SID)
    s.files["notes/a.md"] = state.FileState(
        seen_hash=DIGEST, ingested_hash=DIGEST, ingested_at="2026-08-16T00:00:00+00:00",
        truncated=True, error=None, pending_delete=False,
    )
    s.last_sync_at = "2026-08-16T00:00:00+00:00"
    s.last_walk_completed = True
    state.write_state(SID, s)

    back = state.read_state(SID)
    assert back.files["notes/a.md"].seen_hash == DIGEST
    assert back.files["notes/a.md"].truncated is True
    assert back.last_sync_at == "2026-08-16T00:00:00+00:00"
    assert back.last_walk_completed is True


def test_corrupt_state_reads_as_empty_and_is_left_in_place(firekeep_home, tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path / "logs"))
    p = state.state_path(SID)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ broken", encoding="utf-8")
    assert state.read_state(SID).files == {}
    assert p.read_text(encoding="utf-8") == "{ broken"
    assert (tmp_path / "logs" / "hooks.log").exists()


def test_delete_state_is_idempotent(firekeep_home):
    state.write_state(SID, state.SourceState())
    state.delete_state(SID)
    state.delete_state(SID)
    assert not state.state_path(SID).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_state_file_is_0600(firekeep_home):
    state.write_state(SID, state.SourceState())
    assert stat.S_IMODE(state.state_path(SID).stat().st_mode) == 0o600


def test_write_leaves_no_temp_file_behind(firekeep_home):
    state.write_state(SID, state.SourceState())
    leftovers = [p.name for p in state.state_path(SID).parent.iterdir()]
    assert leftovers == [f"{SID}.json"]


# --- the seen/ingested split (review #6) ------------------------------------


def test_an_unseen_file_needs_sync():
    assert state.needs_sync(None, DIGEST) is True


def test_an_unchanged_ingested_file_does_not():
    fs = state.FileState(seen_hash=DIGEST, ingested_hash=DIGEST)
    assert state.needs_sync(fs, DIGEST) is False


def test_changed_content_needs_sync():
    fs = state.FileState(seen_hash=DIGEST, ingested_hash=DIGEST)
    assert state.needs_sync(fs, OTHER) is True


def test_an_honest_zero_extraction_is_not_retried():
    """A scanned PDF yields no text. That is a FINAL answer for those bytes:
    seen_hash records it, ingested_hash stays empty because nothing was
    ingested, and the file must not re-enter the work set every six hours."""
    fs = state.FileState(seen_hash=DIGEST, ingested_hash=None, error=None)
    assert state.needs_sync(fs, DIGEST) is False


def test_a_transient_ingest_failure_is_retried():
    """Same bytes, same empty ingested_hash — but an error was recorded, so
    the server never got this file and next sync must try again. This is the
    whole reason the two hashes are separate fields."""
    fs = state.FileState(seen_hash=DIGEST, ingested_hash=None, error="server 503")
    assert state.needs_sync(fs, DIGEST) is True


def test_a_file_awaiting_deletion_is_not_re_ingested():
    fs = state.FileState(seen_hash=DIGEST, ingested_hash=DIGEST, pending_delete=True)
    assert state.needs_sync(fs, DIGEST) is True  # its bytes came back — resurrect it


def test_recording_an_honest_zero_clears_a_previous_error(firekeep_home):
    s = state.SourceState()
    s.files["a.pdf"] = state.FileState(seen_hash=OTHER, error="boom")
    state.record_seen_only(s, "a.pdf", DIGEST)
    assert s.files["a.pdf"].seen_hash == DIGEST
    assert s.files["a.pdf"].ingested_hash is None
    assert s.files["a.pdf"].error is None
    assert state.needs_sync(s.files["a.pdf"], DIGEST) is False


def test_recording_an_ingest_sets_both_hashes(firekeep_home):
    s = state.SourceState()
    state.record_ingested(s, "a.md", DIGEST, truncated=True)
    fs = s.files["a.md"]
    assert fs.seen_hash == fs.ingested_hash == DIGEST
    assert fs.truncated is True and fs.error is None and fs.ingested_at


def test_recording_a_failure_leaves_ingested_hash_behind(firekeep_home):
    s = state.SourceState()
    state.record_ingested(s, "a.md", OTHER)
    state.record_failure(s, "a.md", DIGEST, "server 503")
    fs = s.files["a.md"]
    assert fs.seen_hash == DIGEST      # we saw the new bytes
    assert fs.ingested_hash == OTHER   # the server still holds the old ones
    assert state.needs_sync(fs, DIGEST) is True


# --- counters the doctor row reads ------------------------------------------


def test_counts(firekeep_home):
    s = state.SourceState()
    state.record_ingested(s, "a.md", DIGEST, truncated=True)
    state.record_failure(s, "b.md", DIGEST, "boom")
    state.record_ingested(s, "c.md", DIGEST)
    state.mark_pending_delete(s, "c.md")
    counts = s.counts()
    assert counts == {"files": 3, "failures": 1, "pending_deletes": 1, "truncated": 1}


def test_mark_pending_delete_on_an_unknown_file_is_a_noop(firekeep_home):
    s = state.SourceState()
    state.mark_pending_delete(s, "ghost.md")
    assert s.files == {}
