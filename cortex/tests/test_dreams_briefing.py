"""Dreaming Task 8: the `profile` briefing section.

Surfaces the per-member person profile (app/dreams/store.profile_point_id) in
GET /briefing -- the payoff of the whole Dreaming feature: work -> memories ->
nightly dream -> next session's briefing already knows who it's working with.

Model: cortex/tests/test_briefing_sections_inprocess.py (unit-level section
tests) + cortex/tests/test_briefing_api.py (full-router envelope tests) +
cortex/tests/test_briefing_render.py (render_briefing unit tests).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.briefing import sections as S
from app.briefing.api import create_briefing_router
from app.briefing.render import render_briefing
from app.dreams.store import profile_point_id

_SETTINGS = MagicMock(QDRANT_COLLECTION="firekeep_memory")


def _profile_point(text="Owns the dreaming feature. Prefers terse commit messages."):
    p = MagicMock()
    p.payload = {"text": text, "member_id": "mem1", "timestamp": "2026-08-03T00:00:00Z"}
    return p


# --- profile_section (unit) -------------------------------------------------

@pytest.mark.asyncio
async def test_profile_present_is_ok_with_text_in_data():
    vector = MagicMock()
    vector._client = AsyncMock()
    vector._client.retrieve = AsyncMock(return_value=[_profile_point()])
    sec = await S.profile_section(vector, _SETTINGS, member_id="mem1", workspace_id="ws1")
    assert sec["status"] == "ok"
    assert sec["error"] is None
    assert "dreaming feature" in sec["data"]["text"]
    vector._client.retrieve.assert_awaited_once_with(
        collection_name="firekeep_memory",
        ids=[profile_point_id("mem1", "ws1")],
        with_payload=True,
        with_vectors=False,
    )


@pytest.mark.asyncio
async def test_profile_absent_is_empty_not_unavailable():
    """No profile yet is the normal state on every fresh install (before the
    first dream run), not a failure -- must degrade to 'empty', never
    'unavailable' (which would flip the whole envelope to degraded)."""
    vector = MagicMock()
    vector._client = AsyncMock()
    vector._client.retrieve = AsyncMock(return_value=[])
    sec = await S.profile_section(vector, _SETTINGS, member_id="mem1", workspace_id="ws1")
    assert sec["status"] == "empty"
    assert sec["error"] is None


@pytest.mark.asyncio
async def test_profile_unresolvable_member_id_is_empty_and_skips_lookup():
    vector = MagicMock()
    vector._client = AsyncMock()
    vector._client.retrieve = AsyncMock(side_effect=AssertionError("must not be called"))
    sec = await S.profile_section(vector, _SETTINGS, member_id=None, workspace_id="ws1")
    assert sec["status"] == "empty"
    vector._client.retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_raising_vector_client_propagates():
    """The section itself RAISES on a genuine backend failure (the shared
    sections.py contract: 'Builders ... RAISE on genuine upstream failure').
    api.py's _run_section is what converts that into status='unavailable' --
    exercised end-to-end by test_raising_vector_client_yields_unavailable_only_for_profile
    below."""
    vector = MagicMock()
    vector._client = AsyncMock()
    vector._client.retrieve = AsyncMock(side_effect=RuntimeError("qdrant down"))
    with pytest.raises(RuntimeError, match="qdrant down"):
        await S.profile_section(vector, _SETTINGS, member_id="mem1", workspace_id="ws1")


# --- render_briefing (unit) --------------------------------------------------

def _sec(status="empty", data=None, error=None):
    return {"status": status, "error": error, "data": data or {}}


def _base_sections(**overrides):
    names = ["environment", "tasks", "bulletins", "quality", "strategy_tips",
             "cross_agent", "skills", "vault", "discipline", "dlq",
             "resumable_sessions", "profile"]
    secs = {n: _sec() for n in names}
    secs.update(overrides)
    return secs


def test_rendered_text_includes_profile_when_present():
    secs = _base_sections(profile=_sec("ok", {
        "member_id": "mem1", "text": "Owns the dreaming feature; reviews PRs terse.",
    }))
    text = render_briefing(agent_id="a", goal="g", sections=secs, instructions="i")
    assert "PROFILE: Owns the dreaming feature; reviews PRs terse." in text


def test_rendered_text_omits_profile_line_when_empty():
    secs = _base_sections(profile=_sec("empty", {}))
    text = render_briefing(agent_id="a", goal="g", sections=secs, instructions="i")
    assert "PROFILE" not in text


# --- envelope-level (full router, all upstreams succeeding) -----------------

class _Resp:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _OkClient:
    """Every outbound call the briefing makes succeeds with an empty result,
    so the ONLY thing that can move `degraded` in these tests is the profile
    section under test. The 4 outbound sections themselves are covered by
    test_briefing_sections_outbound.py; here they're just noise to silence."""

    async def get(self, url, headers=None, params=None):
        if "/sessions" in url and params:
            return _Resp({"sessions": []})
        if "/presence/" in url:
            return _Resp({"status": "unknown"})
        if "/tasks" in url:
            return _Resp({"tasks": []})
        if "/bulletin" in url:
            return _Resp({"posts": []})
        if "/events" in url:
            return _Resp({"events": []})
        if "/environment" in url:
            return _Resp({"status": "ok", "event_count": 0, "collectors": {}})
        return _Resp({})


def _make_ok_app(monkeypatch, *, retrieve):
    async def _zero_depths():
        return {"celery": 0, "event_stream": 0, "event_dlq": 0,
                "memory_backfill": 0, "memory_backfill_dlq": 0, "distill_dlq": 0}
    monkeypatch.setattr(S, "collect_queue_depths", _zero_depths)

    app = FastAPI()
    app.include_router(create_briefing_router(section_timeout=2.0))
    app.state.replay_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.state.redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)

    vector = MagicMock()
    vector._client = MagicMock()

    async def _scroll(**_kwargs):
        return ([], None)

    async def _query_points(**_kwargs):
        res = MagicMock()
        res.points = []
        return res

    vector._client.scroll = _scroll
    vector._client.query_points = _query_points
    vector._client.retrieve = retrieve
    vector._embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
    app.state.vector_client = vector

    app.state.http_client = _OkClient()
    return app


def test_no_profile_yet_keeps_envelope_not_degraded(monkeypatch):
    app = _make_ok_app(monkeypatch, retrieve=AsyncMock(return_value=[]))
    body = TestClient(app).get("/briefing?agent_id=x").json()
    assert body["sections"]["profile"]["status"] == "empty"
    assert body["sections"]["profile"]["error"] is None
    assert body["degraded"] is False


def test_raising_vector_client_yields_unavailable_only_for_profile(monkeypatch):
    app = _make_ok_app(
        monkeypatch, retrieve=AsyncMock(side_effect=RuntimeError("qdrant down")),
    )
    body = TestClient(app).get("/briefing?agent_id=x").json()
    assert body["sections"]["profile"]["status"] == "unavailable"
    assert "qdrant down" in body["sections"]["profile"]["error"]
    assert body["degraded"] is True
    # One broken section does not cascade -- every other section still
    # resolved normally (SP1b spec §5.1 isolation).
    for name, sec in body["sections"].items():
        if name == "profile":
            continue
        assert sec["status"] != "unavailable", f"{name}: {sec}"


def test_profile_present_renders_into_the_full_briefing_text(monkeypatch):
    app = _make_ok_app(
        monkeypatch, retrieve=AsyncMock(return_value=[_profile_point("Prefers dark mode.")]),
    )
    body = TestClient(app).get("/briefing?agent_id=mem1").json()
    assert body["sections"]["profile"]["status"] == "ok"
    assert "PROFILE: Prefers dark mode." in body["rendered"]
    assert body["degraded"] is False
