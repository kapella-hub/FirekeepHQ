"""Pydantic models for FirekeepSentinel events."""

from typing import Literal

from pydantic import BaseModel, Field


class EventIngest(BaseModel):
    source: str = Field(..., max_length=500)
    event_type: str = Field(..., max_length=200)
    summary: str = Field(..., max_length=10000)
    details: dict = {}
    severity: Literal["info", "warning", "error", "critical"] = "info"
    tags: list[str] = []


class EventRecord(EventIngest):
    id: str
    timestamp: float
