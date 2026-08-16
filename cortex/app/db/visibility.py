"""The shared visibility filter (Docdex spec §4.4).

ONE builder, consumed by every member-principal egress (VectorClient
queries, corpus source listing). A new egress path that skips this module
is the bug class it exists to prevent. Dashboard and /memory/export are
OPERATOR surfaces by the spec's threat boundary and deliberately do not
consume it.
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
