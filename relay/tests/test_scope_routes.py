"""Tests for FirekeepScope REST route handlers (SP2 Phase A Task 5)."""

import json

import pytest
from starlette.requests import Request

import app.routes as routes_mod
from app.routes import (
    handle_post_scope_session, handle_post_scope_screen,
    handle_get_scope_sessions, handle_get_scope_session,
    handle_post_scope_answer, handle_get_scope_events,
    route_post_scope_answer,
)


def _make_request(method: str, path: str, *, path_params: dict | None = None,
                   body: dict | None = None) -> Request:
    """Minimal Starlette Request builder for route-wrapper-level tests
    (auth disabled by default in tests, so no identity is needed —
    see relay/tests/test_scope_routes_auth.py for the auth-gated variant)."""
    body_bytes = json.dumps(body).encode("utf-8") if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "path_params": path_params or {},
        "query_string": b"",
        "state": {},
    }
    return Request(scope, receive)


class TestScopeRouteHandlers:
    @pytest.mark.asyncio
    async def test_post_scope_session_creates(self, redis):
        result = await handle_post_scope_session(redis, agent_id="a", goal="g", origin="cli")
        assert result["scope_id"].startswith("sc_")

    @pytest.mark.asyncio
    async def test_post_scope_screen_mirrors_into_existing_session(self, redis):
        session = await handle_post_scope_session(redis, agent_id="a", goal="g", origin="cli")
        screen = await handle_post_scope_screen(redis, session["scope_id"], {
            "kind": "questions", "mode": "gating", "title": "t", "questions": [],
        })
        assert screen["screen_id"] == f"{session['scope_id']}-1"

    @pytest.mark.asyncio
    async def test_post_scope_screen_unknown_session_raises(self, redis):
        with pytest.raises(ValueError):
            await handle_post_scope_screen(redis, "sc_missing", {"kind": "questions", "mode": "gating", "title": "t", "questions": []})

    @pytest.mark.asyncio
    async def test_get_scope_sessions_lists(self, redis):
        await handle_post_scope_session(redis, agent_id="a", goal="g", origin="cli")
        result = await handle_get_scope_sessions(redis, status="active")
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_get_scope_session_includes_screens(self, redis):
        session = await handle_post_scope_session(redis, agent_id="a", goal="g", origin="cli")
        await handle_post_scope_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t", "questions": []})
        result = await handle_get_scope_session(redis, session["scope_id"])
        assert len(result["screens"]) == 1

    @pytest.mark.asyncio
    async def test_get_scope_session_missing_returns_none(self, redis):
        assert await handle_get_scope_session(redis, "sc_missing") is None

    @pytest.mark.asyncio
    async def test_post_scope_answer_resolves(self, redis):
        session = await handle_post_scope_session(redis, agent_id="a", goal="g", origin="cli")
        screen = await handle_post_scope_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t", "questions": []})
        result = await handle_post_scope_answer(redis, session["scope_id"], screen["screen_id"], answers={"q1": {"choice": "a"}}, source="dashboard")
        assert result["resolved"] is True

    @pytest.mark.asyncio
    async def test_get_scope_events(self, redis):
        session = await handle_post_scope_session(redis, agent_id="a", goal="g", origin="cli")
        await handle_post_scope_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t", "questions": []})
        result = await handle_get_scope_events(redis, session["scope_id"])
        assert result["count"] == 1


class TestPostScopeAnswerErrorCodes:
    """route_post_scope_answer maps post_answer's ValueError to either 404
    (screen not found — a missing resource) or 400 (invalid source — a
    malformed request), distinguished by message content."""

    @pytest.mark.asyncio
    async def test_invalid_source_returns_400(self, redis, monkeypatch):
        async def _fake_get_redis():
            return redis
        monkeypatch.setattr(routes_mod, "_get_redis", _fake_get_redis)

        session = await handle_post_scope_session(redis, agent_id="a", goal="g", origin="cli")
        screen = await handle_post_scope_screen(redis, session["scope_id"], {"kind": "questions", "mode": "gating", "title": "t", "questions": []})

        request = _make_request(
            "POST",
            f"/scope/sessions/{session['scope_id']}/screens/{screen['screen_id']}/answer",
            path_params={"scope_id": session["scope_id"], "screen_id": screen["screen_id"]},
            body={"answers": {}, "source": "carrier-pigeon"},
        )
        response = await route_post_scope_answer(request)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_screen_returns_404(self, redis, monkeypatch):
        async def _fake_get_redis():
            return redis
        monkeypatch.setattr(routes_mod, "_get_redis", _fake_get_redis)

        session = await handle_post_scope_session(redis, agent_id="a", goal="g", origin="cli")

        request = _make_request(
            "POST",
            f"/scope/sessions/{session['scope_id']}/screens/sc_x-99/answer",
            path_params={"scope_id": session["scope_id"], "screen_id": "sc_x-99"},
            body={"answers": {}, "source": "dashboard"},
        )
        response = await route_post_scope_answer(request)
        assert response.status_code == 404
