"""Per-key TTL on scratch markers.

Scratch markers never expired. That is a live customer-facing bug for one of
them — `tasks_digest_{agent}@{profile}` has no session component and no TTL, so
once the pending-task set stops changing the customer is never told about their
own tasks again, across every future session on that machine — and it is a
blocker for anything that wants to key correctness on a marker's freshness.

The fix is opt-in PER KEY, at the write site, because the markers have
deliberately different lifetimes:

  tasks_digest_*         suppression digest      -> wants a TTL
  session_current_*      12h embedded-ts TTL     -> manages its own
  sidecar pid guard      process lifetime        -> must not expire
  update-check stamp     one calendar day        -> manages its own
  presence_registered_*  5s race window          -> harmless either way

A key that does not pass `ttl_seconds` must behave EXACTLY as it does today.
That is what makes this change lossless rather than a blanket sweep that would
break session attribution, the sidecar singleton, and the once-a-day
auto-update guard.
"""

from __future__ import annotations

import time

from firekeep_client import state


# --- opt-out: no ttl_seconds means today's behaviour, unchanged -------------


def test_marker_without_a_ttl_never_expires():
    state.write_scratch("sidecar_pid_agent", "4242")
    assert state.read_scratch("sidecar_pid_agent") == "4242"
    # No expiry was declared, so no amount of elapsed time may drop it.
    state.reap_stale(max_age_seconds=0)
    assert state.read_scratch("sidecar_pid_agent") == "4242"


def test_writing_without_a_ttl_clears_a_previously_declared_one():
    """A key that becomes permanent must not stay on the old expiry clock."""
    state.write_scratch("k", "v1", ttl_seconds=-1)      # already expired
    assert state.read_scratch("k") is None
    state.write_scratch("k", "v2")                      # rewritten, no TTL
    assert state.read_scratch("k") == "v2"


# --- opt-in ----------------------------------------------------------------


def test_marker_is_readable_before_its_ttl_lapses():
    state.write_scratch("k", "v", ttl_seconds=3600)
    assert state.read_scratch("k") == "v"


def test_marker_reads_as_absent_after_its_ttl_lapses():
    state.write_scratch("k", "v", ttl_seconds=3600)
    # Move the declared expiry into the past rather than sleeping.
    state._scratch_ttl_file("k").write_text(str(time.time() - 1), encoding="utf-8")
    assert state.read_scratch("k") is None


def test_a_corrupt_expiry_reads_as_expired():
    """Fail-safe direction: an unreadable expiry must not be treated as 'still
    fresh'. Both consumers want the same fallback — a lapsed suppression digest
    re-announces tasks (accuracy-positive), and a lapsed cursor forces a full
    restore (lossless)."""
    state.write_scratch("k", "v", ttl_seconds=3600)
    state._scratch_ttl_file("k").write_text("not-a-number", encoding="utf-8")
    assert state.read_scratch("k") is None


def test_delete_scratch_removes_the_expiry_sidecar():
    """Otherwise a re-created key inherits the dead key's expiry and reads as
    absent the moment it is written."""
    state.write_scratch("k", "v", ttl_seconds=-1)
    state.delete_scratch("k")
    state.write_scratch("k", "fresh")
    assert state.read_scratch("k") == "fresh"


# --- the sweep -------------------------------------------------------------


def test_reap_removes_expired_markers_and_spares_everything_else():
    state.write_scratch("expired", "x", ttl_seconds=-1)
    state.write_scratch("still_fresh", "y", ttl_seconds=3600)
    state.write_scratch("no_ttl", "z")

    state.reap_stale()

    assert state.read_scratch("expired") is None
    assert not state._scratch_file("expired").exists()      # actually cleaned up
    assert not state._scratch_ttl_file("expired").exists()  # sidecar too
    assert state.read_scratch("still_fresh") == "y"
    assert state.read_scratch("no_ttl") == "z"


def test_reap_does_not_expire_the_session_stash_by_age():
    """The stash carries its own embedded-ts TTL and passes no ttl_seconds.
    Reaping it on file age would drop a live session's id and mis-attribute
    every subsequent memory call — the exact degradation a blanket sweep of
    scratch/ would have caused."""
    state.write_session_stash("agent-a", "personal", session_id="sess-123")
    state.reap_stale(max_age_seconds=0)
    assert (state.read_session_stash("agent-a", "personal") or {}).get("session_id") == "sess-123"
