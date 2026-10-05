"""A caller may act only on sessions it owns (docs/THREAT-MODEL.md §5.16).

Two writes took a client-named session id on trust:

1. ``POST /skill/evaluate`` queued synthesis for ANY session id. The worker
   then read that session with FIREKEEP_INTERNAL_KEY (session:read:workspace)
   and filed a draft built from it in the review queue every memory:read
   holder lists — a member could publish a teammate's session as a skill.
2. Replay events: any key could file events under another member's
   ``X-Session-Id``. #56 stamps the verified writer and hides foreign events
   from READERS, but eval compute, OWM, the pattern engine and the autopilot
   compute over every event filed under a session, so injected events skewed
   the victim's metrics. The agent gateway also kept the named session in its
   prediction record (which the reconcile and the Celery overdue sweep emit
   under) and its per-session rethink counter.

Both are pinned by BEHAVIOUR: real minted keys on fakeredis through the real
scope dependencies, the real replay emitter on fakeredis, and a fake Bridge
that applies Bridge's own ownership rule (auth.principal.owns_session) to the
key it is shown.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import app.main as main_mod
import app.session_owner as session_owner
import app.skills.api as skill_api
from app.agent_gateway.api import create_agent_gateway_router
from app.agent_gateway.models import ActionBeforeResponse
from app.skills.api import create_skills_router
from auth import keys
from auth.principal import deployment_owner_member_id, deployment_workspace_id, owns_session
from replay.config import ReplaySettings
from replay.emitter import close_emitter, emit, init_emitter

WS = deployment_workspace_id()
OWNER = deployment_owner_member_id()
ALICE = "member-alice"
BOB = "member-bob"
ALICE_SESSION = "alice-sess-1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_owner_cache():
    session_owner.reset_cache()
    yield
    session_owner.reset_cache()


@pytest_asyncio.fixture
async def auth_on():
    """Real keys: alice and bob (enrolled members), the owner's own key, the
    "*" dashboard key and an internal-shaped service key."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    from auth.workspace import ensure_workspace
    await ensure_workspace(redis)

    async def member_key(device: str, member_id: str, scopes: list[str] | None = None) -> str:
        if not await redis.exists(f"auth:member:{member_id}"):
            await redis.hset(f"auth:member:{member_id}", mapping={
                "member_id": member_id, "workspace_id": WS,
                "role": "member", "status": "active",
            })
        minted = await keys.create_key(device, sorted(keys.ENROLLABLE_SCOPES))
        record = f"{keys._KEY_PREFIX}{keys._hash_key(minted['api_key'])}"
        mapping = {"member_id": member_id, "workspace_id": WS}
        if scopes is not None:
            mapping["scopes"] = json.dumps(scopes)
        await redis.hset(record, mapping=mapping)
        return minted["api_key"]

    try:
        ks = {
            "alice": await member_key("alice-laptop", ALICE),
            "bob": await member_key("bob-laptop", BOB),
            "owner": await member_key("owner-laptop", OWNER),
            # FIREKEEP_INTERNAL_KEY's declared scopes (deploy/bootstrap-keys.sh).
            "internal": await member_key("firekeep-internal", OWNER, [
                "memory:write", "session:read", "eval:read", "eval:write",
                "session:read:workspace", "relay:write:service",
            ]),
            "dashboard": (await keys.create_key("dashboard", ["*"]))["api_key"],
        }
        yield ks
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()


@pytest_asyncio.fixture
async def replay(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await init_emitter(redis_client=redis, settings=ReplaySettings(ENABLED=True, REDIS_URL="redis://fake"))
    # _replay_emit's lazy init would otherwise open a real connection.
    monkeypatch.setattr(main_mod, "_replay_initialized", True)
    yield redis
    await close_emitter()
    await redis.aclose()


class FakeBridge:
    """GET /sessions/{id} applying Bridge's rule to the presented key.

    ``callers`` maps an API key to (member, workspace-wide reader?); the
    internal resolver's keyless request (FIREKEEP_INTERNAL_KEY unset in tests)
    is treated as the internal service.
    """

    def __init__(self, sessions: dict[str, dict], callers: dict[str, tuple[str, bool]],
                 *, fail: Exception | None = None, status: int | None = None):
        self.sessions = sessions
        self.callers = callers
        self.fail = fail
        self.status = status
        self.calls: list[tuple[str, str | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        sid = request.url.path.rsplit("/", 1)[-1]
        key = request.headers.get("X-API-Key")
        self.calls.append((sid, key))
        if self.fail is not None:
            raise self.fail
        if self.status is not None:
            return httpx.Response(self.status, json={"error": "boom"})
        member, workspace_wide = self.callers.get(key or "", (OWNER, True))
        meta = self.sessions.get(sid)
        visible = meta is not None and (
            owns_session(meta.get("owner_member", ""), meta.get("owner_workspace", ""),
                         member_id=member, workspace_id=WS)
            or workspace_wide
        )
        if not visible:
            return httpx.Response(404, json={"error": "Session not found"})
        return httpx.Response(200, json={
            "session_id": sid, "goal": "g", "outcome": "", "shadow": "",
            "owner_member": meta.get("owner_member", ""),
            "owner_workspace": meta.get("owner_workspace", ""),
        })


def _install_bridge(monkeypatch, bridge: FakeBridge) -> FakeBridge:
    monkeypatch.setattr(
        session_owner, "bridge_client",
        lambda timeout=session_owner.BRIDGE_TIMEOUT_SECONDS: httpx.AsyncClient(
            transport=httpx.MockTransport(bridge.handler)),
    )
    return bridge


def _client(application: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://cortex")


ALICE_META = {"owner_member": ALICE, "owner_workspace": WS}


def _callers(ks: dict[str, str]) -> dict[str, tuple[str, bool]]:
    return {
        ks["alice"]: (ALICE, False),
        ks["bob"]: (BOB, False),
        ks["owner"]: (OWNER, False),
        ks["internal"]: (OWNER, True),
        ks["dashboard"]: (OWNER, True),
    }


# ---------------------------------------------------------------------------
# 1. POST /skill/evaluate
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatch(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(skill_api, "_dispatch_synthesis", mock)
    return mock


def _skills_app(*, synthesis: bool = True) -> FastAPI:
    from app.main import get_vector

    settings = MagicMock()
    settings.QDRANT_COLLECTION = "firekeep_memory"
    settings.SKILL_SYNTHESIS_ENABLED = synthesis
    settings.BRIDGE_URL = "http://bridge:8070"
    application = FastAPI()
    application.include_router(create_skills_router(lambda: settings))
    application.dependency_overrides[get_vector] = lambda: MagicMock()
    return application


async def _evaluate(key: str | None, session_id: str, *, synthesis: bool = True) -> httpx.Response:
    headers = {"X-API-Key": key} if key else {}
    async with _client(_skills_app(synthesis=synthesis)) as client:
        return await client.post(
            "/skill/evaluate", json={"session_id": session_id, "skill_worthy": True},
            headers=headers)


@pytest.mark.asyncio
async def test_member_cannot_queue_synthesis_of_a_teammates_session(auth_on, dispatch, monkeypatch):
    bridge = _install_bridge(monkeypatch, FakeBridge({ALICE_SESSION: ALICE_META}, _callers(auth_on)))

    response = await _evaluate(auth_on["bob"], ALICE_SESSION)

    assert response.status_code == 404
    assert response.json() == {"detail": "Session not found"}
    dispatch.assert_not_awaited()
    # Bob's own key was what Bridge judged.
    assert bridge.calls == [(ALICE_SESSION, auth_on["bob"])]


@pytest.mark.asyncio
async def test_owner_queues_synthesis_of_own_session_with_its_own_key(auth_on, dispatch, monkeypatch):
    """Bridge's ctx_complete_session forwards the completing caller's key (#47)."""
    bridge = _install_bridge(monkeypatch, FakeBridge({ALICE_SESSION: ALICE_META}, _callers(auth_on)))

    response = await _evaluate(auth_on["alice"], ALICE_SESSION)

    assert response.status_code == 202
    assert response.json() == {"status": "queued", "session_id": ALICE_SESSION}
    dispatch.assert_awaited_once()
    assert dispatch.await_args.args[:2] == (ALICE_SESSION, True)
    assert bridge.calls == [(ALICE_SESSION, auth_on["alice"])]


@pytest.mark.asyncio
async def test_workspace_wide_service_key_passes(auth_on, dispatch, monkeypatch):
    _install_bridge(monkeypatch, FakeBridge({ALICE_SESSION: ALICE_META}, _callers(auth_on)))

    response = await _evaluate(auth_on["internal"], ALICE_SESSION)

    assert response.status_code == 202
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_wildcard_key_passes_without_a_bridge_round_trip(auth_on, dispatch, monkeypatch):
    bridge = _install_bridge(monkeypatch, FakeBridge({ALICE_SESSION: ALICE_META}, _callers(auth_on)))

    response = await _evaluate(auth_on["dashboard"], ALICE_SESSION)

    assert response.status_code == 202
    dispatch.assert_awaited_once()
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_legacy_session_is_the_deployment_owners_alone(auth_on, dispatch, monkeypatch):
    _install_bridge(monkeypatch, FakeBridge({"legacy-1": {}}, _callers(auth_on)))

    assert (await _evaluate(auth_on["owner"], "legacy-1")).status_code == 202
    assert (await _evaluate(auth_on["bob"], "legacy-1")).status_code == 404
    assert dispatch.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bridge_kwargs", [
    {"fail": httpx.ConnectError("bridge down")},
    {"status": 500},
    {"status": 502},
])
async def test_bridge_unavailable_fails_closed(auth_on, dispatch, monkeypatch, bridge_kwargs):
    _install_bridge(monkeypatch, FakeBridge({ALICE_SESSION: ALICE_META}, _callers(auth_on),
                                            **bridge_kwargs))

    response = await _evaluate(auth_on["alice"], ALICE_SESSION)

    assert response.status_code == 503
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_synthesis_answers_before_any_bridge_call(auth_on, dispatch, monkeypatch):
    bridge = _install_bridge(monkeypatch, FakeBridge({}, _callers(auth_on), fail=AssertionError("called")))

    response = await _evaluate(auth_on["bob"], ALICE_SESSION, synthesis=False)

    assert response.status_code == 202
    assert response.json() == {"status": "disabled"}
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_auth_disabled_evaluate_behaves_as_before(dispatch, monkeypatch):
    monkeypatch.setattr(keys, "_AUTH_ENABLED", False)
    bridge = _install_bridge(monkeypatch, FakeBridge({}, {}, fail=AssertionError("called")))

    response = await _evaluate(None, "any-session")

    assert response.status_code == 202
    assert response.json() == {"status": "queued", "session_id": "any-session"}
    dispatch.assert_awaited_once()
    assert bridge.calls == []


# ---------------------------------------------------------------------------
# 2. Replay events filed under a session
# ---------------------------------------------------------------------------


async def _session_event_ids(redis, session_id: str) -> list[str]:
    return list(await redis.zrange(f"rp:session_idx:{session_id}", 0, -1))


async def _event(redis, event_id: str) -> dict:
    stream_id = await redis.get(f"rp:eid:{event_id}")
    entries = await redis.xrange("rp:events", min=stream_id, max=stream_id)
    return entries[0][1]


async def _start(session_id: str, owner: str | None) -> None:
    """Bridge's session_start: stamped with the recorded owner since 2026-10-04,
    unstamped before."""
    stamp = {"workspace_id": WS, "member_id": owner} if owner else {}
    await emit("session_start", session_id, "claude", {"goal": "g"}, **stamp)


async def _write(session_id: str, member: str, event_type: str = "memory_read") -> None:
    await main_mod._replay_emit(
        event_type, session_id=session_id, agent_id="codex",
        workspace_id=WS, member_id=member, payload={"query": "q"},
    )


@pytest.mark.asyncio
async def test_member_event_naming_a_teammates_session_is_filed_under_unknown(
        auth_on, replay, monkeypatch):
    bridge = _install_bridge(monkeypatch, FakeBridge({}, {}, fail=AssertionError("called")))
    await _start(ALICE_SESSION, ALICE)

    await _write(ALICE_SESSION, BOB)
    await _write(ALICE_SESSION, BOB, "memory_feedback")

    in_alice = [await _event(replay, eid) for eid in await _session_event_ids(replay, ALICE_SESSION)]
    assert [e["event_type"] for e in in_alice] == ["session_start"]
    unknown = [await _event(replay, eid) for eid in await _session_event_ids(replay, "unknown")]
    # Kept, with its verified writer, in the writer's own timeline.
    assert [(e["event_type"], e["member_id"]) for e in unknown] == [
        ("memory_read", BOB), ("memory_feedback", BOB)]
    assert session_owner.get_stats()["reattributed"] >= 2
    assert bridge.calls == []  # the stamped start event answered


@pytest.mark.asyncio
async def test_owner_events_stay_in_their_session(auth_on, replay, monkeypatch):
    _install_bridge(monkeypatch, FakeBridge({}, {}, fail=AssertionError("called")))
    await _start(ALICE_SESSION, ALICE)

    await _write(ALICE_SESSION, ALICE)

    in_alice = [await _event(replay, eid) for eid in await _session_event_ids(replay, ALICE_SESSION)]
    assert [(e["event_type"], e.get("member_id")) for e in in_alice] == [
        ("session_start", ALICE), ("memory_read", ALICE)]


@pytest.mark.asyncio
async def test_delegated_distillation_write_stays_in_the_owners_session(auth_on, replay, monkeypatch):
    """Bridge's distiller writes through /memory/learn/delegated with its own
    service key, naming the session OWNER; _store_action_log stamps the event
    with that delegated member, so it is the owner's write."""
    _install_bridge(monkeypatch, FakeBridge({}, {}, fail=AssertionError("called")))
    await _start(ALICE_SESSION, ALICE)

    await _write(ALICE_SESSION, ALICE, "memory_write")

    assert len(await _session_event_ids(replay, ALICE_SESSION)) == 2
    assert await _session_event_ids(replay, "unknown") == []


@pytest.mark.asyncio
async def test_unstamped_session_resolves_its_owner_from_bridge_once(auth_on, replay, monkeypatch):
    """Every session in flight at the deploy that ships Bridge's start-event
    stamps has an UNSTAMPED start event. Its owner comes from Bridge, so its
    own member's events keep landing in it (eval grades exactly as before)."""
    bridge = _install_bridge(monkeypatch, FakeBridge({ALICE_SESSION: ALICE_META}, {}))
    await _start(ALICE_SESSION, None)

    await _write(ALICE_SESSION, ALICE)
    await _write(ALICE_SESSION, BOB)
    await _write(ALICE_SESSION, ALICE)

    in_alice = [await _event(replay, eid) for eid in await _session_event_ids(replay, ALICE_SESSION)]
    assert [e.get("member_id") for e in in_alice] == [None, ALICE, ALICE]
    assert len(await _session_event_ids(replay, "unknown")) == 1
    assert bridge.calls == [(ALICE_SESSION, None)]  # resolved once, then cached


@pytest.mark.asyncio
async def test_prior_art_recall_before_the_start_event_resolves_through_bridge(
        auth_on, replay, monkeypatch):
    """ctx_start_session's prior-art recall can reach Cortex before Bridge's
    session_start replay event is written."""
    bridge = _install_bridge(monkeypatch, FakeBridge({ALICE_SESSION: ALICE_META}, {}))

    await _write(ALICE_SESSION, ALICE)

    assert len(await _session_event_ids(replay, ALICE_SESSION)) == 1
    assert len(bridge.calls) == 1


@pytest.mark.asyncio
async def test_unknown_session_falls_to_the_deployment_owner(auth_on, replay, monkeypatch):
    _install_bridge(monkeypatch, FakeBridge({}, {}))  # Bridge: 404

    await _write("made-up-session", OWNER)
    await _write("made-up-session", BOB)

    assert len(await _session_event_ids(replay, "made-up-session")) == 1
    assert len(await _session_event_ids(replay, "unknown")) == 1


@pytest.mark.asyncio
async def test_bridge_unreachable_keeps_the_claimed_session(auth_on, replay, monkeypatch):
    """Fail OPEN for attribution: failing closed would re-file every
    legitimate event of every cold-cache unstamped session during an outage.
    Readers still hide foreign events (#56); the miss is counted."""
    _install_bridge(monkeypatch, FakeBridge({}, {}, fail=httpx.ConnectError("down")))
    await _start(ALICE_SESSION, None)
    before = session_owner.get_stats()["unresolved"]

    await _write(ALICE_SESSION, ALICE)

    assert len(await _session_event_ids(replay, ALICE_SESSION)) == 2
    assert session_owner.get_stats()["unresolved"] == before + 1


@pytest.mark.asyncio
async def test_unattributed_writes_are_not_checked(auth_on, replay, monkeypatch):
    bridge = _install_bridge(monkeypatch, FakeBridge({}, {}, fail=AssertionError("called")))

    await _write("unknown", BOB)
    await main_mod._replay_emit("collection.sync", session_id=ALICE_SESSION,
                                agent_id="collector", payload={})

    assert len(await _session_event_ids(replay, "unknown")) == 1
    assert len(await _session_event_ids(replay, ALICE_SESSION)) == 1
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_auth_disabled_replay_attribution_is_unchanged(replay, monkeypatch):
    monkeypatch.setattr(keys, "_AUTH_ENABLED", False)
    bridge = _install_bridge(monkeypatch, FakeBridge({}, {}, fail=AssertionError("called")))
    await _start(ALICE_SESSION, ALICE)

    await _write(ALICE_SESSION, BOB)

    assert len(await _session_event_ids(replay, ALICE_SESSION)) == 2
    assert bridge.calls == []


# ---------------------------------------------------------------------------
# 2b. The agent gateway's predict path
# ---------------------------------------------------------------------------


class _RecordingService:
    def __init__(self):
        self.session_ids: list[str] = []

    async def decide(self, req):
        self.session_ids.append(req.session_id)
        return ActionBeforeResponse(decision="allow", action_id="act_1", tier="auto",
                                    advisories=[], reconcile_deadline_seconds=60,
                                    auto_reconcile=False)


async def _before(key: str, session_id: str, service: _RecordingService) -> httpx.Response:
    application = FastAPI()
    application.include_router(create_agent_gateway_router(lambda: service))
    async with _client(application) as client:
        return await client.post("/agent/action/before", headers={"X-API-Key": key}, json={
            "session_id": session_id, "agent_id": "codex", "adapter": "rest",
            "action": {"type": "edit_file", "target": "src/x.py"},
        })


@pytest.mark.asyncio
async def test_gateway_acts_under_unknown_for_a_teammates_session(auth_on, replay, monkeypatch):
    """decide() files the predict event, the prediction record (which the
    reconcile and the overdue sweep emit under) and the rethink counter by the
    session id — none of them may land in Alice's session."""
    _install_bridge(monkeypatch, FakeBridge({}, {}, fail=AssertionError("called")))
    await _start(ALICE_SESSION, ALICE)
    service = _RecordingService()

    assert (await _before(auth_on["bob"], ALICE_SESSION, service)).status_code == 200
    assert (await _before(auth_on["alice"], ALICE_SESSION, service)).status_code == 200

    assert service.session_ids == ["unknown", ALICE_SESSION]
