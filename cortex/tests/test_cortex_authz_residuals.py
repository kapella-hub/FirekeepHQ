"""Cortex authorization residuals left open by #45 (audit 2026-10-01).

Four gaps, each pinned by BEHAVIOUR -- requests against the real routes and the
real `require_any_scope` with real minted keys on fakeredis, plus a fake Qdrant
that actually evaluates the filter it is handed -- so a handler that merely
accepts a principal without using it cannot satisfy them:

1. `GET /skills`, `POST /skills` and `POST /skill/evaluate` declared no scope:
   a `session:write`-only relay key could list every skill (drafts included),
   author skills, and queue synthesis runs.
2. `GET /skills` and `GET /memory/contributors` filtered by project only, never
   by the caller's workspace.
3. `POST /memory/feedback` voted on ANY point id -- another workspace's memory,
   another member's member-private memory the caller cannot even recall -- and
   one caller could vote the same memory repeatedly, defeating the Beta prior
   the feedback multiplier relies on ("one reader's thumb nudges").
4. `GET /admin/untagged-calls` had no gate at all.

Ported in spirit from ae1f973's test_retrieval_authorization_contract.py, which
asserted flat `must` conditions; legacy (unattributed) points belong to the
deployment workspace here, so the workspace condition is a nested `should` and
these tests evaluate filters instead of reading their shape.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    IsEmptyCondition,
    MatchAny,
    MatchValue,
)

from app.skills.api import create_skills_router
from auth import keys
from auth.principal import anonymous_principal, deployment_workspace_id

OTHER_WS = "workspace-elsewhere"
RELAY_KEY_SCOPES = ["session:write"]


# ---------------------------------------------------------------------------
# A fake Qdrant that honours the filter it is given
# ---------------------------------------------------------------------------


def _cond_ok(payload: dict, cond) -> bool:
    if isinstance(cond, Filter):
        return _filter_ok(payload, cond)
    if isinstance(cond, IsEmptyCondition):
        value = payload.get(cond.is_empty.key)
        return value is None or value == []
    if isinstance(cond, FieldCondition):
        value = payload.get(cond.key)
        if isinstance(cond.match, MatchValue):
            return value == cond.match.value
        if isinstance(cond.match, MatchAny):
            return value in cond.match.any
    raise AssertionError(f"fake qdrant cannot evaluate {cond!r}")


def _filter_ok(payload: dict, flt: Filter | None) -> bool:
    if flt is None:
        return True
    if flt.must and not all(_cond_ok(payload, c) for c in flt.must):
        return False
    if flt.should and not any(_cond_ok(payload, c) for c in flt.should):
        return False
    if flt.must_not and any(_cond_ok(payload, c) for c in flt.must_not):
        return False
    return True


class FakeQdrant:
    """retrieve / scroll / set_payload over an in-memory point table."""

    def __init__(self, points: dict[str, dict]):
        self.points = {pid: dict(payload) for pid, payload in points.items()}
        self.set_payload_calls = 0

    def _point(self, pid: str):
        return SimpleNamespace(id=pid, payload=self.points[pid])

    async def retrieve(self, *, collection_name, ids, **_kw):
        return [self._point(pid) for pid in ids if pid in self.points]

    async def scroll(self, *, collection_name, scroll_filter=None, limit=100, offset=None, **_kw):
        hits = [self._point(pid) for pid, p in self.points.items() if _filter_ok(p, scroll_filter)]
        return hits[:limit], None

    async def set_payload(self, *, collection_name, payload, points, **_kw):
        self.set_payload_calls += 1
        for pid in points:
            self.points[pid].update(payload)


def _skill(ws: str | None, **extra) -> dict:
    payload = {
        "memory_type": "skill", "skill_status": "active", "trigger": "t",
        "symptoms": "s", "content": "c", "domain": "d", "namespace": "default",
        **extra,
    }
    if ws is not None:
        payload["workspace_id"] = ws
    return payload


def _as_workspace(monkeypatch, workspace_id: str, member_id: str = "member-owner") -> None:
    """Auth-off caller in `workspace_id` (the dependency hands back this identity)."""
    identity = {**anonymous_principal(), "workspace_id": workspace_id, "member_id": member_id}
    monkeypatch.setattr(keys, "_AUTH_ENABLED", False)
    monkeypatch.setattr(keys, "_ANONYMOUS_IDENTITY", identity)


@pytest_asyncio.fixture
async def auth_on():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=redis, enabled=True)
    try:
        agent = await keys.create_key("agent", sorted(keys.ENROLLABLE_SCOPES))
        dashboard = await keys.create_key("dashboard", ["*"])
        admin_only = await keys.create_key("admin-only", ["admin"])
        relay = await keys.create_key("relay", RELAY_KEY_SCOPES)
        yield {
            name: {"X-API-Key": k["api_key"]}
            for name, k in {
                "agent": agent, "dashboard": dashboard,
                "admin_only": admin_only, "relay": relay,
            }.items()
        }
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
        await redis.aclose()


def _client(application: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://cortex")


# ---------------------------------------------------------------------------
# 1 + 2. The /skills router
# ---------------------------------------------------------------------------


def _skills_app(qdrant: FakeQdrant | None = None) -> FastAPI:
    from app.main import get_vector

    settings = MagicMock()
    settings.QDRANT_COLLECTION = "firekeep_memory"
    settings.SKILL_SYNTHESIS_ENABLED = False  # evaluate answers {"status":"disabled"}
    vector = MagicMock()
    vector._client = qdrant or FakeQdrant({})
    if qdrant is None:
        vector._client.upsert = AsyncMock()
    vector._embed = AsyncMock(return_value=[0.1] * 8)
    application = FastAPI()
    application.include_router(create_skills_router(lambda: settings))
    application.dependency_overrides[get_vector] = lambda: vector
    return application


_NEW_SKILL = {"trigger": "When X", "symptoms": "Y", "steps": "1. Z", "domain": "testing"}


async def _hit_skill_routes(client: httpx.AsyncClient, headers: dict) -> dict[str, int]:
    return {
        "list": (await client.get("/skills", headers=headers)).status_code,
        "create": (await client.post("/skills", json=_NEW_SKILL, headers=headers)).status_code,
        "evaluate": (await client.post(
            "/skill/evaluate", json={"session_id": "s-1"}, headers=headers)).status_code,
    }


@pytest.mark.asyncio
async def test_key_without_memory_or_eval_scope_is_refused_by_skill_routes(auth_on):
    """The relay key holds only session:write; it has no business here."""
    async with _client(_skills_app()) as client:
        statuses = await _hit_skill_routes(client, auth_on["relay"])
    assert statuses == {"list": 403, "create": 403, "evaluate": 403}


@pytest.mark.asyncio
async def test_unkeyed_request_is_refused_by_skill_routes_when_auth_is_on(auth_on):
    async with _client(_skills_app()) as client:
        statuses = await _hit_skill_routes(client, {})
    assert statuses == {"list": 401, "create": 401, "evaluate": 401}


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["agent", "dashboard", "admin_only"])
async def test_every_legitimate_skill_caller_keeps_access(auth_on, who):
    """Enrolled member keys (cortex MCP skill tools, bridge's forwarded caller
    key on /skill/evaluate, night shift through the gateway), the dashboard
    ("*") and a literal ["admin"] key."""
    async with _client(_skills_app()) as client:
        statuses = await _hit_skill_routes(client, auth_on[who])
    assert statuses == {"list": 200, "create": 201, "evaluate": 202}


def test_auth_disabled_skill_routes_behave_as_before(monkeypatch):
    monkeypatch.setattr(keys, "_AUTH_ENABLED", False)
    client = TestClient(_skills_app())
    assert client.get("/skills").status_code == 200
    assert client.post("/skills", json=_NEW_SKILL).status_code == 201
    assert client.post("/skill/evaluate", json={"session_id": "s-1"}).status_code == 202


def _listing_qdrant() -> FakeQdrant:
    return FakeQdrant({
        "own": _skill(deployment_workspace_id()),
        "legacy": _skill(None),
        "other": _skill(OTHER_WS),
    })


def test_skill_listing_is_confined_to_the_callers_workspace(monkeypatch):
    """Deployment-workspace caller: its own skills plus unattributed legacy ones."""
    _as_workspace(monkeypatch, deployment_workspace_id())
    resp = TestClient(_skills_app(_listing_qdrant())).get("/skills")
    assert resp.status_code == 200
    assert sorted(s["id"] for s in resp.json()) == ["legacy", "own"]


def test_legacy_skills_are_not_shown_to_another_workspace(monkeypatch):
    _as_workspace(monkeypatch, OTHER_WS)
    resp = TestClient(_skills_app(_listing_qdrant())).get("/skills?status=active")
    assert resp.status_code == 200
    assert [s["id"] for s in resp.json()] == ["other"]


def test_skill_listing_workspace_filter_survives_the_draft_queue_and_project(monkeypatch):
    _as_workspace(monkeypatch, deployment_workspace_id())
    qdrant = FakeQdrant({
        "own-draft": _skill(deployment_workspace_id(), skill_status="draft", project="fk"),
        "other-draft": _skill(OTHER_WS, skill_status="draft", project="fk"),
    })
    resp = TestClient(_skills_app(qdrant)).get("/skills?status=draft&project=FK")
    assert [s["id"] for s in resp.json()] == ["own-draft"]


def _as_author_in(monkeypatch, workspace_id: str) -> None:
    """POST /skills stamps from request_principal, so drive that accessor too."""
    _as_workspace(monkeypatch, workspace_id)
    identity = dict(keys._ANONYMOUS_IDENTITY)
    monkeypatch.setattr("auth.principal.request_principal", lambda _r: identity)


def _reauthor_app(points: dict[str, dict]):
    qdrant = FakeQdrant(points)
    qdrant.upsert = AsyncMock()
    return _skills_app(qdrant), qdrant


@pytest.mark.parametrize("memory_type", ["memory", "corpus", None])
def test_reauthor_of_must_name_a_skill(monkeypatch, memory_type):
    """The bare retrieve accepted any point id -- a memory, a corpus chunk."""
    _as_author_in(monkeypatch, deployment_workspace_id())
    app, qdrant = _reauthor_app({"orig": _skill(deployment_workspace_id(), memory_type=memory_type)})
    resp = TestClient(app).post("/skills", json={**_NEW_SKILL, "reauthor_of": "orig"})
    assert resp.status_code == 404
    qdrant.upsert.assert_not_awaited()


def test_reauthor_of_an_unattributed_skill_is_the_deployment_workspaces_only(monkeypatch):
    """The old check let an unattributed original through for ANY workspace."""
    _as_author_in(monkeypatch, OTHER_WS)
    app, qdrant = _reauthor_app({"orig": _skill(None)})
    assert TestClient(app).post(
        "/skills", json={**_NEW_SKILL, "reauthor_of": "orig"}).status_code == 404
    qdrant.upsert.assert_not_awaited()

    _as_author_in(monkeypatch, deployment_workspace_id())
    app, qdrant = _reauthor_app({"orig": _skill(None)})
    assert TestClient(app).post(
        "/skills", json={**_NEW_SKILL, "reauthor_of": "orig"}).status_code == 201
    qdrant.upsert.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. /memory/contributors
# ---------------------------------------------------------------------------


def _main_overrides(vector, redis_client=None):
    from app.main import app, get_redis, get_vector

    app.dependency_overrides[get_vector] = lambda: vector
    app.dependency_overrides[get_redis] = lambda: redis_client or AsyncMock()
    return app


@pytest.fixture
def main_app():
    from app.main import app

    saved = dict(app.dependency_overrides)
    try:
        yield app
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


def _contributor_points() -> dict[str, dict]:
    return {
        "a": {"member_id": "member-alice", "workspace_id": deployment_workspace_id(),
              "project": "fk"},
        # Unattributed: written before the startup backfill stamped it.
        "b": {"agent_id": "legacy-bot", "project": "fk"},
        "c": {"member_id": "member-mallory", "workspace_id": OTHER_WS, "project": "fk"},
    }


def _contributors_caller(monkeypatch, workspace_id: str) -> None:
    """Drive the caller through request_principal, the accessor the route reads."""
    monkeypatch.setattr("auth.principal.request_principal", lambda _r: {
        "workspace_id": workspace_id, "member_id": "member-alice",
        "credential_id": "c", "scopes": ["memory:read"], "authenticated": True,
    })


@pytest.mark.asyncio
async def test_contributors_count_unattributed_points_for_the_deployment_workspace(
    main_app, monkeypatch,
):
    """Not covered by #54's own test (which pins only the flat workspace match
    for a non-deployment workspace): a legacy point belongs to the deployment
    workspace and is grouped under the deployment owner."""
    from auth.principal import deployment_owner_member_id

    _contributors_caller(monkeypatch, deployment_workspace_id())
    vector = MagicMock()
    vector._client = FakeQdrant(_contributor_points())
    _main_overrides(vector)
    async with _client(main_app) as client:
        resp = await client.get("/memory/contributors")
        scoped = await client.get("/memory/contributors?project=FK")
    assert resp.status_code == 200, resp.text
    want = sorted(["member-alice", deployment_owner_member_id()])
    assert sorted(c["contributor_id"] for c in resp.json()) == want
    assert sorted(c["contributor_id"] for c in scoped.json()) == want


@pytest.mark.asyncio
async def test_contributors_of_another_workspace_exclude_legacy_points(main_app, monkeypatch):
    _contributors_caller(monkeypatch, OTHER_WS)
    vector = MagicMock()
    vector._client = FakeQdrant(_contributor_points())
    _main_overrides(vector)
    async with _client(main_app) as client:
        resp = await client.get("/memory/contributors")
    assert [c["contributor_id"] for c in resp.json()] == ["member-mallory"]


# ---------------------------------------------------------------------------
# 3. Feedback: scope of the target, and one vote per credential
# ---------------------------------------------------------------------------


def _real_vector(points: dict[str, dict]):
    from app.db.vector import VectorClient

    vc = VectorClient.__new__(VectorClient)
    vc._client = FakeQdrant(points)
    vc._collection = "firekeep_memory"
    return vc


def _memory(ws: str | None, **extra) -> dict:
    payload = {"memory_type": "memory", **extra}
    if ws is not None:
        payload["workspace_id"] = ws
    return payload


class TestSetFeedbackScope:
    @pytest.mark.asyncio
    async def test_a_point_in_another_workspace_is_not_found(self):
        from app.db.vector import VectorStoreError

        vc = _real_vector({"m": _memory(OTHER_WS)})
        with pytest.raises(VectorStoreError):
            await vc.set_feedback("m", True, None, "t", workspace_id=deployment_workspace_id(),
                                  member_id="member-owner", see_private=False)
        assert vc._client.set_payload_calls == 0

    @pytest.mark.asyncio
    async def test_legacy_points_belong_to_the_deployment_workspace_only(self):
        from app.db.vector import VectorStoreError

        vc = _real_vector({"m": _memory(None)})
        await vc.set_feedback("m", True, None, "t", workspace_id=deployment_workspace_id(),
                              member_id="member-owner", see_private=False)
        assert vc._client.points["m"]["feedback_useful_count"] == 1
        with pytest.raises(VectorStoreError):
            await vc.set_feedback("m", True, None, "t", workspace_id=OTHER_WS,
                                  member_id="member-x", see_private=False)
        assert vc._client.points["m"]["feedback_useful_count"] == 1

    @pytest.mark.asyncio
    async def test_another_members_private_memory_is_not_found_for_a_member(self):
        from app.db.vector import VectorStoreError

        ws = deployment_workspace_id()
        vc = _real_vector({
            "bobs": _memory(ws, visibility="member", member_id="member-bob"),
            "mine": _memory(ws, visibility="member", member_id="member-alice"),
            "shared": _memory(ws, visibility="workspace", member_id="member-bob"),
        })
        with pytest.raises(VectorStoreError):
            await vc.set_feedback("bobs", True, None, "t", workspace_id=ws,
                                  member_id="member-alice", see_private=False)
        await vc.set_feedback("mine", True, None, "t", workspace_id=ws,
                              member_id="member-alice", see_private=False)
        await vc.set_feedback("shared", True, None, "t", workspace_id=ws,
                              member_id="member-alice", see_private=False)
        assert "feedback_useful_count" not in vc._client.points["bobs"]

    @pytest.mark.asyncio
    async def test_an_operator_may_rate_a_member_private_memory(self):
        """The dashboard (admin) is an operator surface, as in visibility.py."""
        ws = deployment_workspace_id()
        vc = _real_vector({"bobs": _memory(ws, visibility="member", member_id="member-bob")})
        await vc.set_feedback("bobs", False, None, "t", workspace_id=ws,
                              member_id="member-owner", see_private=True)
        assert vc._client.points["bobs"]["feedback_not_useful_count"] == 1


class TestOneVotePerVoter:
    """`voter` is the credential id on the REST path (see the route tests)."""

    @pytest.mark.asyncio
    async def test_a_repeated_vote_counts_once(self):
        vc = _real_vector({"m": _memory(None)})
        for _ in range(5):
            await vc.set_feedback("m", True, None, "t", voter="cred-alice")
        p = vc._client.points["m"]
        assert (p["feedback_useful_count"], p["feedback_not_useful_count"]) == (1, 0)

    @pytest.mark.asyncio
    async def test_the_last_vote_wins(self):
        vc = _real_vector({"m": _memory(None)})
        await vc.set_feedback("m", True, None, "t", voter="cred-alice")
        await vc.set_feedback("m", False, "it was wrong", "t2", voter="cred-alice")
        p = vc._client.points["m"]
        assert (p["feedback_useful_count"], p["feedback_not_useful_count"]) == (0, 1)
        assert p["feedback_last_comment"] == "it was wrong"
        assert p["feedback_last_at"] == "t2"

    @pytest.mark.asyncio
    async def test_distinct_voters_each_count(self):
        vc = _real_vector({"m": _memory(None)})
        for voter in ("cred-alice", "cred-bob", "cred-carol"):
            await vc.set_feedback("m", True, None, "t", voter=voter)
        assert vc._client.points["m"]["feedback_useful_count"] == 3

    @pytest.mark.asyncio
    async def test_unattributed_legacy_counts_are_kept(self):
        """Votes cast before attribution carry no voter; they stay in the totals."""
        vc = _real_vector({"m": _memory(None, feedback_useful_count=2,
                                        feedback_not_useful_count=1)})
        await vc.set_feedback("m", True, None, "t", voter="cred-alice")
        await vc.set_feedback("m", True, None, "t", voter="cred-alice")
        p = vc._client.points["m"]
        assert (p["feedback_useful_count"], p["feedback_not_useful_count"]) == (3, 1)
        await vc.set_feedback("m", False, None, "t", voter="cred-alice")
        assert (p["feedback_useful_count"], p["feedback_not_useful_count"]) == (2, 2)

    @pytest.mark.asyncio
    async def test_no_voter_still_accumulates(self):
        """Auth-off and pre-existing direct callers keep the counter semantics."""
        vc = _real_vector({"m": _memory(None)})
        await vc.set_feedback("m", True, None, "t")
        await vc.set_feedback("m", True, None, "t")
        assert vc._client.points["m"]["feedback_useful_count"] == 2


async def _feedback(client, ids, headers, useful=True):
    with patch("app.main._replay_emit", new_callable=AsyncMock):
        resp = await client.post(
            "/memory/feedback", json={"memory_ids": ids, "useful": useful}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["updated"]


@pytest.mark.asyncio
async def test_feedback_route_does_not_reach_another_workspace(main_app, auth_on):
    vc = _real_vector({"theirs": _memory(OTHER_WS), "ours": _memory(deployment_workspace_id())})
    _main_overrides(vc)
    async with _client(main_app) as client:
        updated = await _feedback(client, ["theirs", "ours"], auth_on["agent"])
    assert updated == 1
    assert "feedback_useful_count" not in vc._client.points["theirs"]


@pytest.mark.asyncio
async def test_feedback_route_hides_another_members_private_memory(main_app, auth_on):
    ws = deployment_workspace_id()
    vc = _real_vector({"bobs": _memory(ws, visibility="member", member_id="member-bob")})
    _main_overrides(vc)
    async with _client(main_app) as client:
        as_member = await _feedback(client, ["bobs"], auth_on["agent"])
        as_dashboard = await _feedback(client, ["bobs"], auth_on["dashboard"])
    assert (as_member, as_dashboard) == (0, 1)


@pytest.mark.asyncio
async def test_feedback_route_counts_one_vote_per_credential(main_app, auth_on):
    vc = _real_vector({"m": _memory(deployment_workspace_id())})
    _main_overrides(vc)
    async with _client(main_app) as client:
        for _ in range(4):
            await _feedback(client, ["m"], auth_on["agent"])
    assert vc._client.points["m"]["feedback_useful_count"] == 1


@pytest.mark.asyncio
async def test_feedback_route_keeps_distinct_keys_of_the_owner_member_apart(main_app, auth_on):
    """Dashboard-minted and firekeep-admin keys all carry the OWNER's member_id
    (auth/keys.py validate_key falls back to deployment_owner_member_id), so a
    ballot keyed on member_id would merge two different people into one vote.
    The ballot is the credential's."""
    vc = _real_vector({"m": _memory(deployment_workspace_id())})
    _main_overrides(vc)
    async with _client(main_app) as client:
        await _feedback(client, ["m"], auth_on["agent"])
        await _feedback(client, ["m"], auth_on["admin_only"])
    assert vc._client.points["m"]["feedback_useful_count"] == 2


@pytest.mark.asyncio
async def test_feedback_route_auth_disabled_keeps_accumulating(main_app, monkeypatch):
    monkeypatch.setattr(keys, "_AUTH_ENABLED", False)
    vc = _real_vector({"m": _memory(None)})
    _main_overrides(vc)
    async with _client(main_app) as client:
        await _feedback(client, ["m"], {})
        await _feedback(client, ["m"], {})
    assert vc._client.points["m"]["feedback_useful_count"] == 2


# ---------------------------------------------------------------------------
# 4. /admin/untagged-calls
# ---------------------------------------------------------------------------


def _counter_redis():
    r = AsyncMock()
    r.get = AsyncMock(return_value="3")
    return r


@pytest.mark.asyncio
async def test_untagged_calls_is_admin_only_when_auth_is_on(main_app, auth_on):
    _main_overrides(MagicMock(), _counter_redis())
    async with _client(main_app) as client:
        statuses = {
            who: (await client.get("/admin/untagged-calls", headers=auth_on[who])).status_code
            for who in ("agent", "relay", "dashboard", "admin_only")
        }
        unkeyed = (await client.get("/admin/untagged-calls")).status_code
    assert statuses == {"agent": 403, "relay": 403, "dashboard": 200, "admin_only": 200}
    assert unkeyed == 401


@pytest.mark.asyncio
async def test_untagged_calls_stays_readable_when_auth_is_off(main_app, monkeypatch):
    """The dashboard's discipline card runs on personal (auth-off) boxes, where
    no caller can hold admin -- so this is NOT require_scope("admin")."""
    monkeypatch.setattr(keys, "_AUTH_ENABLED", False)
    _main_overrides(MagicMock(), _counter_redis())
    async with _client(main_app) as client:
        resp = await client.get("/admin/untagged-calls?days=2")
    assert resp.status_code == 200
    assert resp.json()["total"] == 6
