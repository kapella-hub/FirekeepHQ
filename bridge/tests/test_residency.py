"""The residency contract's fail-safe matrix.

These are the tests that make the losslessness claim real. Every one of them
asserts the SAME thing from a different angle: when anything at all is doubtful,
the caller gets a FULL restore. Written first, deliberately.
"""
from __future__ import annotations

import base64
import json as _json

from app.residency import (
    decode_cursor, encode_cursor, filter_since, high_water_of, omission_notice,
    plan_sha_of,
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
    """The load-bearing assertion here is `omitted is None`, not `out == _data()` --
    with the empty-hw guard deleted, filter_since still returns every entry (an
    empty hw makes every stamp compare >=), so `out == _data()` alone would keep
    passing even with the fail-safe gone. Only `omitted is None` catches that."""
    c = encode_cursor(SID, EPOCH, "", "x")
    out, omitted = filter_since(_data(), c, session_id=SID, epoch=EPOCH)
    assert out == _data()
    assert omitted is None


def test_cursor_with_whitespace_only_high_water_is_a_full_restore():
    """M2: whitespace is truthy, so a bare `if not hw` gate would let a
    whitespace-only high-water mark through as if it were real."""
    c = encode_cursor(SID, EPOCH, "   ", "x")
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


# --- C3 + I1 + M3: unknown/unparseable/incomparable age is KEPT, never dropped ---

def test_entry_missing_timestamp_key_is_kept():
    """C3: `(e.get("timestamp") or "") >= hw` turned a missing stamp into "", and
    "" >= hw is False, so the entry was silently DROPPED. Unknown age must be kept:
    we cannot prove the agent already has it."""
    d = _data()
    d["decisions"][0] = {"content": "no timestamp at all"}
    hw = "2026-07-30T11:00:00.000001+00:00"
    c = encode_cursor(SID, EPOCH, hw, plan_sha_of(_data()))
    out, _ = filter_since(d, c, session_id=SID, epoch=EPOCH)
    assert {"content": "no timestamp at all"} in out["decisions"]


def test_entry_with_empty_string_timestamp_is_kept():
    d = _data()
    d["decisions"][0] = {"timestamp": "", "content": "empty stamp"}
    hw = "2026-07-30T11:00:00.000001+00:00"
    c = encode_cursor(SID, EPOCH, hw, plan_sha_of(_data()))
    out, _ = filter_since(d, c, session_id=SID, epoch=EPOCH)
    assert {"timestamp": "", "content": "empty stamp"} in out["decisions"]


def test_bare_string_decision_entry_is_kept_and_does_not_raise():
    """I1: `e.get(...)` assumed every entry is a dict; a bare string raised
    AttributeError. Task 7 calls high_water_of on every ctx_get_shadow, so a
    malformed entry must not turn a degraded-but-readable restore into a hard
    failure -- checked on both filter_since and high_water_of here."""
    d = _data()
    d["decisions"].append("not a dict")
    hw = "2026-07-30T11:00:00.000001+00:00"
    c = encode_cursor(SID, EPOCH, hw, plan_sha_of(_data()))
    out, _ = filter_since(d, c, session_id=SID, epoch=EPOCH)
    assert "not a dict" in out["decisions"]
    assert high_water_of(d) == "2026-07-30T13:00:00.000001+00:00"


def test_bare_string_files_value_is_kept_and_does_not_raise():
    d = _data()
    d["files"]["c.py"] = "not a dict"
    hw = "2026-07-30T11:00:00.000001+00:00"
    c = encode_cursor(SID, EPOCH, hw, plan_sha_of(_data()))
    out, _ = filter_since(d, c, session_id=SID, epoch=EPOCH)
    assert out["files"]["c.py"] == "not a dict"
    assert high_water_of(d) == "2026-07-30T13:00:00.000001+00:00"


def test_high_water_of_skips_non_dict_entries_without_raising():
    d = {
        "decisions": ["not a dict",
                      {"timestamp": "2026-07-30T10:00:00.000001+00:00", "content": "x"}],
        "progress": [],
        "files": {"a.py": "not a dict"},
    }
    assert high_water_of(d) == "2026-07-30T10:00:00.000001+00:00"


def test_naive_stamp_at_the_boundary_is_kept():
    """M3 + M7: a naive stamp is a PREFIX of the same instant with an offset, so raw
    string comparison sorts it LESS despite being newer-or-equal -- it would be
    dropped under `>=` string comparison. Parsing with datetime fixes it. This is
    also the second inclusivity-at-the-boundary test M7 asked for."""
    hw = "2026-07-30T11:00:00.000001+00:00"
    d = _data()
    d["decisions"].append(
        {"timestamp": "2026-07-30T11:00:00.000001", "content": "naive at boundary"})
    c = encode_cursor(SID, EPOCH, hw, plan_sha_of(_data()))
    out, _ = filter_since(d, c, session_id=SID, epoch=EPOCH)
    assert any(x["content"] == "naive at boundary" for x in out["decisions"])


def test_naive_vs_aware_stamp_is_incomparable_and_kept():
    """A stamp that can't be compared to hw (naive vs aware) must be kept, not
    raise and not be dropped."""
    hw = "2026-07-30T11:00:00.000001"   # naive
    d = _data()
    d["decisions"].append(
        {"timestamp": "2026-07-30T12:00:00.000001+00:00", "content": "aware stamp"})
    c = encode_cursor(SID, EPOCH, hw, plan_sha_of(_data()))
    out, _ = filter_since(d, c, session_id=SID, epoch=EPOCH)
    assert any(x["content"] == "aware stamp" for x in out["decisions"])


# --- I2: decode_cursor's guards beyond base64/JSON parsing -----------------

def _raw_cursor(obj) -> str:
    raw = _json.dumps(obj)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def test_decode_cursor_rejects_wrong_version():
    """The version field is the feature's field kill switch -- bumping
    _CURSOR_VERSION is how every outstanding cursor gets invalidated."""
    cursor = _raw_cursor({"v": 2, "sid": SID, "hw": "x", "plan_sha": "y"})
    assert decode_cursor(cursor) is None


def test_decode_cursor_rejects_non_string_sid():
    assert decode_cursor(encode_cursor(None, EPOCH, "x", "y")) is None


def test_decode_cursor_rejects_non_string_hw():
    assert decode_cursor(encode_cursor(SID, EPOCH, 12345, "y")) is None


def test_decode_cursor_rejects_a_json_array():
    cursor = _raw_cursor(["v", 1])
    assert decode_cursor(cursor) is None


def test_decode_cursor_rejects_a_json_string():
    cursor = _raw_cursor("just a string")
    assert decode_cursor(cursor) is None


# --- I3: omission_notice is the wording that carries the losslessness claim ---

def test_omission_notice_names_every_omitted_section():
    notice = omission_notice({"decisions": 2, "progress": 1, "files": 3, "plan": True})
    assert "2 decisions" in notice
    assert "1 progress" in notice
    assert "3 files" in notice
    assert "your unchanged plan" in notice


def test_omission_notice_states_the_content_still_exists():
    notice = omission_notice({"decisions": 1, "progress": 0, "files": 0, "plan": False})
    assert "still exist" in notice


def test_omission_notice_tells_the_reader_how_to_get_the_full_document():
    notice = omission_notice({"decisions": 1, "progress": 0, "files": 0, "plan": False})
    assert "ctx_get_shadow()" in notice


def test_omission_notice_is_empty_when_nothing_was_omitted():
    assert omission_notice({"decisions": 0, "progress": 0, "files": 0, "plan": False}) == ""


def test_omission_notice_never_renders_the_plan_bool_as_a_count():
    """`omitted["plan"]` is a bool, not a count -- it must never render as e.g.
    "True omitted" or be concatenated as a number."""
    notice = omission_notice({"decisions": 0, "progress": 0, "files": 0, "plan": True})
    assert "True" not in notice
    assert "your unchanged plan" in notice
