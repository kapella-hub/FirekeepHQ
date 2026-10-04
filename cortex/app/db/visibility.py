"""The shared visibility filter (Docdex spec §4.4).

ONE builder, consumed by every member-principal egress (VectorClient
queries, corpus source listing). A new egress path that skips this module
is the bug class it exists to prevent. Dashboard and /memory/export are
OPERATOR surfaces by the spec's threat boundary and deliberately do not
consume it.

The workspace helpers below are the same idea one level up: the tenancy
boundary for routes that scroll Qdrant directly instead of going through
VectorClient.search (GET /skills, the briefing's skills section,
/memory/contributors, /memory/feedback).
"""
from __future__ import annotations

from qdrant_client.models import (
    FieldCondition,
    Filter,
    IsEmptyCondition,
    MatchValue,
    PayloadField,
)

# Task 6 wires this as a must_not on recall: chunks written but never
# committed (mid-ingest failure) are invisible until the next successful
# ingest sweeps them. Absent field passes — every pre-Phase-V point.
GENERATION_GUARD = FieldCondition(key="committed", match=MatchValue(value=False))


def workspace_condition(workspace_id: str):
    """A `must` entry confining a scroll/query to one caller's workspace.

    A point with NO recorded workspace predates attribution and belongs to the
    deployment's own workspace -- the rule workspace_migration.backfill_memories
    applies at startup and skills/api.py `_load_owned_skill` applies per id. So
    the deployment workspace gets an `IsEmpty` arm (a point written before the
    next backfill is not silently dropped from the dashboard's review queue)
    and any other workspace gets the plain match.
    """
    from auth.principal import deployment_workspace_id

    match = FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id))
    if workspace_id == deployment_workspace_id():
        return Filter(should=[
            match, IsEmptyCondition(is_empty=PayloadField(key="workspace_id")),
        ])
    return match


def payload_in_workspace(payload: dict, workspace_id: str | None) -> bool:
    """The python-side twin of `workspace_condition`, for a retrieved point."""
    from auth.principal import deployment_workspace_id

    return (payload.get("workspace_id") or deployment_workspace_id()) == workspace_id


def payload_visible_to_member(payload: dict, member_id: str | None) -> bool:
    """The python-side twin of `visibility_should` (fail closed on no member)."""
    if payload.get("visibility") != "member":
        return True
    return bool(member_id) and payload.get("member_id") == member_id


def visibility_should(member_id: str | None) -> list:
    """Conditions for a `should` group: legacy OR workspace OR own-private.

    member_id None/"" omits the private branch entirely — a caller with
    no member identity sees no private chunks (fail closed, spec I1).
    """
    conds: list = [
        IsEmptyCondition(is_empty=PayloadField(key="visibility")),
        FieldCondition(key="visibility", match=MatchValue(value="workspace")),
    ]
    if member_id:
        conds.append(Filter(must=[
            FieldCondition(key="visibility", match=MatchValue(value="member")),
            FieldCondition(key="member_id", match=MatchValue(value=member_id)),
        ]))
    return conds
