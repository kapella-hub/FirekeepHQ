"""Outcome truth (PR1) — Bridge storage layer.

Real-Redis-semantics tests for complete_session's WATCH/MULTI CAS loop. The
suite's generic `mock_redis` fixture (conftest.py) cannot model WATCH: an
unconfigured `await AsyncMock().hget(...)` is truthy, not the real value, so
these tests use two fakeredis clients sharing one `fakeredis.FakeServer()` to
get REAL Redis WATCH/MULTI semantics instead.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
import pytest_asyncio
import redis

from app.config import Settings
from app.distill_worker import QUEUE_KEY
from app.session import SessionManager

SID = "sess-v9"
SKEY = f"nb:session:{SID}"


@pytest_asyncio.fixture
async def real_manager():
    server = fakeredis.FakeServer()
    r = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    mgr = SessionManager(r, Settings())
    yield mgr, r, server
    await r.aclose()


async def _seed(r, *, owner="member-alice", agent="alice-agent",
                 status="active", grade=""):
    mapping = {
        "goal": "ship truth", "status": status, "agent_id": agent,
        "owner_member": owner, "tags": "[]", "task_result": grade,
        "task_result_source": "self_reported" if grade else "",
    }
    await r.hset(SKEY, mapping=mapping)
    await r.set(f"nb:active:{agent}", SID)


@pytest.mark.asyncio
async def test_enumerate_takeover_grade_attack_has_zero_terminal_side_effects(
    real_manager,
):
    """Mallory knows SID + Alice's label (ctx_list_sessions exposes both),
    tries the formerly-legal takeover, then tries completing with that known
    label. Neither half may mutate the bound session."""
    mgr, r, _ = real_manager
    await _seed(r)
    before = await r.hgetall(SKEY)
    with patch("app.session._replay_emit", new=AsyncMock()) as emit:
        with pytest.raises(ValueError, match="verified owner"):
            await mgr.resume_session(
                SID, agent_id="mallory-agent", takeover=True,
                verified_member="member-mallory")
        with pytest.raises(ValueError, match="verified owner"):
            await mgr.complete_session(
                SID, outcome="pwned", agent_id="alice-agent",
                task_result="success", verified_member="member-mallory")
    assert await r.hgetall(SKEY) == before
    assert await r.get("nb:active:alice-agent") == SID
    assert await r.xlen(QUEUE_KEY) == 0
    emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_grade_is_stored_emitted_and_returned(real_manager):
    mgr, r, _ = real_manager
    await _seed(r)
    with patch("app.session._replay_emit", new=AsyncMock()) as emit:
        result = await mgr.complete_session(
            SID, outcome="done", agent_id="alice-agent",
            task_result="success", task_evidence=["pytest passed"],
            verified_member="member-alice")
    stored = await r.hgetall(SKEY)
    assert stored["task_result"] == "success"
    assert stored["task_result_source"] == "self_reported"
    assert json.loads(stored["task_evidence"]) == ["pytest passed"]
    assert result["task_result"] == "success"
    assert result["task_result_source"] == "self_reported"
    assert emit.await_args.kwargs["payload"]["task_result"] == "success"
    assert (await mgr.get_session_data(SID))["task_evidence"] == ["pytest passed"]


@pytest.mark.asyncio
async def test_existing_grade_is_authoritative_on_regrade(real_manager):
    mgr, r, _ = real_manager
    await _seed(r, status="completed", grade="success")
    await r.hset(SKEY, "task_evidence", '["original"]')
    with patch("app.session._replay_emit", new=AsyncMock()) as emit:
        result = await mgr.complete_session(
            SID, outcome="again", agent_id="alice-agent",
            task_result="failure", verified_member="member-alice")
    assert (await r.hget(SKEY, "task_result")) == "success"
    assert result["task_result"] == "success"                 # authority, not attempt
    assert result["task_result_dropped"] == "session already has a stored grade"
    assert emit.await_args.kwargs["payload"]["task_result"] == "success"
    assert await r.hget(SKEY, "task_evidence") == '["original"]'


@pytest.mark.asyncio
async def test_ungraded_recompletion_cannot_erase_the_pair(real_manager):
    mgr, r, _ = real_manager
    await _seed(r, status="completed", grade="failure")
    with patch("app.session._replay_emit", new=AsyncMock()) as emit:
        result = await mgr.complete_session(
            SID, outcome="reworded", agent_id="alice-agent",
            task_result=None, verified_member="member-alice")
    assert await r.hget(SKEY, "task_result") == "failure"
    assert await r.hget(SKEY, "task_result_source") == "self_reported"
    assert result["task_result"] == "failure"
    assert emit.await_args.kwargs["payload"]["task_result"] == "failure"


@pytest.mark.asyncio
async def test_sourceless_hash_grade_is_not_fabricated_as_authoritative(real_manager):
    mgr, r, _ = real_manager
    await _seed(r, status="completed", grade="success")
    await r.hset(SKEY, "task_result_source", "")
    with patch("app.session._replay_emit", new=AsyncMock()) as emit:
        result = await mgr.complete_session(
            SID, agent_id="alice-agent", task_result=None,
            verified_member="member-alice")
    assert result["task_result"] is None
    assert result["task_result_source"] is None
    assert "task_result" not in emit.await_args.kwargs["payload"]


@pytest.mark.asyncio
async def test_legacy_session_completes_but_never_grades(real_manager):
    mgr, r, _ = real_manager
    # Model partial-deploy/corrupt state too: recognized strings in an unbound
    # hash are not authority and must not escape through the terminal event.
    await _seed(r, owner="", grade="success")
    with patch("app.session._replay_emit", new=AsyncMock()) as emit:
        result = await mgr.complete_session(
            SID, agent_id="alice-agent", task_result="failure",
            verified_member="member-alice")
    assert await r.hget(SKEY, "task_result") == "success"  # preserved, never trusted
    assert result["task_result"] is None
    assert result["task_result_source"] is None
    assert "pre-upgrade" in result["task_result_dropped"]
    assert "task_result" not in emit.await_args.kwargs["payload"]


@pytest.mark.asyncio
async def test_conflicting_completions_return_one_authoritative_winner(real_manager):
    mgr1, r1, server = real_manager
    r2 = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    mgr2 = SessionManager(r2, Settings())
    await _seed(r1)
    ready = 0
    ready_lock = asyncio.Lock()
    release = asyncio.Event()

    def gate_first_execute(client):
        nonlocal ready
        real_pipeline = client.pipeline
        first = {"pending": True}

        def factory(*args, **kwargs):
            pipe = real_pipeline(*args, **kwargs)
            real_execute = pipe.execute

            async def execute(*ea, **ek):
                nonlocal ready
                if first["pending"]:
                    first["pending"] = False
                    async with ready_lock:
                        ready += 1
                        if ready == 2:
                            release.set()
                    await asyncio.wait_for(release.wait(), timeout=2)
                return await real_execute(*ea, **ek)

            pipe.execute = execute
            return pipe

        client.pipeline = factory

    gate_first_execute(r1)
    gate_first_execute(r2)
    with patch("app.session._replay_emit", new=AsyncMock()):
        a, b = await asyncio.gather(
            mgr1.complete_session(
                SID, agent_id="alice-agent", task_result="success",
                verified_member="member-alice"),
            mgr2.complete_session(
                SID, agent_id="alice-agent", task_result="failure",
                verified_member="member-alice"),
        )
    winner = await r1.hget(SKEY, "task_result")
    assert winner in {"success", "failure"}
    assert a["task_result"] == b["task_result"] == winner
    await r2.aclose()


@pytest.mark.asyncio
async def test_cas_exhaustion_commits_and_emits_nothing(real_manager):
    mgr, r, _ = real_manager
    await _seed(r)
    before = await r.hgetall(SKEY)
    real_pipeline = r.pipeline

    def always_stale(*args, **kwargs):
        pipe = real_pipeline(*args, **kwargs)

        async def execute(*_args, **_kwargs):
            raise redis.WatchError("forced contention")

        pipe.execute = execute
        return pipe

    r.pipeline = always_stale
    with patch("app.session._replay_emit", new=AsyncMock()) as emit:
        with pytest.raises(RuntimeError, match="contended repeatedly"):
            await mgr.complete_session(
                SID, agent_id="alice-agent", task_result="success",
                verified_member="member-alice")
    assert await r.hgetall(SKEY) == before
    assert await r.get("nb:active:alice-agent") == SID
    assert await r.xlen(QUEUE_KEY) == 0
    emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_does_not_delete_a_concurrently_repointed_active_key(
    real_manager,
):
    """The pointer is part of the watched decision. Without WATCH(active_key),
    completion reads SID, a concurrent start/resume repoints it, and the stale
    transaction deletes the NEW session's pointer."""
    mgr, r, server = real_manager
    await _seed(r)
    competitor = fakeredis.aioredis.FakeRedis(
        server=server, decode_responses=True)
    active_key = "nb:active:alice-agent"
    real_pipeline = r.pipeline
    state = {"repointed": False}

    def repoint_before_first_exec(*args, **kwargs):
        pipe = real_pipeline(*args, **kwargs)
        real_execute = pipe.execute

        async def execute(*ea, **ek):
            if not state["repointed"]:
                state["repointed"] = True
                await competitor.set(active_key, "new-session")
            # Correct code WATCHed active_key, so this EXEC raises WatchError;
            # the retry re-reads "new-session" and omits the delete. Broken
            # code executes its stale DEL and makes the assertion below fail.
            return await real_execute(*ea, **ek)

        pipe.execute = execute
        return pipe

    r.pipeline = repoint_before_first_exec
    try:
        with patch("app.session._replay_emit", new=AsyncMock()):
            result = await mgr.complete_session(
                SID, agent_id="alice-agent", task_result="success",
                verified_member="member-alice")
        assert result["task_result"] == "success"
        assert state["repointed"] is True
        assert await r.get(active_key) == "new-session"
    finally:
        await competitor.aclose()
