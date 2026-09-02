"""Pydantic models for FirekeepCortex API requests and responses."""

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.procedures.models import StepSpec
from app.version import VERSION


def normalize_namespace(ns: str) -> str:
    """Normalize a namespace: lowercase, hyphens to underscores."""
    return ns.lower().strip().replace("-", "_")


# ---------------------------------------------------------------------------
# Request Models
# ---------------------------------------------------------------------------


class ContextQuery(BaseModel):
    """Query for memory recall — describes what the agent is trying to do."""

    task: str = Field(..., min_length=1, max_length=2000)
    tags: list[Annotated[str, Field(max_length=100)]] = Field(default=[], max_length=20)
    top_k: int = Field(default=5, ge=1, le=100)
    # Who initiated this recall — absent means a deliberate agent/tool call;
    # "prompt-hook" marks the client's pushed per-prompt recall (Proactive
    # Recall, 2026-08-18). Carried into the memory_read replay event so the
    # compliance measurement can slice deliberate vs pushed recall: a pushed
    # recall raises "recall before you answer" mechanically, and an exposure
    # change that cannot be attributed would confound the frozen predicates'
    # trend (the 0.1.41 round-2 contract precedent).
    trigger: str | None = Field(default=None, max_length=32)
    # Category scope, and OPTIONAL BY DEFAULT — see `db/vector.namespace_condition`.
    # Omitting it searches every namespace the caller's workspace_id permits;
    # naming one scopes to exactly that one, `"default"` included. This field
    # used to default to the literal `"default"`, which the vector leg then
    # treated as a wildcard — so "unspecified" and "the default category" were
    # the same request and could not both be served. Splitting them is what
    # lets an unscoped recall reach the 146 memories the product's own guidance
    # tells agents to file under `namespace="infrastructure"` and friends.
    namespace: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    include_archived: bool = Field(default=False)
    project: str | None = Field(default=None, max_length=200)
    token_budget: int = Field(default=600, ge=50, le=10000)
    format: Literal["synthesized", "raw"] = Field(default="synthesized")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "task": "Fix the authentication timeout bug in the login flow",
                    "tags": ["auth", "bugfix"],
                    "top_k": 5,
                }
            ]
        }
    }

    @field_validator("project", mode="before")
    @classmethod
    def lowercase_project(cls, v: str | None) -> str | None:
        return v.lower().strip() if v else None

    @field_validator("namespace")
    @classmethod
    def _normalize_namespace(cls, v: str | None) -> str | None:
        return normalize_namespace(v) if v is not None else None


class ActionLog(BaseModel):
    """Record of an agent action and its outcome, optionally with a resolution."""

    action: str = Field(..., min_length=1, max_length=5000)
    outcome: str = Field(..., min_length=1, max_length=5000)
    resolution: str | None = Field(default=None, max_length=5000)
    tags: list[Annotated[str, Field(max_length=100)]] = Field(default=[], max_length=20)
    domain: str = Field(default="general", min_length=1, max_length=200)
    memory_type: Literal["reference", "procedural", "episodic", "transient"] = Field(default="episodic")
    namespace: str = Field(default="default", min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_-]+$")

    # Versioning & provenance (optional on input — system fills defaults)
    memory_chain_id: str | None = Field(default=None, max_length=200)
    created_by: str = Field(default="agent", max_length=200)
    source_session_id: str | None = Field(default=None, max_length=200)

    # Team continuity
    project: str | None = Field(default=None, max_length=200)
    # Access tracking (written by RAG engine, synced by memory_agent)
    access_count: int = Field(default=0, ge=0)
    last_recalled_at: str | None = Field(default=None)
    importance_score: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("project", mode="before")
    @classmethod
    def lowercase_project(cls, v: str | None) -> str | None:
        return v.lower().strip() if v else None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "action": "Increased connection pool size from 5 to 20",
                    "outcome": "Database timeout errors resolved",
                    "resolution": "Updated DB_POOL_SIZE in config and restarted service",
                    "tags": ["database", "performance"],
                    "domain": "infrastructure",
                }
            ]
        }
    }

    @field_validator("namespace")
    @classmethod
    def _normalize_namespace(cls, v: str) -> str:
        return normalize_namespace(v)


_MAX_PAYLOAD_SERIALIZED_BYTES = 50_000  # 50 KB limit per event payload


class GenericEventIngest(BaseModel):
    """Arbitrary event data for stream ingestion into the Redis queue."""

    source: str = Field(..., min_length=1, max_length=500)
    payload: dict[str, Any]
    timestamp: datetime | None = None
    tags: list[Annotated[str, Field(max_length=100)]] = Field(default=[], max_length=20)
    namespace: str = Field(default="default", min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_-]+$")

    @model_validator(mode="after")
    def _validate_payload_size(self) -> "GenericEventIngest":
        """Reject payloads that exceed the serialized byte limit."""
        import json as _json

        serialized = _json.dumps(self.payload, default=str)
        if len(serialized) > _MAX_PAYLOAD_SERIALIZED_BYTES:
            msg = f"Payload exceeds maximum size of {_MAX_PAYLOAD_SERIALIZED_BYTES} bytes"
            raise ValueError(msg)
        return self

    def model_post_init(self, __context: Any) -> None:
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "source": "ci-pipeline",
                    "payload": {
                        "build_id": "abc-123",
                        "status": "failed",
                        "error": "OOM in test suite",
                    },
                    "tags": ["ci", "failure"],
                }
            ]
        }
    }

    @field_validator("namespace")
    @classmethod
    def _normalize_namespace(cls, v: str) -> str:
        return normalize_namespace(v)


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------


class MemorySource(BaseModel):
    """Provenance record for a single memory retrieval result."""

    store: Literal["graph", "vector", "both"]
    content: str
    score: float
    metadata: dict[str, Any] = {}


class RecallResponse(BaseModel):
    """Response from /memory/recall — merged, scored memory context."""

    context_block: str
    sources: list[MemorySource]
    score: float
    # Stable Qdrant ids for the returned sources. MCP renders these beside the
    # Markdown so callers can pass the exact memories they acted on to feedback.
    memory_ids: list[str] = Field(default_factory=list, max_length=50)
    request_id: str | None = None
    # Echoes back the namespace the query was SCOPED to; `None` means the recall
    # spanned every namespace in the caller's workspace. Never invent
    # `"default"` here — that string now names one category, and reporting it
    # for an unscoped recall is exactly the confusion this change removes.
    namespace: str | None = None
    tokens_used: int = 0
    token_budget: int = 600
    format: str = "raw"
    degraded: bool = False  # True when vector search failed and results are graph-only


class LearnResponse(BaseModel):
    """Response from /memory/learn — confirmation of stored action log."""

    status: str
    graph_id: str | None = None
    vector_id: str | None = None
    namespace: str = "default"
    superseded: list[str] = []
    # SP0 A2: True when a failed vector write was queued for background backfill
    backfill_queued: bool = False
    backlinks: list[dict] = []
    # Versioning fields
    memory_chain_id: str | None = None
    version: int | None = None


class StreamResponse(BaseModel):
    """Response from /memory/stream — confirmation of queued events."""

    status: str
    queued: int


class ServiceStatus(BaseModel):
    """Health status of an individual service dependency."""

    status: str
    detail: str | None = None


class HealthResponse(BaseModel):
    """Response from /health — service connectivity status."""

    status: str
    services: dict[str, ServiceStatus]
    version: str = VERSION
    uptime_seconds: float | None = None
    memory_count: int | None = None
    replay_stream_length: int | None = None
    replay_stream_utilization: float | None = None  # 0.0 to 1.0
    replay_emitter: dict | None = None  # {emitted, dropped_disabled, dropped_dedup, dropped_error, stream_length}
    # SP0 A2: backfill queue visibility — memories awaiting vector backfill
    backfill_queue_depth: int | None = None  # "memory:backfill" stream length
    backfill_dlq_depth: int | None = None    # "memory:backfill:dlq" list length


# ---------------------------------------------------------------------------
# Error & Feedback Models
# ---------------------------------------------------------------------------


class ErrorDetail(BaseModel):
    """Structured error response with machine-readable code."""

    error_code: str
    detail: str
    request_id: str | None = None
    suggestion: str | None = None


class FeedbackRequest(BaseModel):
    """Relevance feedback for recalled memories."""

    memory_ids: list[str] = Field(..., min_length=1, max_length=50)
    useful: bool
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackResponse(BaseModel):
    """Response from /memory/feedback."""

    status: str
    updated: int


class ContestedResolveRequest(BaseModel):
    """Human verdict on a contested memory pair (Knowledge Autopilot).

    'supersede' = the winner is right (and gets confirmed — a verdict IS human
    evidence); 'coexist' = both are true in their own contexts, clear the flags.
    """

    winner_id: str = Field(..., min_length=1, max_length=128)
    loser_id: str = Field(..., min_length=1, max_length=128)
    action: Literal["supersede", "coexist"] = "supersede"


class ContestedProposeRequest(BaseModel):
    """A fleet worker's PROPOSED verdict on a contested pair (Fleet-as-GPU).

    Same shape as the human verdict plus a rationale, so the two can be compared
    later (the ledger's `matched` counter). Proposing never resolves: the pair
    stays contested until a human calls /memory/contested/resolve.
    """

    winner_id: str = Field(..., min_length=1, max_length=128)
    loser_id: str = Field(..., min_length=1, max_length=128)
    action: Literal["supersede", "coexist"] = "supersede"
    rationale: str = Field(default="", max_length=1000)


# ---------------------------------------------------------------------------
# Stats & Transfer Models
# ---------------------------------------------------------------------------


class MemoryStats(BaseModel):
    """Response from /memory/stats — aggregated memory statistics."""

    total_memories: int
    graph_nodes: int
    graph_edges: int
    domains: list[str]
    top_tags: list[dict]  # [{"tag": "auth", "count": 15}, ...]
    dlq_depth: int
    oldest_memory: str | None  # ISO timestamp
    newest_memory: str | None  # ISO timestamp
    namespace_counts: dict[str, int]  # {"default": 42, "agent-1": 15}


class ImportResponse(BaseModel):
    """Response from /memory/import."""

    status: str
    imported_memories: int = 0
    imported_nodes: int = 0
    imported_edges: int = 0
    errors: list[str] = []


# ---------------------------------------------------------------------------
# Knowledge Lifecycle Models
# ---------------------------------------------------------------------------


class DeprecateRequest(BaseModel):
    """Request to change memory status."""

    memory_ids: list[str] = Field(..., min_length=1, max_length=50)
    status: Literal["deprecated", "superseded", "archived"] = Field(...)
    reason: str = Field(..., min_length=1, max_length=2000)
    superseded_by: str | None = Field(default=None)


class DeprecateResponse(BaseModel):
    """Response from /memory/deprecate."""

    status: str
    updated: int


class ConfirmRequest(BaseModel):
    """Request to confirm memories are still valid."""

    memory_ids: list[str] = Field(..., min_length=1, max_length=50)


class ConfirmResponse(BaseModel):
    """Response from /memory/confirm."""

    status: str
    confirmed: int


class RestoreRequest(BaseModel):
    """Request to bring archived memories back into circulation."""

    memory_ids: list[str] = Field(..., min_length=1, max_length=50)


class RestoreResponse(BaseModel):
    """Response from /memory/restore.

    ``restored`` counts only records that actually changed — a memory that was
    never archived is a no-op, not a failure, so the dashboard can tell the
    difference between "brought back" and "was already live".
    """

    status: str
    restored: int


class BacklinksResponse(BaseModel):
    """Response from /memory/{id}/backlinks."""

    memory_id: str
    backlinks: list[dict] = []
    total: int = 0


class MemoryHistoryResponse(BaseModel):
    """Response from /memory/{id}/history."""

    memory_id: str
    status: str
    superseded_by: dict | None = None
    supersedes: list[dict] = []
    confirmed_count: int = 0
    contradicted_count: int = 0
    last_confirmed_at: str | None = None


# ---------------------------------------------------------------------------
# Versioned Memory Models
# ---------------------------------------------------------------------------


class MemoryVersionInfo(BaseModel):
    """A single version of a memory in the version chain."""

    version: int
    memory_chain_id: str
    content: str
    confidence: float
    memory_type: str
    created_at: str
    created_by: str
    is_valid: bool = True
    is_latest: bool = False
    invalidation_reason: str | None = None
    invalidated_at: str | None = None
    invalidated_by: str | None = None


class VersionHistoryResponse(BaseModel):
    """Response from GET /memory/{chain_id}/versions."""

    memory_chain_id: str
    versions: list[MemoryVersionInfo]
    current_version: int
    total_versions: int


class InvalidateRequest(BaseModel):
    """Request to invalidate a memory."""

    reason: str = Field(..., min_length=1, max_length=2000)
    invalidated_by: str = Field(default="user", max_length=200)


class InvalidateResponse(BaseModel):
    """Response from POST /memory/{chain_id}/invalidate."""

    status: str
    memory_chain_id: str
    version_invalidated: int
    reason: str


class HandoffRequest(BaseModel):
    """Request body for POST /memory/handoff."""

    project: str = Field(..., min_length=1, max_length=200)
    since_days: int = Field(default=7, ge=1, le=365)


# ---------------------------------------------------------------------------
# Skill Synthesis
# ---------------------------------------------------------------------------


class SkillEvaluateRequest(BaseModel):
    session_id: str
    skill_worthy: bool = False


# Mirrors Settings.PROCEDURE_MAX_SPECS. A pydantic validator cannot read
# Settings (model validation would then depend on process env, and every test
# that builds a request would inherit the deployment's config), so the cap is
# stated in both places and tests/test_procedure_config.py is the drift guard.
MAX_STEP_SPECS_PER_SKILL = 50


def _cap_step_specs(v: list[StepSpec] | None) -> list[StepSpec] | None:
    if v is not None and len(v) > MAX_STEP_SPECS_PER_SKILL:
        raise ValueError(f"at most {MAX_STEP_SPECS_PER_SKILL} step_specs per skill")
    return v


class SkillRequest(BaseModel):
    trigger: str
    symptoms: str
    steps: str
    gotchas: str = ""
    domain: str = ""
    project: str | None = None
    namespace: str = "default"
    # Client-authored skills default to active (unchanged skill_create behavior);
    # a client-side knowledge-ingest flow can create them as "draft" so they land
    # in the same human-review queue as server-drafted skills (draft skills are
    # excluded from every recall path until approved via PATCH skill_status=active).
    status: Literal["active", "draft"] = "active"
    # Living Procedures: optional per-step matchers, compiled by the AUTHORING
    # agent (the server runs no LLM for this). Absent => the skill is simply not
    # an observed procedure. See docs/superpowers/specs/2026-08-06-living-procedures-design.md
    step_specs: list[StepSpec] | None = None
    # Fleet-as-GPU (spec 2026-09-02): which fleet job produced this draft, and — for
    # a stale-skill re-author — which skill it rewrites. origin_job feeds the
    # approval ledger; reauthor_of is validated server-side against the caller's
    # workspace (404 otherwise) so a worker cannot draft across a tenancy boundary.
    origin_job: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    reauthor_of: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("project", mode="before")
    @classmethod
    def _lower_project(cls, v: str | None) -> str | None:
        return v.lower() if v else v

    @field_validator("step_specs")
    @classmethod
    def _cap_specs(cls, v: list[StepSpec] | None) -> list[StepSpec] | None:
        return _cap_step_specs(v)


class SkillResponse(BaseModel):
    id: str
    trigger: str
    symptoms: str
    content: str
    skill_status: str
    skill_score: float = 0.0
    source_session_id: str | None = None
    domain: str = ""
    project: str | None = None
    agent_id: str | None = None
    namespace: str = "default"
    created_at: str | None = None
    # Provenance (SP2 docs->skills). Separate axes — do not conflate:
    #   source_type: how the draft originated (session|document|manual)
    #   content_class: what kind of content it is (procedural|reference)
    source_type: str = "session"
    content_class: str | None = None
    source_doc: str | None = None
    procedure_title: str | None = None
    needs_rereview: bool = False
    # Staleness (Task #4): the memory-agent sweep flags active skills unrecalled
    # past SKILL_STALE_AFTER_DAYS for human review; last_recalled_at feeds it.
    stale: bool = False
    stale_detected_at: str | None = None
    stale_reviewed_at: str | None = None
    last_recalled_at: str | None = None
    # Outcome truth PR3 (D2/D4): the nightly OWM pass's PARALLEL, skill-only
    # efficacy score (see app/owm.py) — never populated on skills authored or
    # scored before that pass ran, hence optional with None defaults (old
    # points parse fine, no migration). VISIBILITY ONLY: read here, never
    # ranked or mutated by this reader. `skill_efficacy_n` MUST accompany the
    # score wherever it's shown — a low-n score is mostly the neutral Beta
    # prior, not a measurement, and a reader who sees only the score has no
    # way to tell the difference.
    skill_efficacy: float | None = None
    skill_efficacy_n: int | None = None
    skill_efficacy_updated_at: str | None = None
    # Living Procedures: absent on every skill authored before this feature, and
    # on every skill whose author declined to compile specs — None, not [], so a
    # reader can tell "not a procedure" from "a procedure with no steps".
    step_specs: list[StepSpec] | None = None
    # Fleet-as-GPU: absent on every pre-existing skill (None, no migration).
    origin_job: str | None = None
    reauthor_of: str | None = None
    # A REAL approval timestamp (draft->active), stamped for every skill from this
    # release on. stale_reviewed_at is NOT this: it is also written by "still valid".
    approved_at: str | None = None


class SkillPatchRequest(BaseModel):
    skill_status: str | None = None
    content: str | None = None
    trigger: str | None = None
    symptoms: str | None = None
    needs_rereview: bool | None = None
    stale: bool | None = None
    # Replaces the whole list when present (I3: the PATCH path is the ONLY
    # writer of step_specs — every derived number lives in Redis).
    step_specs: list[StepSpec] | None = None

    @field_validator("step_specs")
    @classmethod
    def _cap_specs(cls, v: list[StepSpec] | None) -> list[StepSpec] | None:
        return _cap_step_specs(v)
