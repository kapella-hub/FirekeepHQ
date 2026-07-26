"""Tests for replay event models."""

import pytest
from pydantic import ValidationError

from replay.models import TraceEvent, TraceLink


class TestTraceLink:
    def test_valid_link(self):
        link = TraceLink(
            target_event_id="abc123",
            link_type="observed",
            relationship="informed_by",
            confidence=0.8,
        )
        assert link.target_event_id == "abc123"
        assert link.link_type == "observed"
        assert link.confidence == 0.8

    def test_default_confidence(self):
        link = TraceLink(
            target_event_id="abc",
            link_type="declared",
            relationship="triggered",
        )
        assert link.confidence == 1.0

    def test_invalid_link_type(self):
        with pytest.raises(ValidationError):
            TraceLink(
                target_event_id="abc",
                link_type="causal",  # NOT a valid type — intentionally
                relationship="triggered",
            )

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            TraceLink(
                target_event_id="abc",
                link_type="observed",
                relationship="preceded",
                confidence=1.5,
            )
        with pytest.raises(ValidationError):
            TraceLink(
                target_event_id="abc",
                link_type="observed",
                relationship="preceded",
                confidence=-0.1,
            )

    def test_empty_target_rejected(self):
        with pytest.raises(ValidationError):
            TraceLink(
                target_event_id="",
                link_type="observed",
                relationship="preceded",
            )


class TestTraceEvent:
    def test_minimal_event(self):
        event = TraceEvent(
            session_id="sess-123",
            agent_id="default",
            event_type="memory_read",
        )
        assert event.session_id == "sess-123"
        assert event.event_type == "memory_read"
        assert event.schema_version == 1
        assert event.namespace == "default"
        assert event.trace_links == []
        assert event.payload == {}
        assert event.outcome is None
        assert event.context_ref is None
        assert len(event.id) == 32  # UUID4 hex

    def test_auto_generated_ids(self):
        e1 = TraceEvent(session_id="s", agent_id="a", event_type="claim")
        e2 = TraceEvent(session_id="s", agent_id="a", event_type="claim")
        assert e1.id != e2.id
        assert e1.trace_id != e2.trace_id
        assert e1.span_id != e2.span_id

    def test_explicit_trace_span_ids(self):
        event = TraceEvent(
            session_id="s",
            agent_id="a",
            event_type="memory_write",
            trace_id="trace-1",
            span_id="span-1",
            parent_span_id="span-0",
        )
        assert event.trace_id == "trace-1"
        assert event.span_id == "span-1"
        assert event.parent_span_id == "span-0"

    def test_namespace_normalization(self):
        event = TraceEvent(
            session_id="s",
            agent_id="a",
            event_type="ctx_update",
            namespace="My-Namespace",
        )
        assert event.namespace == "my_namespace"

    def test_all_event_types(self):
        valid_types = [
            "session_start", "session_end", "memory_read", "memory_write",
            "ctx_update", "env_change", "claim", "release",
            "coordination", "webhook",
        ]
        for et in valid_types:
            event = TraceEvent(session_id="s", agent_id="a", event_type=et)
            assert event.event_type == et

    def test_invalid_event_type(self):
        with pytest.raises(ValidationError):
            TraceEvent(session_id="s", agent_id="a", event_type="unknown_type")

    def test_payload_size_limit(self):
        # 50KB payload should be rejected
        big_payload = {"data": "x" * 60_000}
        with pytest.raises(ValidationError, match="maximum size"):
            TraceEvent(
                session_id="s",
                agent_id="a",
                event_type="memory_read",
                payload=big_payload,
            )

    def test_normal_payload_accepted(self):
        event = TraceEvent(
            session_id="s",
            agent_id="a",
            event_type="memory_read",
            payload={"query": "auth bug", "result_count": 3, "top_score": 0.95},
        )
        assert event.payload["result_count"] == 3

    def test_with_trace_links(self):
        event = TraceEvent(
            session_id="s",
            agent_id="a",
            event_type="memory_write",
            trace_links=[
                TraceLink(
                    target_event_id="prev-event",
                    link_type="declared",
                    relationship="informed_by",
                    confidence=0.9,
                ),
            ],
        )
        assert len(event.trace_links) == 1
        assert event.trace_links[0].link_type == "declared"

    def test_outcome_values(self):
        for outcome in ("success", "failure", "partial"):
            event = TraceEvent(
                session_id="s", agent_id="a",
                event_type="claim", outcome=outcome,
            )
            assert event.outcome == outcome

    def test_invalid_outcome(self):
        with pytest.raises(ValidationError):
            TraceEvent(
                session_id="s", agent_id="a",
                event_type="claim", outcome="maybe",
            )

    def test_tags_limit(self):
        event = TraceEvent(
            session_id="s", agent_id="a",
            event_type="webhook",
            tags=["tag1", "tag2", "tag3"],
        )
        assert len(event.tags) == 3

    def test_idempotency_key(self):
        event = TraceEvent(
            session_id="s", agent_id="a",
            event_type="memory_write",
            idempotency_key="learn-abc-123",
        )
        assert event.idempotency_key == "learn-abc-123"

    def test_timestamp_auto_generated(self):
        event = TraceEvent(session_id="s", agent_id="a", event_type="claim")
        assert event.timestamp is not None
