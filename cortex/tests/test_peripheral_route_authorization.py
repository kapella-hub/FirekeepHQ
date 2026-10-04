"""Every destructive or tenant-revealing Cortex route declares the scope it needs.

Ported from ae1f973 (agent/member-runtime-authz) and adapted to main. Before
2026-10-01 these routes were authenticated by the global key middleware and
nothing more, so ANY valid key -- including a `session:write`-only service
key -- could recall and write the owner's memory, delete any Qdrant point
through /skills/{id}, approve its own draft skill, re-embed the whole store
under a model of its choosing, and read every member's recall queries from
/audit. See docs/THREAT-MODEL.md section 5.10.

The structural checks read the REAL dependency closures, so a handler that
merely mentions a scope in a comment cannot satisfy them; the behavioural
checks drive the real `require_scope` / `require_any_scope` against fakeredis
with real minted keys.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.audit import create_audit_router
from app.embedding_admin import create_embedding_router
from app.skills.api import create_skills_router
from auth import keys


def _required_scope_sets(route: APIRoute) -> list[set[str]]:
    """Each scope dependency on the route, as the set of scopes that satisfies it.

    require_scope closes over `scope` (one string); require_any_scope closes
    over `scopes` (a tuple, any one of which suffices).
    """
    found: list[set[str]] = []
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        pending.extend(dependant.dependencies)
        call = dependant.call
        if not callable(call):
            continue
        try:
            nonlocals = inspect.getclosurevars(call).nonlocals
        except TypeError:
            continue
        if isinstance(nonlocals.get("scope"), str):
            found.append({nonlocals["scope"]})
        elif isinstance(nonlocals.get("scopes"), tuple):
            found.append(set(nonlocals["scopes"]))
    return found


def _api_routes(routes):
    """Every APIRoute, on either FastAPI route-table shape.

    0.128 flattens included routers into `app.routes`; 0.140+ wraps each in an
    `_IncludedRouter` with no `.path` (see test_auth_admin_router_gating.py).
    `fastapi>=0.115,<1` spans both, so a flat scan passes on a dev box and finds
    nothing in CI. The factories below include routers WITHOUT a prefix, so a
    nested route's own path is already the served path."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _api_routes(inner.routes)


def _route(application, path: str, method: str) -> APIRoute:
    routes = application.routes if hasattr(application, "routes") else application
    matches = [
        r for r in _api_routes(routes)
        if r.path == path and method.upper() in r.methods
    ]
    assert len(matches) == 1, f"{method} {path}: {len(matches)} routes"
    return matches[0]


def _main_app():
    from app.main import app

    return app


def _streaming_app():
    from app.streaming import create_streaming_router

    application = FastAPI()
    application.include_router(create_streaming_router(AsyncMock(), AsyncMock(), AsyncMock()))
    return application


def _skills_app():
    application = FastAPI()
    application.include_router(create_skills_router(lambda: MagicMock()))
    return application


def _audit_app():
    application = FastAPI()
    application.include_router(create_audit_router(lambda: AsyncMock()))
    return application


def _embedding_app():
    application = FastAPI()
    application.include_router(create_embedding_router(AsyncMock()))
    return application


@pytest.mark.parametrize(
    ("factory", "path", "method", "expected"),
    [
        (_main_app, "/memory/recall", "POST", {"memory:read", "admin"}),
        (_main_app, "/memory/learn", "POST", {"memory:write", "admin"}),
        # Service-only, and matched LITERALLY inside the handler too
        # (auth/principal.py delegated_attribution): "*" passes this
        # dependency but not the delegation check.
        (_main_app, "/memory/learn/delegated", "POST", {"memory:write:delegated"}),
        (_main_app, "/memory/stream", "POST", {"memory:write", "admin"}),
        (_main_app, "/memory/feedback", "POST", {"memory:write", "admin"}),
        (_main_app, "/memory/contributors", "GET", {"memory:read", "admin"}),
        (_main_app, "/memory/handoff", "POST", {"memory:read", "admin"}),
        (_streaming_app, "/memory/recall/stream", "POST", {"memory:read", "admin"}),
        (_skills_app, "/skills/{skill_id}", "GET", {"memory:read", "admin"}),
        (_skills_app, "/skills/{skill_id}", "PATCH", {"memory:write", "admin"}),
        (_skills_app, "/skills/{skill_id}", "DELETE", {"memory:write", "admin"}),
        (_audit_app, "/audit/memory", "GET", {"replay:read"}),
        (_audit_app, "/audit/memory/summary", "GET", {"replay:read"}),
        (_embedding_app, "/admin/embeddings/status", "GET", {"memory:read", "admin"}),
        (_embedding_app, "/admin/embeddings/reembed", "POST", {"admin"}),
        (_embedding_app, "/admin/embeddings/reembed/{task_id}", "GET", {"admin"}),
    ],
)
def test_route_declares_its_required_scope(factory, path, method, expected):
    route = _route(factory(), path, method)
    assert expected in _required_scope_sets(route), (
        f"{method} {path} declares {_required_scope_sets(route)}, expected {expected}"
    )


def test_every_embedding_admin_route_is_scoped():
    """A route added to the router later must not ship ungated by omission."""
    routes = list(_api_routes(_embedding_app().routes))
    assert routes, "found no routes -- the walk is broken, not the gate"
    for route in routes:
        assert _required_scope_sets(route), f"{route.path} declares no scope"


def test_reembed_refuses_anonymous_when_auth_is_disabled(monkeypatch):
    monkeypatch.setattr(keys, "_AUTH_ENABLED", False)
    resp = TestClient(_embedding_app()).post("/admin/embeddings/reembed")
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"]


def test_embedding_status_stays_readable_when_auth_is_disabled(monkeypatch):
    """The dashboard's status widget runs on auth-off boxes too."""
    monkeypatch.setattr(keys, "_AUTH_ENABLED", False)
    vector = AsyncMock()
    vector.get_embedding_info = AsyncMock(return_value={"model": "m", "dimensions": 3})
    application = FastAPI()
    application.include_router(create_embedding_router(vector))
    resp = TestClient(application).get("/admin/embeddings/status")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Behavioural: real minted keys against the real dependencies.
# ---------------------------------------------------------------------------

# What deploy/bootstrap-keys.sh mints. Kept literal on purpose: if the script's
# scope lists change, the matching assertion in deploy/tests/test_bootstrap_keys.sh
# changes with them and this table should be revisited in the same commit.
INTERNAL_KEY_SCOPES = ["memory:write", "session:read", "eval:read", "eval:write"]
# The bootstrap also gives this key the service-only `eval:grade`, which
# create_key refuses to mint by design; it is irrelevant to these routes.
# memory:read here is TRANSITIONAL (deploy/bootstrap-keys.sh): it covers Bridge
# recalling with its own key, which fix/bridge-session-ownership replaces with
# the caller's key — drop it from both places when that lands.
BRIDGE_KEY_SCOPES = ["memory:read", "memory:write", "session:read", "eval:read", "eval:write"]
RELAY_KEY_SCOPES = ["session:write"]


@pytest_asyncio.fixture
async def auth_on():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    try:
        yield redis
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()


def _client(application: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://cortex")


@pytest.mark.asyncio
async def test_reembed_is_admin_only_with_real_keys(auth_on, monkeypatch):
    import app.workers.reembed as reembed_mod

    started = MagicMock()
    started.id = "task-1"
    delay = MagicMock(return_value=started)
    monkeypatch.setattr(reembed_mod.reembed_all, "delay", delay)

    internal = await keys.create_key("internal", INTERNAL_KEY_SCOPES)
    agent = await keys.create_key("agent", sorted(keys.ENROLLABLE_SCOPES))
    owner = await keys.create_key("owner", ["*"])

    async with _client(_embedding_app()) as client:
        refused_internal = await client.post(
            "/admin/embeddings/reembed", headers={"X-API-Key": internal["api_key"]})
        refused_agent = await client.post(
            "/admin/embeddings/reembed", headers={"X-API-Key": agent["api_key"]})
        allowed = await client.post(
            "/admin/embeddings/reembed", headers={"X-API-Key": owner["api_key"]})

    assert refused_internal.status_code == 403
    assert refused_agent.status_code == 403
    assert allowed.status_code == 200
    delay.assert_called_once()


def _recall_app() -> FastAPI:
    """The real /memory/recall dependency list, on a minimal app."""
    from app.main import _MEMORY_READ

    application = FastAPI()

    @application.post("/memory/recall", dependencies=[_MEMORY_READ])
    async def _recall() -> dict:
        return {"ok": True}

    return application


@pytest.mark.asyncio
async def test_memory_read_gate_admits_every_legitimate_recall_caller(auth_on):
    """The caller table in the commit message, executed.

    Bridge (prior-art, proactive recall) recalls with FIREKEEP_BRIDGE_KEY;
    agents recall through cortex-mcp with their enrolled key; the dashboard
    with DASHBOARD_API_KEY ("*"); a literal ["admin"] key keeps working. A
    relay-only key, which has no business reading memory, is refused.
    """
    bridge = await keys.create_key("bridge", BRIDGE_KEY_SCOPES)
    agent = await keys.create_key("agent", sorted(keys.ENROLLABLE_SCOPES))
    dashboard = await keys.create_key("dashboard", ["*"])
    admin_only = await keys.create_key("admin-only", ["admin"])
    relay = await keys.create_key("relay", RELAY_KEY_SCOPES)

    async with _client(_recall_app()) as client:
        statuses = {
            name: (await client.post(
                "/memory/recall", headers={"X-API-Key": k["api_key"]})).status_code
            for name, k in {
                "bridge": bridge, "agent": agent, "dashboard": dashboard,
                "admin_only": admin_only, "relay": relay,
            }.items()
        }

    assert statuses == {
        "bridge": 200, "agent": 200, "dashboard": 200, "admin_only": 200, "relay": 403,
    }


def test_enrolled_member_keys_carry_every_scope_these_routes_demand():
    """A teammate's enrolled key must not start 403ing on its own memory."""
    for scope in ("memory:read", "memory:write", "replay:read"):
        assert scope in keys.ENROLLABLE_SCOPES
