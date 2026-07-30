"""The residency contract's fail-safe matrix.

These are the tests that make the losslessness claim real. Every one of them
asserts the SAME thing from a different angle: when anything at all is doubtful,
the caller gets a FULL restore. Written first, deliberately.
"""
from __future__ import annotations

from app.residency import (
    decode_cursor, encode_cursor, filter_since, high_water_of, plan_sha_of,
)

SID = "sess-1"
EPOCH = "1000"


def _data(**over):
    d = {
        "goal": "g", "status": "active", "plan": "- [ ] step one",
        "decisions": [
            {"timestamp": "2026-07-30T10:00:00.000001+00:00", "content": "chose A"},
            {"timestamp": "2026-07-30T12:00:00.000001+00:00", "content": "chose B"},
        ],
        "progress": [
            {"timestamp": "2026-07-30T11:00:00.000001+00:00", "content": "did X"},
        ],
        "files": {
            "a.py": {"summary": "old", "last_action": "2026-07-30T09:00:00.000001+00:00"},
            "b.py": {"summary": "new", "last_action": "2026-07-30T13:00:00.000001+00:00"},
        },
        "scratch": {"workspace_snapshot": "branch=main"},
    }
    d.update(over)
    return d


# --- codec -----------------------------------------------------------------

def test_cursor_round_trips():
    c = encode_cursor(SID, EPOCH, "2026-07-30T11:00:00.000001+00:00", "abc123")
    got = decode_cursor(c)
    assert got["sid"] == SID and got["epoch"] == EPOCH
    assert got["hw"] == "2026-07-30T11:00:00.000001+00:00"
    assert got["plan_sha"] == "abc123"


def test_garbage_cursor_decodes_to_none():
    for junk in ("", "not-base64!!", "eyJ9", "null", "[]"):
        assert decode_cursor(junk) is None


# --- the five fail-safes: every one must yield a FULL restore --------------

def test_no_cursor_is_a_full_restore():
    out, omitted = filter_since(_data(), None, session_id=SID, epoch=EPOCH)
    assert out == _data()
    assert omitted is None


def test_unknown_cursor_is_a_full_restore():
    out, omitted = filter_since(_data(), "garbage", session_id=SID, epoch=EPOCH)
    assert out == _data()
    assert omitted is None


def test_cursor_from_a_different_session_is_a_full_restore():
    c = encode_cursor("other-session", EPOCH, "2026-07-30T12:00:00.000001+00:00", "x")
    out, omitted = filter_since(_data(), c, session_id=SID, epoch=EPOCH)
    assert out == _data()
    assert omitted is None


def test_cursor_with_a_stale_epoch_is_a_full_restore():
    """precompact bumped the epoch: the agent's context was compacted, so any
    cursor it still holds is a lie about what it can see."""
    c = encode_cursor(SID, "999", "2026-07-30T12:00:00.000001+00:00", "x")
    out, omitted = filter_since(_data(), c, session_id=SID, epoch="1000")
    assert out == _data()
    assert omitted is None


def test_cursor_with_no_high_water_is_a_full_restore():
    c = encode_cursor(SID, EPOCH, "", "x")
    out, omitted = filter_since(_data(), c, session_id=SID, epoch=EPOCH)
    assert out == _data()
    assert omitted is None


# --- the delta itself ------------------------------------------------------

def test_delta_keeps_entries_at_or_after_the_high_water_mark():
    """INCLUSIVE comparison: re-sending the boundary entry costs a few tokens,
    dropping it costs correctness. Duplication beats omission."""
    hw = "2026-07-30T11:00:00.000001+00:00"
    c = encode_cursor(SID, EPOCH, hw, plan_sha_of(_data()))
    out, omitted = filter_since(_data(), c, session_id=SID, epoch=EPOCH)
    assert [d["content"] for d in out["decisions"]] == ["chose B"]
    assert [p["content"] for p in out["progress"]] == ["did X"]     # == hw, kept
    assert list(out["files"]) == ["b.py"]
    assert omitted["decisions"] == 1
    assert omitted["files"] == 1


def test_delta_always_sends_scratch_in_full():
    """scratch entries carry NO timestamp (bridge/app/session.py: hset(key,
    content)), so they cannot be time-filtered. Sending them all is the only
    lossless option — and precompact's workspace snapshot lives here, which is
    exactly what a post-compaction agent needs most."""
    c = encode_cursor(SID, EPOCH, "2026-07-30T23:00:00.000001+00:00", plan_sha_of(_data()))
    out, _ = filter_since(_data(), c, session_id=SID, epoch=EPOCH)
    assert out["scratch"] == {"workspace_snapshot": "branch=main"}


def test_delta_omits_the_plan_only_when_its_hash_matches():
    d = _data()
    c = encode_cursor(SID, EPOCH, "2026-07-30T23:00:00.000001+00:00", plan_sha_of(d))
    out, omitted = filter_since(d, c, session_id=SID, epoch=EPOCH)
    assert out["plan"] == ""
    assert omitted["plan"] is True

    changed = _data(plan="- [x] step one")
    out2, omitted2 = filter_since(changed, c, session_id=SID, epoch=EPOCH)
    assert out2["plan"] == "- [x] step one"
    assert omitted2["plan"] is False


def test_delta_always_sends_proactive_memories_in_full():
    """`set_proactive_memories` REPLACES the whole `nb:session:{sid}:proactive`
    JSON blob on each proactive-recall trigger — it is not append-only and has no
    per-entry timestamp, so treating its absence as 'unchanged' would hide a full
    replacement. filter_since must not touch it. Note the `### Relevant Past
    Experience` section is also CONDITIONAL (emitted only when non-empty), so
    nothing may assume the shadow has a fixed section count."""
    d = _data(proactive_memories=[{"score": 0.9, "content": "seen this before"}])
    c = encode_cursor(SID, EPOCH, "2026-07-30T23:00:00.000001+00:00", plan_sha_of(d))
    out, _ = filter_since(d, c, session_id=SID, epoch=EPOCH)
    assert out["proactive_memories"] == d["proactive_memories"]


def test_delta_preserves_the_header_fields():
    """goal/status/created_at drive the shadow header. Omitting them would make a
    delta unreadable as a document."""
    c = encode_cursor(SID, EPOCH, "2026-07-30T23:00:00.000001+00:00", "x")
    out, _ = filter_since(_data(), c, session_id=SID, epoch=EPOCH)
    assert out["goal"] == "g" and out["status"] == "active"


def test_high_water_of_is_the_newest_timestamp_across_every_section():
    assert high_water_of(_data()) == "2026-07-30T13:00:00.000001+00:00"   # b.py


def test_high_water_of_empty_session_is_empty_not_a_crash():
    assert high_water_of({"decisions": [], "progress": [], "files": {}}) == ""


def test_delta_union_equals_a_full_restore():
    """No entry may be reachable only via one path. A full restore, then a delta
    taken at that point, must together cover every entry the session holds."""
    d = _data()
    full, _ = filter_since(d, None, session_id=SID, epoch=EPOCH)
    c = encode_cursor(SID, EPOCH, high_water_of(d), plan_sha_of(d))
    later = _data()
    later["decisions"] = d["decisions"] + [
        {"timestamp": "2026-07-30T14:00:00.000001+00:00", "content": "chose C"}]
    delta, _ = filter_since(later, c, session_id=SID, epoch=EPOCH)

    seen = {x["content"] for x in full["decisions"]} | {x["content"] for x in delta["decisions"]}
    assert seen == {"chose A", "chose B", "chose C"}
