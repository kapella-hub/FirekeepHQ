"""Pydantic models for the Agent Gateway contract."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr

Decision = Literal["allow", "rethink", "block"]
Tier = Literal["auto", "lightweight", "full"]
Adapter = Literal["shell-hook", "mcp", "rest"]
ActionType = Literal["edit_file", "run_command", "call_api", "delete", "other"]

AdvisoryCode = Literal[
    "low_confidence",
    "recent_failure",
    "lease_conflict",
    "pattern_risk",
    "prediction_required",
    "wall_detected",
    "rethink_limit",
    "path_deny",
    "session_health",
    "file_risk",
    # Living Procedures. AdvisoryCode is a CLOSED Literal, so constructing an
    # Advisory with an unlisted code raises inside decide() — which is not
    # wrapped at that site, 500s the before-call, and makes the client fail open,
    # silently disabling the whole gate.
    "procedure_step_missing",
    # Enforced runbooks (round 2). `runbook_ack_required` rides a rethink and
    # carries the challenge_id (message + evidence_event_id); `runbook_blocked`
    # rides a block naming the runbook and its unmet load-bearing steps.
    "runbook_ack_required",
    "runbook_blocked",
    # The positive-evaluation receipt (review 2026-08-15): attached with an
    # EMPTY message on every verdict where runbook evaluation genuinely ran,
    # so the client's block-mode branch can tell an evaluated allow from a
    # degraded server's bare allow. Empty message = never reaches the human
    # advisory line; the code is the payload.
    "runbook_evaluated",
]


class Action(BaseModel):
    type: ActionType
    target: str
    preview: Optional[str] = Field(default=None, max_length=2048)
    # Round 2 (enforced runbooks): the working directory the client hook ran
    # the command from. AUDIT ONLY — it is persisted with the action record and
    # the pending command evidence, and never participates in matching or
    # verdicts.
    cwd: Optional[str] = Field(default=None, max_length=2048)


class Prediction(BaseModel):
    intent: str = Field(max_length=512)
    expected_changes: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class ActionBeforeRequest(BaseModel):
    session_id: str
    agent_id: str
    adapter: Adapter
    action: Action
    prediction: Optional[Prediction] = None

    # The VERIFIED principal, stamped by the REST router from the auth layer
    # AFTER validation — PrivateAttr by design: it is not part of the wire
    # schema, so no client payload can ever set it, and `agent_id` (a
    # self-reported observability label) never authorizes anything.
    _verified_workspace: str = PrivateAttr(default="")
    _verified_member: str = PrivateAttr(default="")


class Advisory(BaseModel):
    code: AdvisoryCode
    message: str
    suggested_questions: list[str] = Field(default_factory=list)
    evidence_event_id: Optional[str] = None


class ActionBeforeResponse(BaseModel):
    decision: Decision
    action_id: str
    tier: Tier
    advisories: list[Advisory] = Field(default_factory=list)
    reconcile_deadline_seconds: int = 300
    auto_reconcile: bool = False


class Outcome(BaseModel):
    success: bool
    actual_changes: list[str] = Field(default_factory=list)
    observed_criteria_met: list[str] = Field(default_factory=list)
    deviation_notes: Optional[str] = Field(default=None, max_length=512)


class ActionAfterRequest(BaseModel):
    action_id: str
    outcome: Outcome
    # Round 2 (enforced runbooks): the REAL shell exit code, sent when the
    # harness provides it. Command evidence commits ONLY on success AND
    # exit_status == 0; absent is NOT success — `success` alone no longer
    # commits command evidence (spec: "Allow is not success").
    exit_status: Optional[int] = None

    # Stamped by the REST router from the verified principal (see
    # ActionBeforeRequest) so a caller cannot settle another workspace's
    # pending evidence.
    _verified_workspace: str = PrivateAttr(default="")


class ActionAfterResponse(BaseModel):
    action_id: str
    prediction_match_score: Optional[float] = None
    recorded: bool = True
