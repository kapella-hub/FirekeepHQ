"""Cortex briefing router — GET /briefing aggregator (SP1b-server).

Template: app/ops.py create_ops_router(). Reads shared clients from
request.app.state (mirrors the audit router at main.py:213). All 12 sections
run as independent asyncio tasks with a per-section timeout; a failed/hung
upstream degrades only that section (SP1b spec §5). The 12th, `observed`, is the
N=1 learning surface (descriptive, unvalidated, provenance-tagged).
"""
from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable

from fastapi import APIRouter, Depends, Query, Request

from auth.middleware import require_scope

from app.config import get_settings
from app.version import get_version_info
from app.briefing import render, sections as S

logger = logging.getLogger(__name__)

Section = dict[str, Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _run_section(name: str, coro: Awaitable[Section], timeout: float) -> Section:
    """Await a section coroutine under a timeout; any failure → unavailable."""
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        logger.warning("Briefing section '%s' timed out after %.2fs", name, timeout)
        return {"status": "unavailable", "error": f"{name}: timeout after {timeout}s", "data": None}
    except Exception as exc:  # noqa: BLE001 — fail-loud per section, never abort the briefing
        logger.warning("Briefing section '%s' failed: %s", name, exc)
        return {"status": "unavailable", "error": f"{name}: {exc}", "data": None}


def create_briefing_router(section_timeout: float = 2.0) -> APIRouter:
    """Create the /briefing aggregator router.

    Args:
        section_timeout: per-section wall budget in seconds (tests pass a small
            value to exercise the timeout→unavailable path deterministically).
    """
    router = APIRouter(prefix="", tags=["briefing"])

    @router.get("/briefing")
    async def get_briefing(
        request: Request,
        identity: dict = Depends(require_scope("session:read")),
        agent_id: str = Query(default="default"),
        goal: str = Query(default=""),
        project: str | None = Query(default=None),
    ) -> dict[str, Any]:
        st = request.app.state
        settings = get_settings()
        scopes = identity.get("scopes", [])
        briefing_id = uuid.uuid4().hex
        ab_group = random.choice(["treatment", "control"])

        # Section name -> coroutine. Order here is irrelevant (gathered
        # concurrently); render.py imposes the display order.
        builders: dict[str, Awaitable[Section]] = {
            "environment": S.environment_section(st.http_client, settings),
            "tasks": S.tasks_section(st.http_client, settings, agent_id),
            "bulletins": S.bulletins_section(st.http_client, settings),
            "quality": S.quality_section(st.replay_redis),
            "strategy_tips": S.strategy_tips_section(st.replay_redis, goal, briefing_id, ab_group),
            "observed": S.observed_patterns_section(st.replay_redis, agent_id, goal),
            "cross_agent": S.cross_agent_section(st.replay_redis, goal, agent_id),
            "skills": S.skills_section(st.vector_client, settings, goal, project),
            "vault": S.vault_section(scopes),
            "discipline": S.discipline_section(st.redis_client, st.replay_redis),
            "dlq": S.dlq_section(),
            "resumable_sessions": S.resumable_sessions_section(st.http_client, settings, agent_id),
        }

        names = list(builders.keys())
        results = await asyncio.gather(
            *(_run_section(n, builders[n], section_timeout) for n in names)
        )
        sections = dict(zip(names, results))

        degraded = any(s["status"] == "unavailable" for s in sections.values())
        instructions = render.build_instructions(sections, agent_id, briefing_id)
        rendered = render.render_briefing(
            agent_id=agent_id, goal=goal, sections=sections, instructions=instructions,
        )

        return {
            "generated_at": _now_iso(),
            "server_version": get_version_info()["version"],
            "agent_id": agent_id,
            "goal": goal,
            "project": project,
            "briefing_id": briefing_id,
            "degraded": degraded,
            "sections": sections,
            "instructions": instructions,
            "rendered": rendered,
        }

    return router
