"""Eval result models."""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class EvalResult(BaseModel):
    """Evaluation result for a single session's trace.

    Contains Tier 1 metrics (directly measurable from traces) and
    optionally Tier 2 metrics (LLM-judged, clearly labeled).
    """

    session_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trigger: Literal["session_complete", "session_abandon", "manual"]

    # Tier 1: Directly measurable from traces (always present)
    metrics: dict[str, float] = {}

    # Tier 2: LLM-judged (present only if LLM eval was run)
    llm_judged_metrics: dict[str, float] | None = None
    llm_judge_model: str | None = None

    # Session summary
    event_count: int = 0
    duration_ms: int | None = None
    outcome: str | None = None

    # Failure analysis (only if session had failures)
    failure_event_ids: list[str] = []
    has_failures: bool = False


class EvalSummary(BaseModel):
    """Aggregate eval metrics across multiple sessions."""

    total_sessions_evaluated: int = 0
    sessions_with_failures: int = 0
    avg_metrics: dict[str, float] = {}
    metric_ranges: dict[str, dict[str, float]] = {}  # {metric: {min, max, avg}}
    recent_evals: list[dict[str, Any]] = []
