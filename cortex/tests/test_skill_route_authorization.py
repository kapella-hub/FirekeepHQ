"""/skills/{id} write routes: scope, target type, tenancy, and review authority.

Before 2026-10-01 `DELETE /skills/{id}` deleted whatever Qdrant point id it was
handed -- a member-private memory or document chunk as readily as a skill, in
any workspace, by any valid key, and even when the lookup failed -- and
`PATCH /skills/{id} {"skill_status": "active"}` let the agent that drafted a
skill approve it. ae1f973 added a workspace check to DELETE but no
memory_type guard; both are pinned here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.skills.api import create_skills_router
from auth import keys
from auth.principal import deployment_workspace_id


def _point(point_id="abc", *, memory_type="skill", status="draft", workspace_id=None, **extra):
    p = MagicMock()
    p.id = point_id
    p.payload = {
        "memory_type": memory_type, "skill_status": status,
        "trigger": "Fix X", "symptoms": "Error Y", "content": "body",
        "domain": "d", "skill_score": 0.0, "namespace": "default",
        "timestamp": "2026-05-23T00:00:00+00:00",
        **extra,
    }
    if workspace_id is not None:
        p.payload["workspace_id"] = workspace_id
    return p


@pytest.fixture
def settings():
    s = MagicMock()
    s.QDRANT_COLLECTION = "firekeep_memory"
    return s


@pytest.fixture
def vector():
    v = MagicMock()
    v._client = AsyncMock()
    v._client.delete = AsyncMock()
    v._client.set_payload = AsyncMock()
    v._client.upsert = AsyncMock()
    v._embed = AsyncMock(return_value=[0.1] * 8)
    return v


def _app(vector, settings) -> FastAPI:
    from app.main import get_vector

    application = FastAPI()
    application.include_router(create_skills_router(lambda: settings))
    application.dependency_overrides[get_vector] = lambda: vector
    return application


# --- target verification (auth-disabled: the anonymous owner principal) ------


@pytest.mark.parametrize("memory_type", ["memory", "corpus", "dream", None])
def test_delete_refuses_a_point_that_is_not_a_skill(vector, settings, memory_type):
    """The guard ae1f973 lacked: a recall result id is not a skill id."""
    vector._client.retrieve = AsyncMock(return_value=[_point(memory_type=memory_type)])
    resp = TestClient(_app(vector, settings)).delete("/skills/abc")
    assert resp.status_code == 404
    vector._client.delete.assert_not_awaited()


def test_delete_refuses_a_skill_in_another_workspace(vector, settings):
    vector._client.retrieve = AsyncMock(
        return_value=[_point(workspace_id="workspace-someone-else")])
    resp = TestClient(_app(vector, settings)).delete("/skills/abc")
    assert resp.status_code == 404
    vector._client.delete.assert_not_awaited()


def test_delete_refuses_when_the_target_cannot_be_verified(vector, settings):
    """Used to delete blind on a lookup failure; now it refuses and changes nothing."""
    vector._client.retrieve = AsyncMock(side_effect=RuntimeError("qdrant down"))
    resp = TestClient(_app(vector, settings)).delete("/skills/abc")
    assert resp.status_code == 503
    vector._client.delete.assert_not_awaited()


def test_delete_missing_point_is_404(vector, settings):
    vector._client.retrieve = AsyncMock(return_value=[])
    resp = TestClient(_app(vector, settings)).delete("/skills/abc")
    assert resp.status_code == 404
    vector._client.delete.assert_not_awaited()


def test_delete_own_workspace_skill_and_unattributed_skill_succeed(vector, settings):
    for ws in (deployment_workspace_id(), None):
        vector._client.delete.reset_mock()
        vector._client.retrieve = AsyncMock(return_value=[_point(workspace_id=ws)])
        resp = TestClient(_app(vector, settings)).delete("/skills/abc")
        assert resp.status_code == 204
        vector._client.delete.assert_awaited_once()


def test_patch_and_get_refuse_a_point_that_is_not_a_skill(vector, settings):
    vector._client.retrieve = AsyncMock(return_value=[_point(memory_type="memory")])
    client = TestClient(_app(vector, settings))
    assert client.patch("/skills/abc", json={"skill_status": "active"}).status_code == 404
    assert client.get("/skills/abc").status_code == 404
    vector._client.set_payload.assert_not_awaited()
    vector._client.upsert.assert_not_awaited()


def test_get_refuses_a_skill_in_another_workspace(vector, settings):
    vector._client.retrieve = AsyncMock(return_value=[_point(workspace_id="workspace-other")])
    assert TestClient(_app(vector, settings)).get("/skills/abc").status_code == 404


def test_auth_disabled_keeps_the_dashboard_review_queue_working(vector, settings):
    """With auth off every caller IS the owner; approval must still work."""
    vector._client.retrieve = AsyncMock(return_value=[_point(status="draft")])
    resp = TestClient(_app(vector, settings)).patch("/skills/abc", json={"skill_status": "active"})
    assert resp.status_code == 200
    vector._client.set_payload.assert_awaited_once()


# --- review authority with real minted keys (auth enabled) ------------------


@pytest_asyncio.fixture
async def minted():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    try:
        agent = await keys.create_key("agent", sorted(keys.ENROLLABLE_SCOPES))
        owner = await keys.create_key("dashboard", ["*"])
        admin_only = await keys.create_key("admin-only", ["admin"])
        relay = await keys.create_key("relay", ["session:write"])
        yield {
            "agent": {"X-API-Key": agent["api_key"]},
            "owner": {"X-API-Key": owner["api_key"]},
            "admin_only": {"X-API-Key": admin_only["api_key"]},
            "relay": {"X-API-Key": relay["api_key"]},
        }
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()


def _client(application):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://c")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"skill_status": "active"},
    {"skill_status": "trial"},
    {"skill_status": "deprecated"},
    {"needs_rereview": False},
    {"stale": False},
    {"clear_duplicate_of": True},
])
async def test_agent_key_cannot_make_a_review_decision(vector, settings, minted, body):
    vector._client.retrieve = AsyncMock(return_value=[_point(status="draft")])
    async with _client(_app(vector, settings)) as client:
        resp = await client.patch("/skills/abc", json=body, headers=minted["agent"])
    assert resp.status_code == 403, resp.text
    assert "admin" in resp.json()["detail"]
    vector._client.set_payload.assert_not_awaited()
    vector._client.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_key_cannot_rewrite_an_approved_skill(vector, settings, minted):
    vector._client.retrieve = AsyncMock(return_value=[_point(status="active")])
    async with _client(_app(vector, settings)) as client:
        resp = await client.patch(
            "/skills/abc", json={"content": "poisoned"}, headers=minted["agent"])
    assert resp.status_code == 403
    vector._client.upsert.assert_not_awaited()
    vector._embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_key_may_still_refine_a_draft_and_compile_step_specs(vector, settings, minted):
    """The two agent-facing PATCH uses must keep working."""
    draft = _point(status="draft")
    active = _point(status="active")
    async with _client(_app(vector, settings)) as client:
        vector._client.retrieve = AsyncMock(return_value=[draft])
        edited = await client.patch(
            "/skills/abc", json={"content": "better steps"}, headers=minted["agent"])
        vector._client.retrieve = AsyncMock(return_value=[active])
        specs = await client.patch(
            "/skills/abc",
            json={"step_specs": [{"text": "run tests", "kind": "unobservable"}]},
            headers=minted["agent"],
        )
    assert edited.status_code == 200, edited.text
    assert specs.status_code == 200, specs.text


@pytest.mark.asyncio
async def test_agent_key_cannot_delete_a_skill(vector, settings, minted):
    vector._client.retrieve = AsyncMock(return_value=[_point(status="draft")])
    async with _client(_app(vector, settings)) as client:
        resp = await client.delete("/skills/abc", headers=minted["agent"])
    assert resp.status_code == 403
    vector._client.delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "admin_only"])
async def test_dashboard_and_admin_keys_keep_review_authority(vector, settings, minted, who):
    vector._client.retrieve = AsyncMock(return_value=[_point(status="draft")])
    async with _client(_app(vector, settings)) as client:
        approved = await client.patch(
            "/skills/abc", json={"skill_status": "active"}, headers=minted[who])
        deleted = await client.delete("/skills/abc", headers=minted[who])
    assert approved.status_code == 200, approved.text
    assert approved.json()  # re-fetched point serialised
    assert deleted.status_code == 204
    vector._client.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_key_without_memory_scope_cannot_reach_skill_writes(vector, settings, minted):
    vector._client.retrieve = AsyncMock(return_value=[_point()])
    async with _client(_app(vector, settings)) as client:
        patched = await client.patch(
            "/skills/abc", json={"content": "x"}, headers=minted["relay"])
        read = await client.get("/skills/abc", headers=minted["relay"])
    assert patched.status_code == 403
    assert read.status_code == 403
