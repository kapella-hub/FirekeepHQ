"""Pydantic models for the Replay Engine trace events."""

from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Trace link types
# ---------------------------------------------------------------------------

# We observe temporal correlations between events, NOT true causation.
# Link types are explicit about the level of certainty.

LinkType = Literal[
    "observed",   # System observed temporal correlation (A before B in same trace)
    "declared",   # Emitting service explicitly declared the link
    "inferred",   # Post-hoc inference (e.g., memory_read shortly before tool_call)
]

Relationship = Literal[
    "informed_by",  # This event was informed by the target
    "triggered",    # This event triggered the target
    "produced",     # This event produced the target as output
    "preceded",     # This event preceded the target temporally
]

EventType = Literal[
    "session_start",
    "session_end",
    "memory_read",
    "memory_write",
    "ctx_update",
    "env_change",
    "claim",
    "release",
    "coordination",
    "webhook",
]

EventOutcome = Literal["success", "failure", "partial"]

# ---------------------------------------------------------------------------
# Max payload size (same guard as Cortex GenericEventIngest)
# ---------------------------------------------------------------------------

_MAX_PAYLOAD_BYTES = 50_000  # 50 KB


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def _new_id() -> str:
    """Generate a time-sortable unique ID.

    Uses UUID4 hex (128-bit random). Ordering relies on Redis stream IDs
    (timestamp-based), not on the event ID itself.
    """
    return uuid4().hex


class TraceLink(BaseModel):
    """A link between two trace events.

    Explicitly typed and confidence-scored. These are NOT causal links —
    we observe correlations, not causation. The link_type field makes this
    explicit.
    """

    target_event_id: str = Field(..., min_length=1, max_length=200)
    link_type: LinkType
    relationship: Relationship
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class TraceEvent(BaseModel):
    """Single event in the replay trace log.

    Follows OpenTelemetry-compatible trace/span model for grouping related
    events. Schema version is mandatory for forward compatibility.
    """

    # Identity
    id: str = Field(default_factory=_new_id)
    schema_version: int = Field(default=1, ge=1)
    trace_id: str = Field(default_factory=_new_id)
    span_id: str = Field(default_factory=_new_id)
    parent_span_id: str | None = None

    # Context
    session_id: str = Field(..., min_length=1, max_length=200)
    agent_id: str = Field(..., min_length=1, max_length=200)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    namespace: str = Field(default="default", min_length=1, max_length=200)

    # Classification
    event_type: EventType

    # Trace links
    trace_links: list[TraceLink] = Field(default=[], max_length=50)

    # Payload (varies by event_type, documented per type)
    payload: dict[str, Any] = Field(default={})

    # Outcome
    outcome: EventOutcome | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=5000)

    # Context snapshot ref (only at decision points)
    context_ref: str | None = Field(default=None, max_length=200)

    # Idempotency
    idempotency_key: str | None = Field(default=None, max_length=500)

    # Tags
    tags: list[Annotated[str, Field(max_length=100)]] = Field(default=[], max_length=20)

    @field_validator("namespace")
    @classmethod
    def _normalize_namespace(cls, v: str) -> str:
        return v.lower().strip().replace("-", "_")

    @field_validator("payload")
    @classmethod
    def _validate_payload_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        import json as _json

        serialized = _json.dumps(v, default=str)
        if len(serialized) > _MAX_PAYLOAD_BYTES:
            msg = f"Payload exceeds maximum size of {_MAX_PAYLOAD_BYTES} bytes"
            raise ValueError(msg)
        return v


# ---------------------------------------------------------------------------
# Response models (for query endpoints)
# ---------------------------------------------------------------------------


class TraceEventResponse(BaseModel):
    """Single event returned from query endpoints."""

    id: str
    schema_version: int
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    session_id: str
    agent_id: str
    timestamp: str  # ISO format string
    namespace: str
    event_type: str
    trace_links: list[TraceLink] = []
    payload: dict[str, Any] = {}
    outcome: str | None = None
    duration_ms: int | None = None
    error: str | None = None
    context_ref: str | None = None
    tags: list[str] = []
    # Redis stream ID (for pagination)
    stream_id: str | None = None


class TimelineResponse(BaseModel):
    """Response from GET /replay/sessions/{sid}/events."""

    session_id: str
    events: list[TraceEventResponse]
    total: int
    has_more: bool = False


class SessionSummaryResponse(BaseModel):
    """Response from GET /replay/sessions/{sid}/summary."""

    session_id: str
    event_count: int
    duration_ms: int | None = None
    event_type_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    first_event_at: str | None = None
    last_event_at: str | None = None
    agents: list[str] = []
    has_failures: bool = False


class NarrowingResult(BaseModel):
    """Single suspect from the narrowing algorithm."""

    event: TraceEventResponse
    suspicion_score: float
    depth: int


class NarrowingResponse(BaseModel):
    """Response from POST /replay/sessions/{sid}/narrow."""

    failure_event_id: str
    suspects: list[NarrowingResult]
    total_events_walked: int
    # An empty `suspects` list has three very different causes and used to
    # report all three identically. These two separate them: the id was not
    # found at all, the session records no trace links for the algorithm to
    # walk, or a genuine walk turned up nothing. Defaults keep the shape
    # backward-compatible for any caller built against the old body.
    failure_event_found: bool = True
    session_has_trace_links: bool = False
