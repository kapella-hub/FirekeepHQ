"""The /procedures surface. Mounted only when PROCEDURE_ENABLED — the
/dreams + /collectors precedent: a disabled deploy 404s rather than serving a
disabled-shaped body.

Scope note, and it is why three of these tests exist at all: spec §9 gates the
reads on `memory:read` and the dismiss on `admin`. `require_scope("admin")`
refuses the ANONYMOUS identity (auth blocker 7 — `ANONYMOUS_SCOPES` is
`SCOPES - {admin, *}` and the disabled path now actually runs the check), so a
dismiss cannot be exercised by an unauthenticated caller and must present a
real admin key. The router builds those dependencies inside a `try/except`, so
a broken auth import would silently serve every route ungated with nothing else
in the suite noticing — `test_reads_are_scope_gated` and
`test_dismiss_refuses_the_anonymous_identity` are what make that impossible.
"""
import json

import pytest
import pytest_asyncio
import fakeredis.aioredis as fr
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from auth import keys
from app.procedures import store
from app.procedures.api import create_procedures_router


class _Settings:
    PROCEDURE_ENABLED = True
    PROCEDURE_EXEC_TTL_DAYS = 90
    PROCEDURE_MAX_SPECS = 50
    QDRANT_COLLECTION = "c"


def _client_for(r):
    app = FastAPI()
    app.include_router(create_procedures_router(
        get_redis=lambda: r, get_vector=lambda: None, settings_fn=lambda: _Settings(),
    ))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.fixture
async def app_and_redis():
    r = fr.FakeRedis(decode_responses=True)
    async with _client_for(r) as c:
        yield c, r


@pytest_asyncio.fixture
async def auth_keys():
    """Real keys, with auth enforcement on: `{"admin": ..., "reader": ...}`.

    Two keys, not one, because `scopes_allow` does NOT treat `admin` as a
    superset of anything — a literal `["admin"]` key is refused by
    `require_scope("memory:read")`. That is also what lets the reader key prove
    the dismiss gate is admin-STRENGTH under enforcement, not merely present.

    Auth state is module-global (`keys._AUTH_ENABLED` / `keys._redis`), so the
    teardown restoring it is load-bearing for every other test in the process.
    """
    auth_redis = fr.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=auth_redis, enabled=True)
    admin = await keys.create_key("owner", ["admin"])
    reader = await keys.create_key("teammate", ["memory:read"])
    try:
        yield {"admin": admin["api_key"], "reader": reader["api_key"]}
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await auth_redis.aclose()


@pytest.mark.asyncio
async def test_rollup_reports_coverage_honestly(app_and_redis):
    client, r = app_and_redis
    await r.set(store.INDEX_KEY, json.dumps([
        {"skill_id": "s1", "skill_trigger": "release", "step_id": "a",
         "step_text": "bump", "pattern": "*.toml", "load_bearing": True, "order": 0},
    ]))
    await store.write_step_stats(r, _Settings(), "s1", {
        "a": {"observed": 3, "skipped": 1, "executions": 4},
    })
    resp = await client.get("/procedures")
    assert resp.status_code == 200
    body = resp.json()
    row = body["procedures"][0]
    assert row["skill_id"] == "s1"
    assert row["observable_steps"] == 1
    assert row["steps"]["a"]["observed"] == 3


@pytest.mark.asyncio
async def test_executions_endpoint_returns_the_receipts(app_and_redis):
    client, r = app_and_redis
    await store.record_observation(
        r, _Settings(), session_id="sess", skill_id="s1", step_id="a",
        action_id="act", target="pyproject.toml", agent_id="ag", adapter="shell-hook")
    resp = await client.get("/procedures/s1/executions")
    assert resp.status_code == 200
    execs = resp.json()["executions"]
    assert execs[0]["session_id"] == "sess"
    assert execs[0]["observed"]["a"][0]["target"] == "pyproject.toml"


@pytest.mark.asyncio
async def test_dismiss_removes_a_proposal_and_404s_on_an_unknown_one(auth_keys):
    r = fr.FakeRedis(decode_responses=True)
    hdr = {"X-API-Key": auth_keys["admin"]}
    async with _client_for(r) as client:
        await store.write_proposals(r, "s1", [
            {"id": "p1", "kind": "dead_step", "skill_id": "s1", "step_id": "a",
             "detail": "d"},
        ])
        assert (await client.post(
            "/procedures/proposals/p1/dismiss", headers=hdr)).status_code == 200
        assert await store.list_proposals(r, "s1") == []
        assert (await client.post(
            "/procedures/proposals/nope/dismiss", headers=hdr)).status_code == 404


@pytest.mark.asyncio
async def test_dismiss_refuses_the_anonymous_identity(app_and_redis):
    """The dismiss gate is `admin`, not `memory:read`: with auth OFF the
    anonymous identity holds every scope BELOW admin, so a route that passed
    here would prove the gate had been downgraded (or dropped by the router's
    try/except) rather than that it works."""
    client, r = app_and_redis
    await store.write_proposals(r, "s1", [
        {"id": "p1", "kind": "dead_step", "skill_id": "s1", "step_id": "a",
         "detail": "d"},
    ])
    assert (await client.post("/procedures/proposals/p1/dismiss")).status_code == 403
    # And it really is refused, not merely reported as refused.
    assert len(await store.list_proposals(r, "s1")) == 1


@pytest.mark.asyncio
async def test_reads_are_scope_gated(auth_keys):
    """Under enforcement, a keyless read is refused — the proof that read_dep
    is actually attached. The same call with a `memory:read` key is served."""
    r = fr.FakeRedis(decode_responses=True)
    async with _client_for(r) as client:
        assert (await client.get("/procedures")).status_code == 401
        assert (await client.get("/procedures/s1/executions")).status_code == 401
        ok = await client.get(
            "/procedures", headers={"X-API-Key": auth_keys["reader"]})
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_a_read_key_cannot_dismiss(auth_keys):
    """Under enforcement too: the dismiss gate is `admin`, and a valid key
    holding every read scope is still refused."""
    r = fr.FakeRedis(decode_responses=True)
    async with _client_for(r) as client:
        await store.write_proposals(r, "s1", [
            {"id": "p1", "kind": "dead_step", "skill_id": "s1", "step_id": "a",
             "detail": "d"},
        ])
        resp = await client.post(
            "/procedures/proposals/p1/dismiss",
            headers={"X-API-Key": auth_keys["reader"]},
        )
    assert resp.status_code == 403
    assert len(await store.list_proposals(r, "s1")) == 1


@pytest.mark.asyncio
async def test_a_cold_deployment_reports_zero_not_an_error(app_and_redis):
    client, _ = app_and_redis
    resp = await client.get("/procedures")
    assert resp.status_code == 200
    assert resp.json()["procedures"] == []
    assert resp.json()["specs_total"] == 0
