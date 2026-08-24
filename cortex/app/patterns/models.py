"""Pattern Engine models — Pydantic schemas for session features, pattern cards, datasets, and experiments."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class SessionFeatures(BaseModel):
    """Extracted feature vector for a single session's replay trace."""

    session_id: str
    duration_ms: int | None = None
    outcome: Literal["success", "failure", "unknown"] = "unknown"
    # Provenance of `outcome` (outcome truth, 2026-08-23). The default MUST
    # mean legacy/ungraded: ~30d of cached records carry a fabricated
    # outcome="success" indistinguishable by value.
    outcome_source: Literal["task_result", "legacy"] = "legacy"
    event_count: int = 0
    tool_sequence: list[str] = []
    tool_type_counts: dict[str, int] = {}
    memory_reads: int = 0
    memory_writes: int = 0
    file_paths: list[str] = []
    file_count: int = 0
    claim_count: int = 0
    tool_success_rate: float = 0.0
    failure_rate: float = 0.0
    tags: list[str] = []
    tips_shown: list[str] = []  # pattern IDs that were shown in this session's briefing
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def graded_only(features: list["SessionFeatures"]) -> list["SessionFeatures"]:
    """Features with a real grade — the only population any rate may count."""
    return [
        f for f in features
        if f.outcome_source == "task_result" and f.outcome in ("success", "failure")
    ]


class PatternCard(BaseModel):
    """A discovered pattern describing a strategy that correlates with outcomes."""

    id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    description: str = ""
    pattern_type: Literal[
        "memory_first", "file_hotspot", "tool_sequence", "duration", "failure_mode", "memory_usage"
    ] = "memory_first"
    confidence: float = 0.5
    evidence_count: int = 0
    baseline_rate: float = 0.0
    pattern_rate: float = 0.0
    lift: float = 1.0
    recommendation: str = ""
    tags: list[str] = []
    trending: bool = False
    # Feedback loop: track how this pattern performs when shown in briefings
    times_shown: int = 0          # how many briefings included this tip
    sessions_with_tip: int = 0    # sessions that received this tip
    success_with_tip: int = 0     # successful sessions that had this tip
    success_without_tip: int = 0  # successful sessions that didn't have this tip
    tip_lift: float | None = None # measured improvement when tip is shown
    source_agent: str = ""        # agent that generated this pattern (for cross-agent learning)

    # Pattern classification
    category: Literal["procedural", "risk", "behavioral"] = "procedural"
    stage: Literal["candidate", "observed", "trial", "validated", "stale", "retired", "quarantined"] = "candidate"

    # Scope tags (for goal-scoped relevance)
    scope_goal_type: str = ""     # e.g., "debugging", "feature", "refactor"
    scope_module: str = ""        # e.g., "auth", "billing", "api"
    scope_service: str = ""       # e.g., "cortex", "relay"

    # Quarantine
    quarantine_reason: str = ""
    quarantined_at: datetime | None = None

    # Promotion tracking
    last_matched_at: datetime | None = None  # Last session that matched this pattern
    promoted_at: datetime | None = None      # When last promoted to current stage


class Dataset(BaseModel):
    """A filtered subset of sessions for experiment analysis."""

    id: str                          # dset_{8-char-hash}
    name: str                        # "March debugging sessions"
    description: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    date_min: datetime | None = None
    date_max: datetime | None = None
    agent_ids: list[str] = []        # empty = all
    goal_pattern: str = ""           # regex on session goal
    outcome_filter: str = ""         # "success", "failure", "" = all
    session_ids: list[str] = []      # materialized membership
    session_count: int = 0
    metrics_summary: dict = {}       # aggregated metrics


class Experiment(BaseModel):
    """An experiment linking a pattern to a dataset with statistical results."""

    id: str                          # exp_{8-char-hash}
    name: str                        # "Memory-first strategy validation"
    hypothesis: str                  # "Sessions that check memory first succeed more"
    pattern_id: str                  # Pattern being tested
    status: Literal["running", "concluded", "inconclusive"] = "running"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    dataset_id: str                  # Which dataset this runs against
    treatment_count: int = 0
    control_count: int = 0
    effect_size: float | None = None
    p_value: float | None = None     # chi-square significance
    confidence_interval: tuple[float, float] | None = None
    verdict: str = ""                # "significant", "not significant", "insufficient data"
