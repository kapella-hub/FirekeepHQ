"""Pydantic models for the Agent Gateway contract."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

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
]


class Action(BaseModel):
    type: ActionType
    target: str
    preview: Optional[str] = Field(default=None, max_length=2048)


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


class ActionAfterResponse(BaseModel):
    action_id: str
    prediction_match_score: Optional[float] = None
    recorded: bool = True
