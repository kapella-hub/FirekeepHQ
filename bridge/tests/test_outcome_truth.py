"""The session_end event tells the truth about the task (spec D1-D3, D8, D13)."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app import mcp_server


def _mgr(complete_result=None, complete_error=None):
    mgr = AsyncMock()
    default = {
        "status": "completed", "session_id": "s1",
        "task_result": None, "task_result_source": None,
    }
    mgr.complete_session = AsyncMock(
        return_value=complete_result or default, side_effect=complete_error)
    return mgr


async def _complete(member="member-alice", mgr=None, **kwargs):
    mgr = mgr or _mgr()
    emit = AsyncMock()
    trigger = AsyncMock(return_value=True)
    skill = AsyncMock(return_value=True)
    with patch("app.mcp_server._get_manager", new=AsyncMock(return_value=mgr)), \
         patch("app.mcp_server.get_http_headers", return_value={}), \
         patch("app.mcp_server._verified_member_id", return_value=member), \
         patch("app.mcp_server._trigger_eval", new=trigger), \
         patch("app.mcp_server._trigger_skill_evaluate", new=skill), \
         patch("app.mcp_server._replay_emit", new=emit):
        result = await mcp_server.ctx_complete_session(**kwargs)
    tasks = list(mcp_server._background_tasks)
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return result, emit, mgr, trigger, skill


@pytest.mark.asyncio
async def test_graded_completion_threads_principal_and_emits_the_grade():
    authoritative = {
        "status": "completed", "session_id": "s1",
        "task_result": "failure", "task_result_source": "self_reported",
    }
    result, emit, mgr, trigger, _ = await _complete(
        mgr=_mgr(authoritative), outcome="done", task_result="failure",
        task_evidence=["3 tests red"])
    assert mgr.complete_session.await_args.kwargs["verified_member"] == "member-alice"
    assert mgr.complete_session.await_args.kwargs["task_result"] == "failure"
    call = next(c for c in emit.await_args_list if c.args[0] == "session_end")
    assert call.kwargs["outcome"] == "failure"
    assert call.args[3]["task_result"] == "failure"
    assert call.args[3]["task_result_source"] == "self_reported"
    assert trigger.call_args.kwargs.get("task_result") == "failure"
    assert result["task_result"] == "failure"


@pytest.mark.asyncio
async def test_losing_regrade_emits_the_authoritative_existing_grade():
    mgr = _mgr({
        "status": "completed", "session_id": "s1",
        "task_result": "success", "task_result_source": "self_reported",
        "task_result_dropped": "session already has a stored grade",
    })
    result, emit, _, trigger, _ = await _complete(
        mgr=mgr, outcome="x", task_result="failure")
    call = next(c for c in emit.await_args_list if c.args[0] == "session_end")
    assert call.kwargs["outcome"] == "success"
    assert call.args[3]["task_result"] == "success"
    assert trigger.call_args.kwargs["task_result"] == "success"
    assert result["task_result"] == "success"
    assert "task_result_dropped" in result


@pytest.mark.asyncio
async def test_ungraded_completion_emits_no_outcome():
    result, emit, _, trigger, _ = await _complete(outcome="done")
    call = next(c for c in emit.await_args_list if c.args[0] == "session_end")
    assert call.kwargs["outcome"] is None
    assert "task_result" not in call.args[3]
    assert result["task_result"] is None


@pytest.mark.asyncio
async def test_sourceless_manager_pair_is_not_forwarded():
    mgr = _mgr({
        "status": "completed", "session_id": "s1",
        "task_result": "success", "task_result_source": None,
    })
    result, emit, _, trigger, _ = await _complete(mgr=mgr, outcome="done")
    call = next(c for c in emit.await_args_list if c.args[0] == "session_end")
    assert call.kwargs["outcome"] is None
    assert "task_result" not in call.args[3]
    assert trigger.call_args.kwargs["task_result"] is None
    assert result["task_result"] is None


@pytest.mark.asyncio
async def test_invalid_grade_string_is_coerced_not_fatal():
    result, emit, mgr, _, _ = await _complete(
        outcome="done", task_result="great success")
    assert mgr.complete_session.await_args.kwargs["task_result"] is None
    assert result["task_result"] is None
    assert "task_result_note" in result


@pytest.mark.asyncio
async def test_principal_refusal_has_no_tool_side_effects():
    mgr = _mgr(complete_error=ValueError("different verified owner"))
    result, emit, _, trigger, skill = await _complete(
        member="member-mallory", mgr=mgr, outcome="x", task_result="success")
    assert "different verified owner" in result["error"]
    emit.assert_not_awaited()
    trigger.assert_not_awaited()
    skill.assert_not_awaited()


@pytest.mark.parametrize("explicit_sid", [True, False])
@pytest.mark.asyncio
async def test_bound_abandon_refuses_forged_label_before_side_effects(explicit_sid):
    """D13: ctx_list_sessions exposes SID + label; neither an explicit SID nor
    the legacy active-pointer fallback may turn that label into abandon authority."""
    mgr = AsyncMock()
    mgr.get_active_session_id = AsyncMock(return_value="s1")
    mgr.get_session_data = AsyncMock(return_value={
        "session_id": "s1", "agent_id": "alice-agent",
        "owner_member": "member-alice",
    })
    after = AsyncMock()
    with patch("app.mcp_server._get_manager", new=AsyncMock(return_value=mgr)), \
         patch("app.mcp_server._header_session_id", return_value=None), \
         patch("app.mcp_server._verified_member_id", return_value="member-mallory"), \
         patch("app.mcp_server.after_abandon", new=after):
        result = await mcp_server.ctx_abandon_session(
            session_id="s1" if explicit_sid else None,
            agent_id="alice-agent")
    assert "different verified owner" in result["error"]
    mgr.get_session_data.assert_awaited_once_with("s1")
    if explicit_sid:
        mgr.get_active_session_id.assert_not_awaited()
    else:
        mgr.get_active_session_id.assert_awaited_once_with("alice-agent")
    mgr.abandon_session.assert_not_awaited()
    after.assert_not_awaited()


@pytest.mark.asyncio
async def test_evidence_is_trimmed_and_needs_a_grade():
    _, _, mgr, _, _ = await _complete(
        outcome="done", task_result="success",
        task_evidence=["x" * 400] + [f"e{i}" for i in range(11)])
    ev = mgr.complete_session.await_args.kwargs["task_evidence"]
    assert len(ev) == 10 and len(ev[0]) == 300
    _, _, mgr2, _, _ = await _complete(
        outcome="done", task_evidence=["orphan"])
    assert mgr2.complete_session.await_args.kwargs["task_evidence"] == []


def test_verified_member_id_parses_a_scope_identity(monkeypatch):
    """Unit test of the HELPER only — the real propagation is pinned by the
    integration test below."""
    class _Req:
        scope = {"state": {"identity": {
            "workspace_id": "w", "member_id": "member-alice",
            "credential_id": "c", "scopes": []}}}
    monkeypatch.setattr("app.mcp_server.get_http_request", lambda: _Req(),
                        raising=False)
    assert mcp_server._verified_member_id() == "member-alice"


def test_verified_member_id_is_none_outside_a_request(monkeypatch):
    def _boom():
        raise RuntimeError("no active request")
    monkeypatch.setattr("app.mcp_server.get_http_request", _boom, raising=False)
    assert mcp_server._verified_member_id() is None


@pytest.mark.asyncio
async def test_verified_member_propagates_through_middleware_and_fastmcp(monkeypatch):
    """D13 INTEGRATION: the REAL path — accepted credential → FirekeepKeyAuthMiddleware
    → scope['state']['identity'] → get_http_request() inside the tool — over
    lifespan-managed ASGI (the fake-scope test above proves only the helper's
    parsing). The raw-ASGI path was probe-confirmed on fastmcp 3.1.1."""
    import httpx
    from auth.asgi import build_auth_middleware
    from auth.config import AuthSettings

    # 1. Auth ON, and validate_key resolves exactly one test credential. The ASGI
    #    middleware imports validate_key INTO auth.asgi (auth/asgi.py:25:
    #    `from auth.keys import ... validate_key`), so the patch target is the
    #    NAME IN auth.asgi, not auth.middleware (the binding is verified at
    #    auth/asgi.py:25 and this raising=True patch will fail on drift).
    async def _fake_validate(api_key, redis_client=None):
        if api_key == "nxs_test-key":
            return {"workspace_id": "w", "member_id": "member-alice",
                    "credential_id": "c", "scopes": ["session:write"],
                    "authenticated": True}
        return None
    monkeypatch.setattr("auth.asgi.validate_key", _fake_validate, raising=True)
    # 2. Stub the lifespan workers and the tool's side effects; capture the manager.
    async def _noop():
        return None
    monkeypatch.setattr("app.distill_worker.distill_worker_loop", _noop)
    monkeypatch.setattr("app.reaper.reaper_loop", _noop)
    monkeypatch.setattr("app.distill_worker.close_distiller", _noop)
    mgr = _mgr()
    monkeypatch.setattr("app.mcp_server._get_manager", AsyncMock(return_value=mgr))
    monkeypatch.setattr("app.mcp_server._replay_emit", AsyncMock())
    monkeypatch.setattr("app.mcp_server._trigger_eval", AsyncMock(return_value=True))
    monkeypatch.setattr("app.mcp_server._trigger_skill_evaluate",
                        AsyncMock(return_value=True))

    # 3. Drive a raw streamable-HTTP tools/call through the real middleware.
    from app.mcp_server import mcp
    app = mcp.http_app(
        middleware=build_auth_middleware(
            AuthSettings(ENABLED=True, REDIS_URL="redis://unused/7")),
        stateless_http=True,
    )
    headers = {
        "X-API-Key": "nxs_test-key",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "ctx_complete_session",
                   "arguments": {"outcome": "done"}},
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/mcp", json=body, headers=headers)

    # ctx_complete_session detaches a background eval trigger (SP0 D5); left
    # uncancelled it outlives this test's event loop and corrupts whichever
    # later test next touches mcp_server._background_tasks (observed:
    # "RuntimeError: Event loop is closed" in an unrelated test) — so cleanup
    # runs in `finally`, even if an assertion below fails.
    try:
        assert response.status_code == 200
        assert mgr.complete_session.await_args.kwargs["verified_member"] == "member-alice"
    finally:
        tasks = list(mcp_server._background_tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _stub_lifespan(monkeypatch):
    async def _noop():
        return None
    monkeypatch.setattr("app.distill_worker.distill_worker_loop", _noop)
    monkeypatch.setattr("app.reaper.reaper_loop", _noop)
    monkeypatch.setattr("app.distill_worker.close_distiller", _noop)


@pytest.mark.asyncio
async def test_wire_wrong_typed_grade_is_rejected_before_the_tool_runs(monkeypatch):
    """D1: FastMCP validates annotations pre-function — a numeric task_result
    is a recoverable client error, the session untouched (fastmcp 3.1.1)."""
    _stub_lifespan(monkeypatch)
    mgr = _mgr()
    monkeypatch.setattr("app.mcp_server._get_manager", AsyncMock(return_value=mgr))
    from fastmcp import Client
    from app.mcp_server import mcp
    async with Client(mcp) as client:
        res = await client.call_tool_mcp("ctx_complete_session", {"task_result": 123})
        assert res.isError
    mgr.complete_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_wire_invalid_grade_string_reaches_coercion(monkeypatch):
    """A wrong VALUE (valid type) passes validation and is coerced in-body."""
    _stub_lifespan(monkeypatch)
    mgr = _mgr()
    monkeypatch.setattr("app.mcp_server._get_manager", AsyncMock(return_value=mgr))
    monkeypatch.setattr("app.mcp_server._verified_member_id", lambda: "member-alice")
    monkeypatch.setattr("app.mcp_server._replay_emit", AsyncMock())
    monkeypatch.setattr("app.mcp_server._trigger_eval", AsyncMock(return_value=True))
    monkeypatch.setattr("app.mcp_server._trigger_skill_evaluate",
                        AsyncMock(return_value=True))
    from fastmcp import Client
    from app.mcp_server import mcp
    async with Client(mcp) as client:
        res = await client.call_tool_mcp(
            "ctx_complete_session", {"outcome": "done", "task_result": "great success"})
        assert not res.isError
    mgr.complete_session.assert_awaited_once()
    tasks = list(mcp_server._background_tasks)
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_start_binds_the_verified_member(monkeypatch):
    mgr = AsyncMock()
    mgr.start_session.return_value = {"session_id": "s1"}
    monkeypatch.setattr(mcp_server, "_get_manager", AsyncMock(return_value=mgr))
    monkeypatch.setattr(mcp_server, "_verified_member_id",
                        lambda: "member-alice")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    monkeypatch.setattr(mcp_server, "_replay_emit", AsyncMock())
    monkeypatch.setattr(mcp_server, "assemble_prior_art",
                        AsyncMock(return_value={}))
    await mcp_server.ctx_start_session("goal")
    assert mgr.start_session.await_args.kwargs["owner_member"] == "member-alice"


@pytest.mark.asyncio
async def test_resume_threads_verified_member(monkeypatch):
    mgr = AsyncMock()
    mgr.get_session_data.return_value = {"goal": "g", "status": "active"}
    monkeypatch.setattr(mcp_server, "_get_manager", AsyncMock(return_value=mgr))
    monkeypatch.setattr(mcp_server, "_verified_member_id",
                        lambda: "member-alice")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    monkeypatch.setattr(mcp_server, "assemble_shadow", lambda data: "shadow")
    await mcp_server.ctx_resume_session("s1", takeover=True)
    assert mgr.resume_session.await_args.kwargs["verified_member"] == "member-alice"


@pytest.mark.asyncio
async def test_refused_resume_does_not_read_shadow(monkeypatch):
    mgr = AsyncMock()
    mgr.resume_session.side_effect = ValueError("different verified owner")
    monkeypatch.setattr(mcp_server, "_get_manager", AsyncMock(return_value=mgr))
    monkeypatch.setattr(mcp_server, "_verified_member_id",
                        lambda: "member-mallory")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    result = await mcp_server.ctx_resume_session("s1", takeover=True)
    assert "different verified owner" in result["error"]
    mgr.get_session_data.assert_not_awaited()
