"""Wrapper-level scope-gate coverage for the FirekeepScope REST routes (SP2
Phase A Task 5 review fix).

relay/tests/test_scope_routes.py only exercises the extracted handle_*
functions, which never call require_scope_asgi at all — so nothing there
would catch a future edit that accidentally drops or misorders the scope
check on the Starlette route wrappers. These 6 routes are the first in
Relay to carry per-route scope logic (unlike /presence, /dm/*, /tasks/*,
/status, which rely solely on the blanket FirekeepKeyAuthMiddleware).

Same technique as bridge/tests/test_session_context_route.py's
TestPostSessionContextRouteScopeGate (SP2 Phase A Task 4 fix round): build a
real Starlette Request with a manually-constructed ASGI scope carrying
state["identity"], and call the route wrapper function directly.
"""

import json
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

import app.routes as routes_mod
from app.routes import (
    route_post_scope_session, route_post_scope_screen,
    route_get_scope_sessions, route_get_scope_session,
    route_post_scope_answer, route_get_scope_events,
)


def _make_request(method: str, path: str, *, path_params: dict | None = None,
                   identity: dict | None = None, body: dict | None = None,
                   query_string: bytes = b"") -> Request:
    """Build a real Starlette Request, same technique as
    auth/tests/test_asgi_scope_check.py and bridge's
    test_session_context_route.py, but with query_string wired too (our GET
    routes read request.query_params, which KeyErrors without it)."""
    body_bytes = json.dumps(body).encode("utf-8") if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "path_params": path_params or {},
        "query_string": query_string,
        "state": {},
    }
    if identity is not None:
        scope["state"]["identity"] = identity
    return Request(scope, receive)


@pytest.fixture
def auth_enabled(monkeypatch):
    # Patch the env-derived settings require_scope_asgi actually reads —
    # keys._AUTH_ENABLED is init_auth() state that never exists in the
    # relay process (the 2026-07-16 scope-gate regression).
    import auth.asgi as asgi_module
    from auth.config import AuthSettings
    monkeypatch.setattr(asgi_module, "get_auth_settings", lambda: AuthSettings(ENABLED=True))


@pytest.fixture
def patched_redis(monkeypatch, redis):
    """Route the wrapper's _get_redis() to the fakeredis fixture, so
    correct-scope-succeeds tests exercise real app.scope.* storage without
    hand-configuring a mock for every redis call scope.py makes."""
    async def _fake_get_redis():
        return redis
    monkeypatch.setattr(routes_mod, "_get_redis", _fake_get_redis)
    return redis


@pytest.fixture
def redis_spy(monkeypatch):
    """A _get_redis() replacement that records whether it was ever reached,
    for wrong-scope-denied tests proving the write short-circuits before
    any Redis access — mirrors Task 4's fake_get_manager.assert_not_called()."""
    spy = AsyncMock()
    monkeypatch.setattr(routes_mod, "_get_redis", spy)
    return spy


WRITE_IDENTITY = {"agent_id": "caller", "scopes": ["relay:write"], "key_id": "k1"}
READ_IDENTITY = {"agent_id": "caller", "scopes": ["relay:read"], "key_id": "k1"}
WRONG_SCOPE_FOR_WRITE = {"agent_id": "caller", "scopes": ["relay:read"], "key_id": "k1"}
WRONG_SCOPE_FOR_READ = {"agent_id": "caller", "scopes": ["relay:write"], "key_id": "k1"}


# ---------------------------------------------------------------------------
# Write routes (relay:write) — full coverage: correct scope succeeds AND
# genuinely touches redis; wrong scope denies AND genuinely never touches
# redis (the higher-severity subset — write-path exposure).
# ---------------------------------------------------------------------------


class TestPostScopeSessionScopeGate:
    @pytest.mark.asyncio
    async def test_allows_when_caller_has_relay_write_scope(self, auth_enabled, patched_redis):
        request = _make_request(
            "POST", "/scope/sessions",
            identity=WRITE_IDENTITY,
            body={"agent_id": "a", "goal": "g", "origin": "cli"},
        )
        response = await route_post_scope_session(request)

        assert response.status_code == 200
        payload = json.loads(response.body)
        assert payload["scope_id"].startswith("sc_")

    @pytest.mark.asyncio
    async def test_denies_when_caller_lacks_relay_write_scope(self, auth_enabled, redis_spy):
        request = _make_request(
            "POST", "/scope/sessions",
            identity=WRONG_SCOPE_FOR_WRITE,
            body={"agent_id": "a", "goal": "g", "origin": "cli"},
        )
        response = await route_post_scope_session(request)

        assert response.status_code == 403
        redis_spy.assert_not_called()


class TestPostScopeScreenScopeGate:
    @pytest.mark.asyncio
    async def test_allows_when_caller_has_relay_write_scope(self, auth_enabled, patched_redis):
        from app.scope import create_session
        session = await create_session(patched_redis, agent_id="a", goal="g", origin="cli")

        request = _make_request(
            "POST", f"/scope/sessions/{session['scope_id']}/screens",
            path_params={"scope_id": session["scope_id"]},
            identity=WRITE_IDENTITY,
            body={"kind": "questions", "mode": "gating", "title": "t", "questions": []},
        )
        response = await route_post_scope_screen(request)

        assert response.status_code == 200
        payload = json.loads(response.body)
        assert payload["screen_id"] == f"{session['scope_id']}-1"

    @pytest.mark.asyncio
    async def test_denies_when_caller_lacks_relay_write_scope(self, auth_enabled, redis_spy):
        request = _make_request(
            "POST", "/scope/sessions/sc_whatever/screens",
            path_params={"scope_id": "sc_whatever"},
            identity=WRONG_SCOPE_FOR_WRITE,
            body={"kind": "questions", "mode": "gating", "title": "t", "questions": []},
        )
        response = await route_post_scope_screen(request)

        assert response.status_code == 403
        redis_spy.assert_not_called()


class TestPostScopeAnswerScopeGate:
    @pytest.mark.asyncio
    async def test_allows_when_caller_has_relay_write_scope(self, auth_enabled, patched_redis):
        from app.scope import create_session, mirror_screen
        session = await create_session(patched_redis, agent_id="a", goal="g", origin="cli")
        screen = await mirror_screen(patched_redis, session["scope_id"], {
            "kind": "questions", "mode": "gating", "title": "t", "questions": [],
        })

        request = _make_request(
            "POST",
            f"/scope/sessions/{session['scope_id']}/screens/{screen['screen_id']}/answer",
            path_params={"scope_id": session["scope_id"], "screen_id": screen["screen_id"]},
            identity=WRITE_IDENTITY,
            body={"answers": {"q1": {"choice": "a"}}, "source": "dashboard"},
        )
        response = await route_post_scope_answer(request)

        assert response.status_code == 200
        payload = json.loads(response.body)
        assert payload["resolved"] is True

    @pytest.mark.asyncio
    async def test_denies_when_caller_lacks_relay_write_scope(self, auth_enabled, redis_spy):
        request = _make_request(
            "POST", "/scope/sessions/sc_whatever/screens/sc_whatever-1/answer",
            path_params={"scope_id": "sc_whatever", "screen_id": "sc_whatever-1"},
            identity=WRONG_SCOPE_FOR_WRITE,
            body={"answers": {"q1": {"choice": "a"}}, "source": "dashboard"},
        )
        response = await route_post_scope_answer(request)

        assert response.status_code == 403
        redis_spy.assert_not_called()


# ---------------------------------------------------------------------------
# Read routes (relay:read) — lighter coverage: one correct-scope-succeeds
# and one wrong-scope-denied case each, per reviewer guidance (lower
# severity than the write routes above).
# ---------------------------------------------------------------------------


class TestGetScopeSessionsScopeGate:
    @pytest.mark.asyncio
    async def test_allows_when_caller_has_relay_read_scope(self, auth_enabled, patched_redis):
        from app.scope import create_session
        await create_session(patched_redis, agent_id="a", goal="g", origin="cli")

        request = _make_request(
            "GET", "/scope/sessions",
            identity=READ_IDENTITY,
            query_string=b"status=active",
        )
        response = await route_get_scope_sessions(request)

        assert response.status_code == 200
        payload = json.loads(response.body)
        assert payload["count"] == 1

    @pytest.mark.asyncio
    async def test_denies_when_caller_lacks_relay_read_scope(self, auth_enabled, redis_spy):
        request = _make_request(
            "GET", "/scope/sessions",
            identity=WRONG_SCOPE_FOR_READ,
            query_string=b"status=active",
        )
        response = await route_get_scope_sessions(request)

        assert response.status_code == 403
        redis_spy.assert_not_called()


class TestGetScopeSessionScopeGate:
    @pytest.mark.asyncio
    async def test_allows_when_caller_has_relay_read_scope(self, auth_enabled, patched_redis):
        from app.scope import create_session
        session = await create_session(patched_redis, agent_id="a", goal="g", origin="cli")

        request = _make_request(
            "GET", f"/scope/sessions/{session['scope_id']}",
            path_params={"scope_id": session["scope_id"]},
            identity=READ_IDENTITY,
        )
        response = await route_get_scope_session(request)

        assert response.status_code == 200
        payload = json.loads(response.body)
        assert payload["scope_id"] == session["scope_id"]

    @pytest.mark.asyncio
    async def test_denies_when_caller_lacks_relay_read_scope(self, auth_enabled, redis_spy):
        request = _make_request(
            "GET", "/scope/sessions/sc_whatever",
            path_params={"scope_id": "sc_whatever"},
            identity=WRONG_SCOPE_FOR_READ,
        )
        response = await route_get_scope_session(request)

        assert response.status_code == 403
        redis_spy.assert_not_called()


class TestGetScopeEventsScopeGate:
    @pytest.mark.asyncio
    async def test_allows_when_caller_has_relay_read_scope(self, auth_enabled, patched_redis):
        from app.scope import create_session, mirror_screen
        session = await create_session(patched_redis, agent_id="a", goal="g", origin="cli")
        await mirror_screen(patched_redis, session["scope_id"], {
            "kind": "questions", "mode": "gating", "title": "t", "questions": [],
        })

        request = _make_request(
            "GET", f"/scope/sessions/{session['scope_id']}/events",
            path_params={"scope_id": session["scope_id"]},
            identity=READ_IDENTITY,
            query_string=b"since=0",
        )
        response = await route_get_scope_events(request)

        assert response.status_code == 200
        payload = json.loads(response.body)
        assert payload["count"] == 1

    @pytest.mark.asyncio
    async def test_denies_when_caller_lacks_relay_read_scope(self, auth_enabled, redis_spy):
        request = _make_request(
            "GET", "/scope/sessions/sc_whatever/events",
            path_params={"scope_id": "sc_whatever"},
            identity=WRONG_SCOPE_FOR_READ,
            query_string=b"since=0",
        )
        response = await route_get_scope_events(request)

        assert response.status_code == 403
        redis_spy.assert_not_called()
