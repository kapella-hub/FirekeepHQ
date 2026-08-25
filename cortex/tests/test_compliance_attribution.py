"""Living Instructions round 2 — compliance by_runtime, exposure, and the
corrected ctx_working_state row.

Everything here is ADDITIVE to the round-1 table: the six frozen keys, the
frozen predicates, and the all-sessions headline hits/total/rate are pinned
unchanged. The new per-row `by_runtime` and `exposure` slices are computed
with the SAME predicates over attribution the evals now carry, and every
unattributed session lands in a disclosed bucket ("unattributed" /
"unknown") — never silently dropped, never guessed at.

The corrected row is Correction 1 of the round-2 spec
(docs/superpowers/specs/2026-08-11-living-instructions-design.md): the
cb36570 disclosure claimed the stop-hook snapshot satisfies the ctx row, but
only agent ctx_update(category=plan|decision) writes a context_ref, so the
row measured agent discipline all along. The label/description must say so;
the key and predicate stay byte-identical.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis as fr
import pytest

from app.autopilot import compliance as comp

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)

ALL_KEYS = {
    "recall_before_work", "write_as_you_go", "recall_visibly_used",
    "ctx_working_state", "declared_predictions", "outcome_bearing",
    # grade_self_reported (PR4 D2, adoption row) is additive — a NEW row,
    # not a change to any round-1/round-2 key. It carries instruction text
    # like the other non-derived rows, so it belongs in TEXT_KEYS below too.
    "grade_self_reported",
}
TEXT_KEYS = ALL_KEYS - {"recall_visibly_used", "outcome_bearing"}

CURRENT = {"rendered": "aaa111bbb222", "expected": "aaa111bbb222"}
STALE = {"rendered": "0ldhash00000", "expected": "aaa111bbb222"}
ABSENT = {"rendered": "absent", "expected": "aaa111bbb222"}
GATEWAY = {"gateway": "ccc333ddd444"}


def rec(sid, *, runtime=None, client_version=None, instructions=None,
        days_ago=1.0, **metrics):
    r = {
        "session_id": sid,
        "created_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "trigger": "session_complete",
        "metrics": metrics,
    }
    if runtime is not None:
        r["runtime"] = runtime
    if client_version is not None:
        r["client_version"] = client_version
    if instructions is not None:
        r["instructions"] = instructions
    return r


def rows_by_key(evals):
    return {r["key"]: r for r in comp.build_rows(evals)}


def exposure(evals, key):
    return rows_by_key(evals)[key]["exposure"]


# ------------------------------------------------- the corrected ctx row --

def test_ctx_row_label_and_description_state_the_correction():
    row = rows_by_key([])["ctx_working_state"]
    assert row["instruction"] == "Working state captured (agent plan/decision)"
    desc = row["predicate"]
    assert desc.startswith("context_snapshot_count > 0"), (
        "the predicate itself is frozen; only its description sharpened"
    )
    assert "plan|decision" in desc
    assert "context_ref" in desc
    assert "scratch" in desc, "must say the stop-hook's scratch snapshots never count"
    # The reversed cb36570 claim must be gone in both its phrasings.
    assert "also satisfies" not in desc
    assert "stop-hook snapshot also" not in desc


def test_ctx_predicate_behavior_is_byte_identical():
    evals = [rec("hit", context_snapshot_count=2),
             rec("miss", context_snapshot_count=0),
             rec("junk", context_snapshot_count="three")]
    row = rows_by_key(evals)["ctx_working_state"]
    assert row["hits"] == 1
    assert row["total"] == 3


def test_headline_and_keys_unchanged_by_round_2():
    evals = [
        rec("s1", runtime="claude", instructions=dict(CURRENT),
            memory_read_count=2),
        rec("s2", memory_read_count=0),
    ]
    rows = rows_by_key(evals)
    assert set(rows) == ALL_KEYS
    row = rows["recall_before_work"]
    assert row["hits"] == 1
    assert row["total"] == 2, "headline denominator stays ALL sessions"
    assert row["rate"] == 0.5


# ------------------------------------------------------------ by_runtime --

def test_by_runtime_slices_the_same_predicate():
    evals = [
        rec("c1", runtime="claude", memory_read_count=2),
        rec("c2", runtime="claude", memory_read_count=0),
        rec("k1", runtime="codex", memory_read_count=1),
        rec("old", memory_read_count=1),  # pre-0.1.41: no runtime at all
    ]
    row = rows_by_key(evals)["recall_before_work"]
    assert row["by_runtime"] == {
        "claude": {"hits": 1, "total": 2},
        "codex": {"hits": 1, "total": 1},
        "unattributed": {"hits": 1, "total": 1},
    }


def test_by_runtime_is_always_present_even_all_unattributed():
    rows = rows_by_key([rec("s1", memory_read_count=1)])
    for key, row in rows.items():
        assert row["by_runtime"] == {"unattributed": {"hits": row["hits"],
                                                      "total": 1}}, key


def test_malformed_runtime_lands_in_unattributed():
    evals = [rec("junk", runtime=42, memory_read_count=1),
             rec("empty", runtime="", memory_read_count=0)]
    row = rows_by_key(evals)["recall_before_work"]
    assert row["by_runtime"] == {"unattributed": {"hits": 1, "total": 2}}


# -------------------------------------------------------------- exposure --

def test_exposure_is_null_for_the_two_derived_rows():
    evals = [rec("s1", runtime="claude", client_version="0.1.41",
                 instructions={**CURRENT, **GATEWAY}, recall_used_rate=0.5,
                 outcome_event_count=3)]
    rows = rows_by_key(evals)
    assert rows["recall_visibly_used"]["exposure"] is None
    assert rows["outcome_bearing"]["exposure"] is None
    for key in TEXT_KEYS:
        assert rows[key]["exposure"] is not None, key


def test_rendered_verified_current_is_exposed():
    ev = [rec("s1", instructions=dict(CURRENT), memory_read_count=1)]
    assert exposure(ev, "recall_before_work") == {
        "exposed": 1, "not_exposed": 0, "unknown": 0,
        "exposed_hits": 1, "exposed_rate": 1.0,
    }


def test_stale_or_absent_block_with_no_gateway_is_not_exposed():
    ev = [rec("stale", instructions=dict(STALE)),
          rec("absent", instructions=dict(ABSENT))]
    assert exposure(ev, "recall_before_work") == {
        "exposed": 0, "not_exposed": 2, "unknown": 0,
        "exposed_hits": 0, "exposed_rate": None,
    }


def test_gateway_hash_alone_delivers_the_pre_attribution_keys():
    """The three old text keys predate attribution: the handshake reaching
    the session is a qualifying artifact with no version gate."""
    ev = [rec("s1", instructions=dict(GATEWAY), memory_write_count=1)]
    for key in ("recall_before_work", "write_as_you_go"):
        assert exposure(ev, key)["exposed"] == 1, key


def test_unattributed_is_unknown_never_not_exposed():
    ev = [
        rec("round1"),                          # no attribution at all
        rec("empty", instructions={}),          # dict arrived, no hashes
        rec("junk", instructions="not-a-dict"),  # malformed
        rec("nulls", instructions={"rendered": None, "gateway": ""}),
    ]
    for key in TEXT_KEYS:
        exp = exposure(ev, key)
        assert exp["unknown"] == 4, key
        assert exp["not_exposed"] == 0, key


def test_expected_only_receipt_is_unknown_not_not_exposed():
    """`expected` alone is the wheel's self-declared hash — a claim about the
    client, not evidence any artifact reached the session (reachable only via
    in-transit header stripping; an intact 0.1.41 client sends all five
    headers together). Negative evidence must not be asserted from absent
    evidence: unknown, never not_exposed (external review 2026-08-12). The
    literal "absent" rendered value is different — an affirmative report that
    no block was on disk — and stays classifiable."""
    ev = [rec("s1", instructions={"expected": "aaa111bbb222"})]
    for key in TEXT_KEYS:
        exp = exposure(ev, key)
        assert exp["unknown"] == 1, key
        assert exp["not_exposed"] == 0, key


def test_declared_predictions_rendered_channel_gates_at_0_1_40():
    ev_below = [rec("s1", client_version="0.1.39", instructions=dict(CURRENT))]
    ev_at = [rec("s2", client_version="0.1.40", instructions=dict(CURRENT))]
    assert exposure(ev_below, "declared_predictions")["not_exposed"] == 1, (
        "a verified current block below the introduction version never "
        "carried the action_before paragraph"
    )
    assert exposure(ev_at, "declared_predictions")["exposed"] == 1
    # The same session IS exposed for a key that predates attribution.
    assert exposure(ev_below, "recall_before_work")["exposed"] == 1


def test_declared_predictions_gateway_channel_gates_at_0_1_41():
    """Correction 2: the handshake channel only exists from 0.1.41 — a
    0.1.40 gateway hash with a stale block is not_exposed for this key."""
    ev_40 = [rec("s1", client_version="0.1.40",
                 instructions={**STALE, **GATEWAY})]
    ev_41 = [rec("s2", client_version="0.1.41",
                 instructions={**STALE, **GATEWAY})]
    assert exposure(ev_40, "declared_predictions")["not_exposed"] == 1
    assert exposure(ev_41, "declared_predictions")["exposed"] == 1


def test_unparseable_version_is_unknown_for_the_gated_key_only():
    ev = [rec("s1", client_version="banana.split",
              instructions={**CURRENT, **GATEWAY})]
    assert exposure(ev, "declared_predictions")["unknown"] == 1
    assert exposure(ev, "recall_before_work")["exposed"] == 1, (
        "the ungated keys need no version, so the same session classifies"
    )
    ev_missing = [rec("s2", instructions=dict(CURRENT))]
    assert exposure(ev_missing, "declared_predictions")["unknown"] == 1


def test_version_comparison_is_numeric_not_lexicographic():
    """"0.1.9" < "0.1.40" numerically but not as strings — the tuple parse
    is what keeps a 0.1.9 session below the gate."""
    ev = [rec("s1", client_version="0.1.9", instructions=dict(CURRENT)),
          rec("s2", client_version="0.2.0", instructions=dict(CURRENT))]
    exp = exposure(ev, "declared_predictions")
    assert exp["not_exposed"] == 1
    assert exp["exposed"] == 1


def test_exposed_rate_is_over_exposed_sessions_only():
    ev = [
        rec("e-hit", instructions=dict(CURRENT), brier_score=0.1,
            memory_read_count=1),
        rec("e-miss", instructions=dict(CURRENT), memory_read_count=0),
        rec("ne-hit", instructions=dict(STALE), memory_read_count=5),
        rec("unk", memory_read_count=5),
    ]
    exp = exposure(ev, "recall_before_work")
    assert exp == {"exposed": 2, "not_exposed": 1, "unknown": 1,
                   "exposed_hits": 1, "exposed_rate": 0.5}
    row = rows_by_key(ev)["recall_before_work"]
    assert row["hits"] == 3 and row["total"] == 4, (
        "the headline still counts every session, exposed or not"
    )


def test_zero_sessions_yield_empty_slices_not_errors():
    rows = rows_by_key([])
    for key, row in rows.items():
        assert row["by_runtime"] == {}, key
        if key in TEXT_KEYS:
            assert row["exposure"] == {
                "exposed": 0, "not_exposed": 0, "unknown": 0,
                "exposed_hits": 0, "exposed_rate": None,
            }, key
        else:
            assert row["exposure"] is None, key


# --------------------------------------------- endpoint-level via fakeredis --

@pytest.mark.asyncio
async def test_build_compliance_survives_malformed_attribution():
    """The _num() per-record isolation discipline extends to the round-2
    fields: poisoned attribution must degrade to unattributed/unknown, never
    500 the endpoint."""
    r = fr.FakeRedis(decode_responses=True)
    await r.set("rp:eval:ok", json.dumps(rec(
        "ok", runtime="claude", client_version="0.1.41",
        instructions={**CURRENT, **GATEWAY}, memory_read_count=1)))
    await r.set("rp:eval:poison", json.dumps(rec(
        "poison", runtime={"x": 1}, client_version=41,
        instructions=["not", "a", "dict"], memory_read_count=1)))

    body = await comp.build_compliance(r)

    assert body["sessions_evaluated"] == 2
    row = {x["key"]: x for x in body["instructions"]}["recall_before_work"]
    assert row["by_runtime"] == {
        "claude": {"hits": 1, "total": 1},
        "unattributed": {"hits": 1, "total": 1},
    }
    assert row["exposure"]["exposed"] == 1
    assert row["exposure"]["unknown"] == 1


@pytest.mark.asyncio
async def test_round_1_records_still_score_and_read_unattributed():
    """Nothing backfills: a stored round-1 eval (no attribution fields at
    all) keeps scoring exactly as before and lands wholly in the disclosed
    buckets."""
    r = fr.FakeRedis(decode_responses=True)
    await r.set("rp:eval:old", json.dumps({
        "session_id": "old",
        "created_at": (NOW - timedelta(days=5)).isoformat(),
        "trigger": "session_complete",
        "metrics": {"memory_read_count": 2.0, "brier_score": 0.1},
    }))

    body = await comp.build_compliance(r)

    rows = {x["key"]: x for x in body["instructions"]}
    assert rows["recall_before_work"]["hits"] == 1
    assert rows["recall_before_work"]["by_runtime"] == {
        "unattributed": {"hits": 1, "total": 1}}
    assert rows["declared_predictions"]["exposure"]["unknown"] == 1


@pytest.mark.asyncio
async def test_notes_disclose_attribution_start_and_exposure_semantics():
    r = fr.FakeRedis(decode_responses=True)
    body = await comp.build_compliance(r)
    notes = " ".join(body["notes"])
    assert "hook-automated" not in notes, (
        "Correction 1: the hook writes scratch, which never carries a "
        "context_ref — the clause was false and must be gone"
    )
    assert "0.1.41" in notes
    assert "backfill" in notes
    assert "not_exposed" in notes and "unknown" in notes
    assert any("BEHAVIOR" in n for n in body["notes"]), (
        "the round-1 compliance!=quality caveat must survive the rewrite"
    )
