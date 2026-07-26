"""Feature extractor — transforms replay traces into SessionFeatures.

Reads replay events for a session and extracts a flat feature vector
suitable for pattern analysis. No ML, just counting.
"""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as aioredis

from app.evals.models import EvalResult
from app.patterns.models import SessionFeatures

logger = logging.getLogger(__name__)


async def extract_session_features(
    replay_redis: aioredis.Redis,
    session_id: str,
    eval_result: EvalResult | None = None,
) -> SessionFeatures | None:
    """Extract features from a session's replay trace.

    Args:
        replay_redis: Redis client for DB 6 (replay data).
        session_id: The session to analyze.
        eval_result: Optional pre-computed eval result to avoid re-reading.

    Returns:
        SessionFeatures or None if the session has no events.
    """
    try:
        from replay.reader import get_session_timeline, get_session_summary

        summary = await get_session_summary(replay_redis, session_id)
        event_count = summary.get("event_count", 0)
        if event_count == 0:
            return None

        timeline = await get_session_timeline(replay_redis, session_id, limit=1000)
        events: list[dict[str, Any]] = timeline.get("events", [])
        if not events:
            return None

        # Extract tool sequence and type counts
        tool_sequence: list[str] = []
        tool_type_counts: dict[str, int] = {}
        memory_reads = 0
        memory_writes = 0
        file_paths: list[str] = []
        claim_count = 0
        tags: set[str] = set()
        success_count = 0
        failure_count = 0

        for event in events:
            event_type = event.get("event_type", "")
            if event_type:
                tool_sequence.append(event_type)
                tool_type_counts[event_type] = tool_type_counts.get(event_type, 0) + 1

            # Count outcomes
            outcome = event.get("outcome")
            if outcome == "success":
                success_count += 1
            elif outcome == "failure":
                failure_count += 1

            # Memory operations
            if event_type == "memory_read":
                memory_reads += 1
            elif event_type == "memory_write":
                memory_writes += 1

            # File claims
            if event_type in ("claim", "lease", "file_edit", "file_write"):
                claim_count += 1
                payload = event.get("payload") or {}
                path = payload.get("path") or payload.get("file_path") or ""
                if path and path not in file_paths:
                    file_paths.append(path)

            # Collect tags
            event_tags = event.get("tags") or []
            for t in event_tags:
                tags.add(t)

        # Compute rates
        total_with_outcome = success_count + failure_count
        tool_success_rate = (success_count / total_with_outcome) if total_with_outcome > 0 else 0.0
        failure_rate = (failure_count / total_with_outcome) if total_with_outcome > 0 else 0.0

        # Determine session outcome
        if eval_result:
            session_outcome = eval_result.outcome or ("failure" if eval_result.has_failures else "success")
        else:
            session_outcome = "failure" if failure_count > success_count else "success"

        return SessionFeatures(
            session_id=session_id,
            duration_ms=summary.get("duration_ms"),
            outcome=session_outcome if session_outcome in ("success", "failure") else "success",
            event_count=event_count,
            tool_sequence=tool_sequence,
            tool_type_counts=tool_type_counts,
            memory_reads=memory_reads,
            memory_writes=memory_writes,
            file_paths=file_paths,
            file_count=len(file_paths),
            claim_count=claim_count,
            tool_success_rate=round(tool_success_rate, 4),
            failure_rate=round(failure_rate, 4),
            tags=sorted(tags),
        )

    except Exception as e:
        logger.warning("Failed to extract features for session %s: %s", session_id, e)
        return None
