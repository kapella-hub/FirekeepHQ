"""A credential can write only so many memories per window (THREAT-MODEL §5.19).

Before this, the only ceiling on memory writes was slowapi's per-IP
``RATE_LIMIT`` on a few REST routes: in-process memory (one bucket per uvicorn
worker), keyed on the client address. Every MCP write reaches cortex-api from
cortex-mcp's container address, so all agents shared one bucket, and one key
used from many hosts escaped it entirely. A compromised non-admin key could
write as fast as Cortex would accept.

Now every memory write a member credential makes is charged against ONE Redis
counter keyed on the verified credential id, whichever surface it arrived
through (REST /memory/learn, /memory/stream per event, POST /skills; MCP
proxies onto those routes with the caller's key). Over the ceiling: 429 with
Retry-After, nothing written, and the first refusal in each window is
signalled to the owner (replay event + webhook) naming the credential.

Behavioural tests: real keys minted into fakeredis, the real scope and auth
middleware, a real (fake) Redis counter, mocked stores.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from auth import keys

from app.config import get_settings
from app.main import app, get_graph, get_rag_engine, get_redis, get_vector

WORKSPACE = "workspace-local"
OWNER = "member-owner"
ALICE = "member-alice"
BOB = "member-bob"
LIMIT = 3


@pytest.fixture(autouse=True)
def _deployment(monkeypatch):
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", WORKSPACE)
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", OWNER)
    settings = get_settings()
    monkeypatch.setattr(settings, "MEMORY_WRITE_LIMIT_PER_CREDENTIAL", LIMIT, raising=False)
    monkeypatch.setattr(settings, "MEMORY_WRITE_LIMIT_WINDOW_SECONDS", 3600, raising=False)
    # slowapi's per-IP limiter is per-process and shared by the whole suite;
    # keep it out of the way so only the per-credential ceiling is measured.
    monkeypatch.setattr(settings, "RATE_LIMIT", "100000/minute")


@pytest_asyncio.fixture
async def auth_on():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    for member, role in ((OWNER, "owner"), (ALICE, "member"), (BOB, "member")):
        await redis.hset(f"auth:member:{member}", mapping={
            "member_id": member, "workspace_id": WORKSPACE,
            "role": role, "status": "active"})
    try:
        yield redis
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()


async def _mint(redis, name: str, scopes: list[str], *, member: str = OWNER,
                extra_scopes: tuple[str, ...] = ()) -> dict:
    created = await keys.create_key(name, scopes)
    key_hash = keys._hash_key(created["api_key"])
    await redis.hset(f"auth:key:{key_hash}", mapping={
        "member_id": member, "workspace_id": WORKSPACE})
    if extra_scopes:
        await redis.hset(f"auth:key:{key_hash}", "scopes",
                         json.dumps(sorted(set(scopes) | set(extra_scopes))))
    return created


@pytest_asyncio.fixture
async def counter():
    """Cortex's own Redis (DB 0 in production): where the counter lives.
    decode_responses=False, like app.state.redis_client in main.py."""
    r = fakeredis.aioredis.FakeRedis()
    try:
        yield r
    finally:
        await r.aclose()


@pytest_asyncio.fixture
async def client(mock_graph, mock_vector, counter, monkeypatch):
    async def _graph():
        return mock_graph

    async def _vector():
        return mock_vector

    async def _redis():
        return counter

    async def _engine():
        return None

    monkeypatch.setattr(app.state, "redis_client", counter, raising=False)
    app.dependency_overrides.update({
        get_graph: _graph, get_vector: _vector,
        get_redis: _redis, get_rag_engine: _engine,
    })
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://cortex",
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def signals(monkeypatch):
    """Record the owner-visible signals instead of emitting them (autouse: the
    real replay emitter would dial a Redis this suite does not run)."""
    replay = AsyncMock()
    webhook = AsyncMock()
    monkeypatch.setattr("app.main._replay_emit", replay)
    monkeypatch.setattr("app.webhooks.fire_webhooks", webhook)
    return {"replay": replay, "webhook": webhook}


BODY = {"action": "wrote a memory", "outcome": "it was stored"}


def _event(i: int = 0) -> dict:
    return {"source": "test", "payload": {"n": i}}


async def _learn(client, key: dict, i: int = 0):
    return await client.post(
        "/memory/learn",
        json={"action": f"wrote memory {i}", "outcome": "stored"},
        headers={"X-API-Key": key["api_key"]},
    )


async def _counter_keys(counter) -> list[str]:
    return [k.decode() async for k in counter.scan_iter("memory:write_limit*")]


# ---------------------------------------------------------------------------
# The ceiling itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_key_is_refused_past_the_limit_and_nothing_is_written(
    auth_on, client, mock_graph, mock_vector, signals
):
    alice = await _mint(auth_on, "alice-laptop", ["memory:write"], member=ALICE)
    for i in range(LIMIT):
        resp = await _learn(client, alice, i)
        assert resp.status_code == 200, resp.text

    resp = await _learn(client, alice, 99)
    assert resp.status_code == 429, resp.text
    retry_after = int(resp.headers["Retry-After"])
    assert 1 <= retry_after <= 3600
    detail = resp.json()["detail"]
    assert detail["error_code"] == "MEMORY_WRITE_LIMITED"
    assert detail["credential_id"] == alice["credential_id"]
    assert detail["limit"] == LIMIT
    assert detail["window_seconds"] == 3600
    assert detail["retry_after"] == retry_after

    # Nothing written for the refused request: both stores saw exactly LIMIT.
    assert mock_vector.upsert.await_count == LIMIT
    assert mock_graph.merge_action_log.await_count == LIMIT


@pytest.mark.asyncio
async def test_limit_is_per_credential_not_per_member_or_address(
    auth_on, client, mock_vector, signals
):
    """Same IP for everyone here (the test transport) -- exactly the MCP
    situation that made the per-IP limiter one shared bucket."""
    alice_laptop = await _mint(auth_on, "alice-laptop", ["memory:write"], member=ALICE)
    alice_desktop = await _mint(auth_on, "alice-desktop", ["memory:write"], member=ALICE)
    bob = await _mint(auth_on, "bob-laptop", ["memory:write"], member=BOB)

    for i in range(LIMIT):
        assert (await _learn(client, alice_laptop, i)).status_code == 200
    assert (await _learn(client, alice_laptop, 9)).status_code == 429

    assert (await _learn(client, alice_desktop, 1)).status_code == 200
    assert (await _learn(client, bob, 1)).status_code == 200


@pytest.mark.asyncio
async def test_zero_disables_the_limit(auth_on, client, mock_vector, counter, monkeypatch):
    monkeypatch.setattr(get_settings(), "MEMORY_WRITE_LIMIT_PER_CREDENTIAL", 0, raising=False)
    alice = await _mint(auth_on, "alice-laptop", ["memory:write"], member=ALICE)
    for i in range(LIMIT + 3):
        assert (await _learn(client, alice, i)).status_code == 200
    assert await _counter_keys(counter) == []


# ---------------------------------------------------------------------------
# One counter for every surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_events_count_per_event_on_the_same_counter(
    auth_on, client, counter, mock_vector, signals
):
    alice = await _mint(auth_on, "alice-laptop", ["memory:write"], member=ALICE)
    assert (await _learn(client, alice)).status_code == 200
    resp = await client.post("/memory/stream", json=[_event(1), _event(2)],
                             headers={"X-API-Key": alice["api_key"]})
    assert resp.status_code == 200, resp.text            # 1 + 2 = LIMIT

    resp = await client.post("/memory/stream", json=_event(3),
                             headers={"X-API-Key": alice["api_key"]})
    assert resp.status_code == 429
    assert (await _learn(client, alice, 5)).status_code == 429
    settings = get_settings()
    assert await counter.llen(settings.REDIS_STREAM_KEY) == 2


@pytest.mark.asyncio
async def test_refused_stream_batch_queues_nothing_and_spends_nothing(
    auth_on, client, counter, signals
):
    alice = await _mint(auth_on, "alice-laptop", ["memory:write"], member=ALICE)
    resp = await client.post(
        "/memory/stream", json=[_event(i) for i in range(LIMIT + 1)],
        headers={"X-API-Key": alice["api_key"]})
    assert resp.status_code == 429
    assert await counter.llen(get_settings().REDIS_STREAM_KEY) == 0
    # The refused batch did not consume the budget it was refused for.
    for i in range(LIMIT):
        assert (await _learn(client, alice, i)).status_code == 200


def _skills_app(counter) -> FastAPI:
    """POST /skills lives on a router the lifespan mounts; mount it bare with
    the same Cortex Redis, as test_cortex_authz_residuals.py does."""
    from app.skills.api import create_skills_router

    settings = MagicMock()
    settings.QDRANT_COLLECTION = "firekeep_memory"
    settings.SKILL_SYNTHESIS_ENABLED = False
    vector = MagicMock()
    vector._client = MagicMock()
    vector._client.upsert = AsyncMock()
    vector._embed = AsyncMock(return_value=[0.1] * 8)
    application = FastAPI()
    application.state.redis_client = counter
    application.include_router(create_skills_router(lambda: settings))
    application.dependency_overrides[get_vector] = lambda: vector
    application.state.test_vector = vector
    return application


@pytest.mark.asyncio
async def test_skill_create_spends_from_the_same_counter(
    auth_on, client, counter, signals
):
    alice = await _mint(auth_on, "alice-laptop", ["memory:read", "memory:write"],
                        member=ALICE)
    for i in range(LIMIT):
        assert (await _learn(client, alice, i)).status_code == 200

    skills_app = _skills_app(counter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=skills_app),
                                 base_url="http://cortex") as skills:
        resp = await skills.post(
            "/skills",
            json={"trigger": "When X", "symptoms": "Y", "steps": "1. Z",
                  "domain": "testing"},
            headers={"X-API-Key": alice["api_key"]})
    assert resp.status_code == 429, resp.text
    assert "Retry-After" in resp.headers
    skills_app.state.test_vector._client.upsert.assert_not_awaited()


# ---------------------------------------------------------------------------
# Who is exempt, and why
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes,extra", [
    (["admin"], ()),                                   # an operator key
    (["*"], ()),                                       # owner / dashboard key
    (["memory:write", "session:read"],                 # FIREKEEP_INTERNAL_KEY
     ("session:read:workspace", "relay:write:service")),
    (["memory:read", "memory:write"],                  # FIREKEEP_BRIDGE_KEY
     ("memory:write:delegated", "eval:grade")),
])
async def test_admin_and_service_keys_are_exempt(
    auth_on, client, counter, scopes, extra
):
    key = await _mint(auth_on, "privileged", scopes, extra_scopes=extra)
    for i in range(LIMIT + 2):
        resp = await _learn(client, key, i)
        assert resp.status_code == 200, resp.text
    assert await _counter_keys(counter) == []


@pytest.mark.asyncio
async def test_delegated_distillates_are_not_charged_to_the_member(
    auth_on, client, counter
):
    """Bridge's distiller presents its own service key; the distillate is
    attributed to the member but does not spend the member's budget."""
    bridge = await _mint(auth_on, "firekeep-bridge", ["memory:write"],
                         extra_scopes=("memory:write:delegated",))
    alice = await _mint(auth_on, "alice-laptop", ["memory:write"], member=ALICE)
    for i in range(LIMIT + 2):
        resp = await client.post(
            "/memory/learn/delegated",
            json={"action": f"distillate {i}", "outcome": "ok"},
            headers={"X-API-Key": bridge["api_key"],
                     "X-Firekeep-Delegated-Member-Id": ALICE,
                     "X-Firekeep-Delegated-Credential-Id": alice["credential_id"]})
        assert resp.status_code == 200, resp.text
    for i in range(LIMIT):
        assert (await _learn(client, alice, i)).status_code == 200


@pytest.mark.asyncio
async def test_auth_disabled_has_no_limit_and_touches_no_counter(
    client, counter, mock_vector
):
    """Personal / no-auth mode behaves exactly as before (and the LongMemEval
    harness, which runs its stack with AUTH_ENABLED=false, is never limited)."""
    await keys.init_auth(redis_client=None, enabled=False)
    for i in range(LIMIT + 3):
        resp = await client.post("/memory/learn",
                                 json={"action": f"m {i}", "outcome": "ok"})
        assert resp.status_code == 200, resp.text
    resp = await client.post("/memory/stream", json=[_event(i) for i in range(10)])
    assert resp.status_code == 200
    assert await _counter_keys(counter) == []


# ---------------------------------------------------------------------------
# Detection: the limit also tells the owner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_refusal_in_a_window_signals_the_owner_once(
    auth_on, client, signals
):
    alice = await _mint(auth_on, "alice-laptop", ["memory:write"], member=ALICE)
    for i in range(LIMIT):
        assert (await _learn(client, alice, i)).status_code == 200
    for i in range(3):
        assert (await _learn(client, alice, 50 + i)).status_code == 429

    limited = [c for c in signals["replay"].await_args_list
               if c.args and c.args[0] == "memory_write_limited"]
    assert len(limited) == 1, signals["replay"].await_args_list
    call = limited[0]
    assert call.kwargs["workspace_id"] == WORKSPACE
    assert call.kwargs["member_id"] == ALICE
    payload = call.kwargs["payload"]
    assert payload["credential_id"] == alice["credential_id"]
    assert payload["surface"] == "memory_learn"
    assert payload["limit"] == LIMIT

    hooks = [c for c in signals["webhook"].call_args_list
             if c.args[1] == "memory.write_limited"]
    assert len(hooks) == 1
    assert hooks[0].args[2]["credential_id"] == alice["credential_id"]
    assert hooks[0].args[2]["member_id"] == ALICE


def test_write_limited_is_a_subscribable_webhook_event():
    from app.webhook_formatters import _EVENT_LABELS as EVENT_LABELS
    from app.webhooks import VALID_EVENTS

    assert "memory.write_limited" in VALID_EVENTS
    assert "memory.write_limited" in EVENT_LABELS


# ---------------------------------------------------------------------------
# The counter itself (unit)
# ---------------------------------------------------------------------------


def _principal(**over) -> dict:
    base = {"workspace_id": WORKSPACE, "member_id": ALICE,
            "credential_id": "a11ce0000000a11c", "scopes": ["memory:write"],
            "authenticated": True}
    base.update(over)
    return base


def test_limit_subject_prefers_the_credential_then_the_member():
    from app.write_limit import limit_subject

    assert limit_subject(_principal()) == "credential:a11ce0000000a11c"
    assert limit_subject(_principal(credential_id="")) == f"member:{ALICE}"
    assert limit_subject(_principal(authenticated=False)) is None
    assert limit_subject(_principal(scopes=["*"])) is None
    assert limit_subject(_principal(scopes=["admin", "memory:write"])) is None
    assert limit_subject(
        _principal(scopes=["memory:write", "memory:write:delegated"])) is None


def test_unattributable_authenticated_principal_fails_closed():
    from fastapi import HTTPException

    from app.write_limit import limit_subject

    with pytest.raises(HTTPException) as exc:
        limit_subject(_principal(credential_id="", member_id=""))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_window_rolls_over(counter, monkeypatch):
    from fastapi import HTTPException

    from app import write_limit

    clock = {"now": 7200.0 + 10}
    monkeypatch.setattr(write_limit.time, "time", lambda: clock["now"])
    for _ in range(LIMIT):
        await write_limit.charge_memory_write(_principal(), counter, surface="t")
    with pytest.raises(HTTPException) as exc:
        await write_limit.charge_memory_write(_principal(), counter, surface="t")
    assert exc.value.headers["Retry-After"] == str(3600 - 10)

    clock["now"] = 7200.0 + 3600 + 1
    await write_limit.charge_memory_write(_principal(), counter, surface="t")


@pytest.mark.asyncio
async def test_counter_store_failure_fails_open(caplog):
    """A Redis outage must not stop every member's writes; it is logged."""
    from app import write_limit

    broken = MagicMock()
    pipe = MagicMock()
    pipe.execute = AsyncMock(side_effect=ConnectionError("redis down"))
    broken.pipeline = MagicMock(return_value=pipe)
    await write_limit.charge_memory_write(_principal(), broken, surface="t")
    assert "fail" in caplog.text.lower()


@pytest.mark.asyncio
async def test_no_counter_store_fails_open():
    from app import write_limit

    await write_limit.charge_memory_write(_principal(), None, surface="t")


@pytest.mark.asyncio
async def test_a_refusal_shows_in_the_memory_audit_trail():
    """The replay event is only a signal if somewhere an owner looks shows it:
    /audit/memory (and the audit_memory MCP tool) list it with the writes."""
    from app.audit import get_memory_audit

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await r.xadd("rp:events", {
        "event_type": "memory_write_limited", "session_id": "unknown",
        "agent_id": "unknown", "workspace_id": WORKSPACE, "member_id": ALICE,
        "timestamp": "2026-10-05T12:00:00+00:00", "outcome": "refused",
        "payload": json.dumps({"credential_id": "a11ce0000000a11c",
                               "surface": "memory_learn"}),
    })
    for action in ("write", None):
        events = await get_memory_audit(r, action=action, workspace_id=WORKSPACE,
                                        member_id=None)
        assert [e["event_type"] for e in events] == ["memory_write_limited"], action
        assert events[0]["payload"]["credential_id"] == "a11ce0000000a11c"
    assert await get_memory_audit(r, action="read", workspace_id=WORKSPACE,
                                  member_id=None) == []
    await r.aclose()
