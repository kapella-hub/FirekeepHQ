"""4-signal skill score computation for breakthrough session detection."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx
import redis.asyncio

from app.config import get_settings
from app.skills import internal_key_headers

logger = logging.getLogger(__name__)

RESOLUTION_PHRASES = frozenset([
    "finally", "the issue was", "turned out to be", "fixed by",
    "root cause", "the fix was", "solved by", "that's why",
    "because of this", "it was actually", "i see now",
])


@dataclass
class SkillScore:
    session_id: str
    total: float
    error_density: float
    session_anomaly: float
    resolution_language: float
    manual_flag: bool
    triggered: bool


async def compute_skill_score(
    session_id: str,
    skill_worthy: bool = False,
) -> SkillScore:
    """Compute weighted 4-signal skill score. Score >= SKILL_SCORE_THRESHOLD triggers synthesis."""
    if skill_worthy:
        return SkillScore(
            session_id=session_id, total=1.0,
            error_density=0.0, session_anomaly=0.0,
            resolution_language=0.0, manual_flag=True, triggered=True,
        )

    settings = get_settings()
    internal_key = settings.FIREKEEP_INTERNAL_KEY

    try:
        error_density = await _score_error_density(session_id, settings.RP_REDIS_URL)
    except Exception:
        error_density = 0.0

    try:
        session_anomaly = await _score_session_anomaly(session_id, settings.BRIDGE_URL, internal_key)
    except Exception:
        session_anomaly = 0.0

    try:
        resolution_language = await _score_resolution_language(session_id, settings.BRIDGE_URL, internal_key)
    except Exception:
        resolution_language = 0.0

    total = min(1.0, (
        error_density * settings.SKILL_ERROR_DENSITY_WEIGHT
        + session_anomaly * settings.SKILL_ANOMALY_WEIGHT
        + resolution_language * settings.SKILL_RESOLUTION_WEIGHT
    ))
    return SkillScore(
        session_id=session_id,
        total=round(total, 4),
        error_density=round(error_density, 4),
        session_anomaly=round(session_anomaly, 4),
        resolution_language=round(resolution_language, 4),
        manual_flag=False,
        triggered=total >= settings.SKILL_SCORE_THRESHOLD,
    )


async def _score_error_density(session_id: str, replay_redis_url: str) -> float:
    """Ratio of failure events to total in the session's replay list in Redis DB 6."""
    try:
        r = redis.asyncio.from_url(replay_redis_url, decode_responses=True)
        try:
            entries = await r.lrange(f"rp:session:{session_id}:events", 0, -1)
            if not entries:
                return 0.0
            total = len(entries)
            failures = sum(
                1 for raw in entries
                for e in [json.loads(raw)]
                if e.get("outcome") in ("failure", "error", "partial")
            )
            return min(1.0, failures / total)
        finally:
            await r.aclose()
    except Exception:
        logger.debug("error_density scoring failed for session %s", session_id)
        return 0.0


async def _score_session_anomaly(
    session_id: str, bridge_url: str, internal_key: str | None = None
) -> float:
    """How much longer this session took vs similar sessions (goal >= 2 shared keywords)."""
    try:
        headers = internal_key_headers(internal_key)
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{bridge_url}/sessions/{session_id}", headers=headers)
            if resp.status_code != 200:
                return 0.0
            session = resp.json()
            duration = float(session.get("duration_seconds") or 0)
            if not duration:
                return 0.0
            goal_words = set((session.get("goal") or "").lower().split())

            hist = await client.get(
                f"{bridge_url}/sessions",
                params={"status": "completed", "limit": 50},
                headers=headers,
            )
            if hist.status_code != 200:
                return 0.0
            similar = [
                s for s in hist.json().get("sessions", [])
                if s.get("session_id") != session_id
                and len(goal_words & set((s.get("goal") or "").lower().split())) >= 2
                and s.get("duration_seconds")
            ]
            if len(similar) < 3:
                return 0.0
            mean = sum(float(s["duration_seconds"]) for s in similar) / len(similar)
            if mean == 0:
                return 0.0
            return min(1.0, max(0.0, (duration / mean - 1.0) / 2.0))
    except Exception:
        logger.debug("session_anomaly scoring failed for session %s", session_id)
        return 0.0


async def _score_resolution_language(
    session_id: str, bridge_url: str, internal_key: str | None = None
) -> float:
    """Fraction of resolution phrases found in session shadow scratch + decisions."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{bridge_url}/sessions/{session_id}",
                headers=internal_key_headers(internal_key),
            )
            if resp.status_code != 200:
                return 0.0
            shadow = resp.json().get("shadow") or {}
            texts: list[str] = []
            for v in shadow.get("scratch", {}).values():
                texts.append(str(v))
            for entry in shadow.get("decision", []):
                texts.append(str(entry.get("value", "")))
            full_text = " ".join(texts).lower()
            if not full_text:
                return 0.0
            hits = sum(1 for phrase in RESOLUTION_PHRASES if phrase in full_text)
            return min(1.0, hits / 3)
    except Exception:
        logger.debug("resolution_language scoring failed for session %s", session_id)
        return 0.0
