"""The briefing's skills section is confined to the caller's workspace.

Same gap as GET /skills (tests/test_cortex_authz_residuals.py): the section
scrolled every workspace's active and trial skills, and its trial fallback did
the same. Legacy (unattributed) skills belong to the deployment workspace.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from auth.principal import deployment_workspace_id
from tests.test_cortex_authz_residuals import (
    OTHER_WS,
    FakeQdrant,
    _as_workspace,
    _client,
    _skill,
)


def _briefing_vector(points: dict[str, dict]):
    vector = MagicMock()
    vector._client = FakeQdrant(points)
    vector._embed = AsyncMock(side_effect=RuntimeError("no embeddings in this test"))
    return vector


@pytest.mark.asyncio
async def test_briefing_skills_are_confined_to_the_callers_workspace():
    from app.briefing import sections as S

    vector = _briefing_vector({
        "own": _skill(deployment_workspace_id(), trigger="own"),
        "legacy": _skill(None, trigger="legacy"),
        "other": _skill(OTHER_WS, trigger="other"),
        "other-trial": _skill(OTHER_WS, skill_status="trial", trigger="other trial"),
    })
    settings = MagicMock(QDRANT_COLLECTION="firekeep_memory")
    with patch("app.main._replay_emit", new_callable=AsyncMock):
        mine = await S.skills_section(vector, settings, goal="", project=None,
                                      workspace_id=deployment_workspace_id())
        theirs = await S.skills_section(vector, settings, goal="", project=None,
                                        workspace_id=OTHER_WS)
    assert sorted(s["id"] for s in mine["data"]["skills"]) == ["legacy", "own"]
    # The trial fallback is filtered too: the other workspace's trial shows only there.
    assert sorted(s["id"] for s in theirs["data"]["skills"]) == ["other", "other-trial"]


@pytest.mark.asyncio
async def test_briefing_skills_without_a_workspace_default_to_the_deployment():
    """A direct caller with no principal must not see every workspace."""
    from app.briefing import sections as S

    vector = _briefing_vector({"other": _skill(OTHER_WS), "own": _skill(None)})
    settings = MagicMock(QDRANT_COLLECTION="firekeep_memory")
    with patch("app.main._replay_emit", new_callable=AsyncMock):
        sec = await S.skills_section(vector, settings, goal="", project=None)
    assert [s["id"] for s in sec["data"]["skills"]] == ["own"]


@pytest.mark.asyncio
async def test_get_briefing_passes_the_verified_workspace_to_the_skills_section(monkeypatch):
    """The route, not just the builder: GET /briefing hands over identity's workspace."""
    from app.briefing import api as briefing_api

    seen = {}

    async def _fake_skills(*_a, workspace_id=None, **_kw):
        seen["workspace_id"] = workspace_id
        return {"status": "empty", "error": None, "data": {"skills": []}}

    monkeypatch.setattr(briefing_api.S, "skills_section", _fake_skills)
    _as_workspace(monkeypatch, OTHER_WS)
    application = FastAPI()
    application.include_router(briefing_api.create_briefing_router(section_timeout=0.5))
    application.state.vector_client = MagicMock()
    application.state.http_client = MagicMock()
    application.state.redis_client = AsyncMock()
    application.state.replay_redis = AsyncMock()
    async with _client(application) as client:
        await client.get("/briefing")
    assert seen["workspace_id"] == OTHER_WS
