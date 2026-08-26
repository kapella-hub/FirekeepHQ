"""Outcome truth, PR4 D1 — the pre-registered experiment_group label.

Stamped once at session START from the verified owner_member and ridden
through the SAME attribution seam runtime/briefing_delivered already use:
session meta (beside owner_member) -> session_start replay payload ->
(cortex/tests/test_eval_attribution.py) compute_session_eval -> EvalResult.

Orthogonal to the grade: assignment never reads task_result, and happens
before any grade could exist. Must be STABLE across process restarts —
sha256, not Python's per-process-salted hash() — since PR5 depends on the
same member landing in the same arm every time.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.session import SessionManager, _experiment_group

# Precomputed once via sha256(member) % 2 so the test data is independent of
# the implementation under test. member-bob -> arm A, member-alice -> arm B.
MEMBER_A = "member-bob"
MEMBER_B = "member-alice"


class TestExperimentGroupHelper:
    def test_stable_sha256_not_python_hash(self):
        """The arm must be a sha256 hash of owner_member, NOT Python's hash()
        (salted per-process → would reshuffle every member's arm on restart,
        breaking the stickiness D1 exists to guarantee).

        Deterministic guard, not probabilistic: for EACH of many members we
        compute the expected arm independently via sha256 and assert the
        function returns it. A regression to hash() would land the wrong arm
        for ~half of these members within any single process, so matching all
        of them by coincidence has probability 2**-N — with N=64 that is a
        deterministic catch, unlike a single-member check (which a salted
        hash() passes ~50% of runs)."""
        members = [f"member-{i:04d}" for i in range(64)]
        for m in members:
            expected = "A" if int(hashlib.sha256(m.encode("utf-8")).hexdigest(), 16) % 2 == 0 else "B"
            assert _experiment_group(m) == expected, (
                f"{m}: expected sha256 arm {expected}, got {_experiment_group(m)} "
                "— a regression to Python hash() would fail this"
            )
        # Sanity: this member set actually exercises BOTH arms (else the loop
        # above could vacuously pass an all-one-arm bug).
        arms = {_experiment_group(m) for m in members}
        assert arms == {"A", "B"}

    def test_deterministic_across_repeated_calls(self):
        first = _experiment_group(MEMBER_A)
        for _ in range(5):
            assert _experiment_group(MEMBER_A) == first

    def test_assigns_different_members_across_both_arms(self):
        assert _experiment_group(MEMBER_A) == "A"
        assert _experiment_group(MEMBER_B) == "B"

    def test_empty_owner_member_is_none(self):
        """hash("") would dump every unauthenticated session into one arm —
        empty must be excluded from arms entirely, not hashed."""
        assert _experiment_group("") is None

    def test_none_owner_member_is_none(self):
        assert _experiment_group(None) is None


def test_arm_function_is_the_shared_auth_implementation():
    """PR5 D1: bridge must use auth.experiment's function, not a local copy —
    identity, not equality, so a silent re-fork fails loudly."""
    from auth.experiment import experiment_group
    from app.session import _experiment_group
    assert _experiment_group is experiment_group


class TestStartSessionPersistsExperimentGroup:
    @pytest.mark.asyncio
    async def test_field_stored_on_the_session_hash_beside_owner_member(self, mock_redis):
        mgr = SessionManager(mock_redis, Settings())
        await mgr.start_session("goal", agent_id="alice", owner_member=MEMBER_A)
        mapping = mock_redis.hset.call_args_list[0].kwargs["mapping"]
        assert mapping["owner_member"] == MEMBER_A
        assert mapping["experiment_group"] == "A"

    @pytest.mark.asyncio
    async def test_absent_owner_member_stores_empty_not_a_hashed_arm(self, mock_redis):
        mgr = SessionManager(mock_redis, Settings())
        await mgr.start_session("goal", agent_id="alice")
        mapping = mock_redis.hset.call_args_list[0].kwargs["mapping"]
        assert mapping["owner_member"] == ""
        assert mapping["experiment_group"] == ""


def _mgr(session_id: str = "abc") -> AsyncMock:
    mgr = AsyncMock()
    mgr.start_session = AsyncMock(
        return_value={"session_id": session_id, "created_at": "now"}
    )
    return mgr


class TestCtxStartSessionExperimentGroup:
    @pytest.mark.asyncio
    async def test_verified_member_carries_its_arm_onto_the_replay_payload(self):
        from app.mcp_server import ctx_start_session

        emit = AsyncMock()
        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch("app.mcp_server.get_http_headers", return_value={}),
            patch("app.mcp_server._verified_member_id", return_value=MEMBER_A),
            patch("app.mcp_server._replay_emit", emit),
        ):
            mgr = _mgr()
            mock_get.return_value = mgr
            await ctx_start_session("goal")

        # Assignment rides the SAME verified owner_member passed to
        # start_session — not a separately re-derived member.
        assert mgr.start_session.call_args.kwargs["owner_member"] == MEMBER_A
        payload = emit.call_args.args[3]
        assert payload["experiment_group"] == "A"

    @pytest.mark.asyncio
    async def test_unverified_session_carries_none_on_the_payload(self):
        from app.mcp_server import ctx_start_session

        emit = AsyncMock()
        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch("app.mcp_server.get_http_headers", return_value={}),
            patch("app.mcp_server._verified_member_id", return_value=None),
            patch("app.mcp_server._replay_emit", emit),
        ):
            mgr = _mgr()
            mock_get.return_value = mgr
            await ctx_start_session("goal")

        payload = emit.call_args.args[3]
        assert payload["experiment_group"] is None
