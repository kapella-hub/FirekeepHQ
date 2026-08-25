"""Feature extractor — transforms replay traces into SessionFeatures.

Reads replay events for a session and extracts a flat feature vector
suitable for pattern analysis. No ML, just counting.
"""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as aioredis

from app.evals.compute import _METRIC_SCAN_MAX  # single source of truth (task 4
# brief): same cap as the eval metrics scan and OWM join, imported rather than
# re-declared so the three can never drift apart.
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
        from replay.reader import get_event_batch, get_session_event_ids, get_session_summary

        summary = await get_session_summary(replay_redis, session_id)
        event_count = summary.get("event_count", 0)
        if event_count == 0:
            return None

        # Full-session snapshot+hydrate (PR1 primitives, task 4b): the whole
        # session, not the oldest-1000 window get_session_timeline used to
        # silently truncate to. Capped at _METRIC_SCAN_MAX, same as the eval
        # metrics scan and the OWM join (D3, outcome truth PR2).
        ids = await get_session_event_ids(replay_redis, session_id, limit=_METRIC_SCAN_MAX)
        events: list[dict[str, Any]] = await get_event_batch(replay_redis, ids)
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

        # Outcome truth (2026-08-23): graded task_result projected through
        # the shared binary_outcome — never invented from silence or event
        # counts. eval_result is a VALIDATED EvalResult, so a non-None
        # task_result implies the recognized source pair.
        from app.evals.models import binary_outcome
        tr = getattr(eval_result, "task_result", None) if eval_result else None
        session_outcome = binary_outcome(tr)
        outcome_source = "task_result" if tr is not None else "legacy"

        return SessionFeatures(
            session_id=session_id,
            duration_ms=summary.get("duration_ms"),
            outcome=session_outcome,
            outcome_source=outcome_source,
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
