"""Evals are read only by the member whose session they describe.

`eval:read` and `eval:write` are enrollable, so every member key holds both.
Before 2026-10-04 `GET /evals/sessions/{sid}` returned any session's eval to
any of them, `GET /evals/summary` listed every member's session ids and
metrics, and `POST /evals/sessions/{sid}/compute` computed a foreign session's
eval and RETURNED it in the response body.

Now an eval carries its session's owner — the verified workspace/member Bridge
stamped on the session-start replay event (replay/authz.py `session_owner`) —
and the same predicate as every other replay read decides who sees it. The two
service keys (Bridge's eval:grade key, the internal session:read:workspace key)
still compute any session's eval: Bridge posts there on EVERY completion with a
key minted onto the owner member, and a 4xx there silently starves OWM.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.evals.api import create_evals_router
from app.evals.compute import compute_session_eval
from app.evals.models import EvalResult
from app.evals.store import store_eval
from auth import keys
from auth.principal import deployment_owner_member_id, deployment_workspace_id
from replay.config import ReplaySettings
from replay.emitter import close_emitter, emit, init_emitter

WS = deployment_workspace_id()
OWNER = deployment_owner_member_id()


@pytest.fixture(autouse=True)
def _no_webhooks(monkeypatch):
    # compute_session_eval fires webhooks against Cortex's own Redis (DB 0).
    monkeypatch.setattr("app.webhooks.fire_webhooks", AsyncMock())


@pytest_asyncio.fixture
async def replay_redis():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await init_emitter(
        redis_client=redis,
        settings=ReplaySettings(ENABLED=True, REDIS_URL="redis://fake"),
    )
    yield redis
    await close_emitter()
    await redis.aclose()


@pytest_asyncio.fixture
async def auth_on():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    try:
        yield redis
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()


def _app(replay) -> FastAPI:
    async def get_replay_redis():
        return replay

    application = FastAPI()
    application.include_router(create_evals_router(get_replay_redis))
    return application


def _client(application: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://c",
    )


async def _member_key(auth_redis, device: str, member_id: str) -> str:
    minted = await keys.create_key(device, sorted(keys.ENROLLABLE_SCOPES))
    await auth_redis.hset(
        f"{keys._KEY_PREFIX}{keys._hash_key(minted['api_key'])}",
        "member_id", member_id,
    )
    return minted["api_key"]


async def _service_key(auth_redis, device: str, scopes: list[str]) -> str:
    """Service-only scopes cannot be minted through create_key (by design):
    bootstrap-keys.sh writes them onto the stored record. Do the same."""
    minted = await keys.create_key(device, ["eval:read"])
    await auth_redis.hset(
        f"{keys._KEY_PREFIX}{keys._hash_key(minted['api_key'])}",
        "scopes", json.dumps(scopes),
    )
    return minted["api_key"]


async def _bridge_session(sid: str, member: str | None) -> None:
    """What Bridge writes: a stamped session_start, then member activity."""
    await emit("session_start", sid, "claude", {"goal": "g"},
               workspace_id=WS if member else None, member_id=member)
    await emit("memory_read", sid, "claude", {"query": f"{member} private query"},
               workspace_id=WS if member else None, member_id=member)


# --- compute stamps the owner --------------------------------------------------


@pytest.mark.asyncio
async def test_compute_records_the_session_owner_from_the_start_event(replay_redis):
    await _bridge_session("alice-session", "member-alice")
    # Anyone can emit a memory event under a client-chosen session id; that
    # must not move ownership. Only the start event decides.
    for _ in range(3):
        await emit("memory_read", "alice-session", "codex", {},
                   workspace_id=WS, member_id="member-bob")

    result = await compute_session_eval(replay_redis, "alice-session", trigger="manual")

    assert result is not None
    assert result.member_id == "member-alice"
    assert result.workspace_id == WS


@pytest.mark.asyncio
async def test_a_session_started_before_attribution_stays_unattributed(replay_redis):
    await _bridge_session("legacy-session", None)
    result = await compute_session_eval(replay_redis, "legacy-session", trigger="manual")
    assert result is not None
    assert result.member_id is None


def test_eval_records_stored_before_attribution_still_parse():
    old = EvalResult.model_validate_json(
        '{"session_id": "s", "trigger": "manual", "metrics": {}}')
    assert old.member_id is None and old.workspace_id is None


# --- reads ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_member_reads_its_own_eval_and_no_one_elses(replay_redis, auth_on):
    await _bridge_session("alice-session", "member-alice")
    await compute_session_eval(replay_redis, "alice-session", trigger="session_complete")
    alice = await _member_key(auth_on, "alice-laptop", "member-alice")
    bob = await _member_key(auth_on, "bob-laptop", "member-bob")
    dashboard = (await keys.create_key("dashboard", ["*"]))["api_key"]

    async with _client(_app(replay_redis)) as client:
        own = await client.get("/evals/sessions/alice-session",
                               headers={"X-API-Key": alice})
        stolen = await client.get("/evals/sessions/alice-session",
                                  headers={"X-API-Key": bob})
        missing = await client.get("/evals/sessions/no-such-session",
                                   headers={"X-API-Key": bob})
        admin = await client.get("/evals/sessions/alice-session",
                                 headers={"X-API-Key": dashboard})

    assert own.status_code == 200
    assert own.json()["session_id"] == "alice-session"
    # A foreign eval reads exactly like a missing one.
    assert stolen.status_code == 404
    assert missing.status_code == 404
    assert stolen.json()["detail"].replace("alice-session", "X") == \
        missing.json()["detail"].replace("no-such-session", "X")
    assert admin.status_code == 200


@pytest.mark.asyncio
async def test_unattributed_evals_belong_to_the_deployment_owner(replay_redis, auth_on):
    await store_eval(replay_redis, EvalResult(session_id="legacy", trigger="manual"))
    owner_runtime = await _member_key(auth_on, "owner-laptop", OWNER)
    bob = await _member_key(auth_on, "bob-laptop", "member-bob")

    async with _client(_app(replay_redis)) as client:
        owner_view = await client.get("/evals/sessions/legacy",
                                      headers={"X-API-Key": owner_runtime})
        bob_view = await client.get("/evals/sessions/legacy",
                                    headers={"X-API-Key": bob})

    assert owner_view.status_code == 200
    assert bob_view.status_code == 404


@pytest.mark.asyncio
async def test_summary_lists_only_the_callers_sessions(replay_redis, auth_on):
    await _bridge_session("alice-session", "member-alice")
    await _bridge_session("bob-session", "member-bob")
    await compute_session_eval(replay_redis, "alice-session", trigger="session_complete")
    await compute_session_eval(replay_redis, "bob-session", trigger="session_complete")
    bob = await _member_key(auth_on, "bob-laptop", "member-bob")
    dashboard = (await keys.create_key("dashboard", ["*"]))["api_key"]

    async with _client(_app(replay_redis)) as client:
        bob_view = await client.get("/evals/summary", headers={"X-API-Key": bob})
        admin_view = await client.get("/evals/summary",
                                      headers={"X-API-Key": dashboard})

    assert bob_view.status_code == 200
    assert [e["session_id"] for e in bob_view.json()["recent_evals"]] == ["bob-session"]
    assert bob_view.json()["total_sessions_evaluated"] == 1
    assert sorted(e["session_id"] for e in admin_view.json()["recent_evals"]) == [
        "alice-session", "bob-session"]


# --- compute -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_member_cannot_compute_and_read_a_foreign_sessions_eval(
    replay_redis, auth_on,
):
    await _bridge_session("alice-session", "member-alice")
    bob = await _member_key(auth_on, "bob-laptop", "member-bob")
    alice = await _member_key(auth_on, "alice-laptop", "member-alice")

    async with _client(_app(replay_redis)) as client:
        stolen = await client.post("/evals/sessions/alice-session/compute",
                                   headers={"X-API-Key": bob})
        own = await client.post("/evals/sessions/alice-session/compute",
                                headers={"X-API-Key": alice})

    assert stolen.status_code == 404
    assert "private query" not in stolen.text
    assert own.status_code == 200
    assert own.json()["member_id"] == "member-alice"


@pytest.mark.asyncio
async def test_service_keys_still_compute_every_members_eval(replay_redis, auth_on):
    """Bridge's key is minted onto the OWNER member and must still compute a
    teammate's eval on every completion — a 4xx there is logged as a permanent
    failure and never retried."""
    await _bridge_session("alice-session", "member-alice")
    await _bridge_session("carol-session", "member-carol")
    # The scope lists deploy/bootstrap-keys.sh declares for the two keys.
    bridge = await _service_key(auth_on, "firekeep-bridge", [
        "memory:read", "memory:write", "session:read", "eval:read",
        "eval:write", "eval:grade"])
    internal = await _service_key(auth_on, "firekeep-internal", [
        "memory:write", "session:read", "eval:read", "eval:write",
        "session:read:workspace"])

    async with _client(_app(replay_redis)) as client:
        by_bridge = await client.post(
            "/evals/sessions/alice-session/compute",
            params={"trigger": "session_complete", "task_result": "success"},
            headers={"X-API-Key": bridge})
        by_internal = await client.post(
            "/evals/sessions/carol-session/compute",
            headers={"X-API-Key": internal})

    assert by_bridge.status_code == 200
    assert by_bridge.json()["task_result"] == "success"
    assert by_internal.status_code == 200


@pytest.mark.asyncio
async def test_auth_disabled_reads_every_eval_as_before(replay_redis, monkeypatch):
    monkeypatch.setattr(keys, "_AUTH_ENABLED", False)
    await _bridge_session("alice-session", "member-alice")
    await compute_session_eval(replay_redis, "alice-session", trigger="session_complete")

    async with _client(_app(replay_redis)) as client:
        view = await client.get("/evals/sessions/alice-session")
        summary = await client.get("/evals/summary")

    assert view.status_code == 200
    assert summary.json()["total_sessions_evaluated"] == 1
