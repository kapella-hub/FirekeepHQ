"""Eval result models."""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

_GRADES = ("success", "partial", "failure")
_RECOGNIZED_SOURCES = ("self_reported",)
_GRADE_EVENT_TYPES = ("session_end", "session.completed")


def recognized_grade_pair(
    task_result: object, task_result_source: object,
) -> tuple[str, str] | tuple[None, None]:
    """The (grade, source) pair is atomic (spec D2): both recognized, or neither.

    The ONLY grade-validity check in cortex — every consumer imports this."""
    if task_result in _GRADES and task_result_source in _RECOGNIZED_SOURCES:
        return task_result, task_result_source  # type: ignore[return-value]
    return None, None


def binary_outcome(task_result: str | None) -> str:
    """Project a grade onto the binary feature space: success/failure pass
    through; partial and None are 'unknown' (binary-ambiguous, excluded)."""
    return task_result if task_result in ("success", "failure") else "unknown"


def grade_from_events(events: list[dict]) -> tuple[str | None, str | None]:
    """Last recognized grade pair on a TERMINAL event (session_end from the
    tool layer, session.completed from SessionManager — redundant channels
    that fail independently, spec D7). Junk degrades to (None, None)."""
    task_result: str | None = None
    task_result_source: str | None = None
    for e in events:
        if e.get("event_type") not in _GRADE_EVENT_TYPES:
            continue
        p = e.get("payload")
        if not isinstance(p, dict):   # round-6 finding 6: a non-empty non-dict
            continue                  # payload must degrade, not raise on .get
        tr, src = recognized_grade_pair(p.get("task_result"),
                                        p.get("task_result_source"))
        if tr:
            task_result, task_result_source = tr, src
    return task_result, task_result_source


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
    # True when the metrics scan (and the OWM join's event fetch) hit
    # _METRIC_SCAN_MAX and the session's true event history is longer — the
    # cap made VISIBLE rather than a silent truncation (task 4, outcome truth
    # PR2 D3). Old stored records predate this field and parse as False,
    # which is honest: they were computed under the old undisclosed
    # oldest-1000 cap, not this one.
    metrics_truncated: bool = False

    # Tier 2: LLM-judged (present only if LLM eval was run)
    llm_judged_metrics: dict[str, float] | None = None
    llm_judge_model: str | None = None

    # Session summary
    event_count: int = 0
    duration_ms: int | None = None
    outcome: str | None = None

    # Attribution (Living Instructions round 2 — the measurement contract).
    # All optional with defaults: records stored before this release carry
    # none of these and MUST keep parsing. `metrics` above stays
    # dict[str, float] — attribution is never a metric.
    runtime: str | None = None
    client_version: str | None = None
    # {rendered, expected, gateway} — only the keys whose headers arrived.
    instructions: dict[str, Any] | None = None
    # bool(session_start payload briefing_id) when a session_start event
    # exists; None when the timeline has no session_start at all.
    briefing_delivered: bool | None = None
    # Pre-registered arm ("A"/"B") from outcome truth PR4 D1 — a
    # deterministic sha256 hash of the verified owner_member, assigned once
    # at session start, orthogonal to task_result. None covers both an
    # unverified/unattributed session (excluded from arms) AND a record
    # stored before this field shipped — the two are indistinguishable on
    # the wire and neither is a measured arm.
    experiment_group: str | None = None
    # PR5 D13: one-way member key (sha256(owner_member)[:12]) riding the same
    # path as experiment_group — the member-level analysis groups on it. None
    # covers both an unattributed session and a pre-PR5 record.
    member_token: str | None = None
    # PR5 D12: the briefing this session received, so the nudge_shown receipt
    # (keyed by briefing_id) is joinable per session. briefing_delivered above
    # stays the exposure receipt; this is the join key, not a new receipt.
    briefing_id: str | None = None
    # From get_session_summary — previously computed per session and discarded.
    agents: list[str] = []

    # Failure analysis (only if session had failures)
    failure_event_ids: list[str] = []
    has_failures: bool = False

    # Structured task grade (outcome truth, 2026-08-23). The pair is atomic;
    # the BEFORE-validator normalizes the raw mapping because Literal field
    # validation would raise before an after-validator ever ran — a junk
    # stored record must parse as ungraded, not fail wholesale.
    task_result: Literal["success", "partial", "failure"] | None = None
    task_result_source: Literal["self_reported"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _atomic_grade_pair(cls, data):
        if isinstance(data, dict) and ("task_result" in data or "task_result_source" in data):
            data = dict(data)
            tr, src = recognized_grade_pair(data.get("task_result"),
                                            data.get("task_result_source"))
            data["task_result"] = tr
            data["task_result_source"] = src
        return data


class EvalSummary(BaseModel):
    """Aggregate eval metrics across multiple sessions."""

    total_sessions_evaluated: int = 0
    sessions_with_failures: int = 0
    avg_metrics: dict[str, float] = {}
    metric_ranges: dict[str, dict[str, float]] = {}  # {metric: {min, max, avg}}
    recent_evals: list[dict[str, Any]] = []
