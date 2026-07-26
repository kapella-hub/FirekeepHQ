"""Tests for the recall_used_rate scorer (N=1 learning loop, Task 4).

recall_used_rate is a v1 proxy for the north-star "is the flywheel spinning?":
a memory_read counts as "used" when any action/write event follows it in the
session. The per-session value is aggregated over the eval window and surfaced
as recall_hit_rate in the briefing discipline section.
"""
from __future__ import annotations

import pytest

from app.evals.scorers import _recall_used_rate, compute_tier1_metrics


@pytest.fixture
def events_read_then_edit() -> list[dict]:
    # a recall followed by an action → the recall was "used"
    # (an Edit surfaces as agent.action.predict via the pre_tool hook).
    return [
        {"event_type": "memory_read"},
        {"event_type": "agent.action.predict"},
    ]


@pytest.fixture
def events_read_only() -> list[dict]:
    # a recall with nothing acting on it afterwards → not used
    return [
        {"event_type": "memory_read"},
    ]


def test_recall_used_rate(events_read_then_edit, events_read_only):
    assert _recall_used_rate(events_read_then_edit) == 1.0
    assert _recall_used_rate(events_read_only) == 0.0


def test_recall_used_rate_no_reads_is_zero():
    # no memory_read at all → 0.0 (NOT None); sessions without recall pull the
    # aggregate down by design.
    assert _recall_used_rate([{"event_type": "ctx_update"}]) == 0.0
    assert _recall_used_rate([]) == 0.0


def test_recall_used_rate_partial():
    # two reads: the first is used (a write follows), the second is not (nothing
    # after it) → 0.5.
    events = [
        {"event_type": "memory_read"},
        {"event_type": "memory_write"},
        {"event_type": "memory_read"},
    ]
    assert _recall_used_rate(events) == 0.5


def test_recall_used_rate_registered_in_metrics():
    events = [
        {"event_type": "memory_read"},
        {"event_type": "memory_write"},
    ]
    metrics = compute_tier1_metrics(events)
    assert metrics["recall_used_rate"] == 1.0
