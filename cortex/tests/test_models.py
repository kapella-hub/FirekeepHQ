"""Tests for Pydantic request/response models."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models import (
    ActionLog,
    ConfirmRequest,
    ConfirmResponse,
    ContextQuery,
    DeprecateRequest,
    DeprecateResponse,
    GenericEventIngest,
    HealthResponse,
    LearnResponse,
    MemoryHistoryResponse,
    MemorySource,
    RecallResponse,
    ServiceStatus,
    StreamResponse,
)


# ---------------------------------------------------------------------------
# ContextQuery
# ---------------------------------------------------------------------------


class TestContextQuery:
    def test_valid_creation(self):
        q = ContextQuery(task="Fix login bug")
        assert q.task == "Fix login bug"

    def test_default_values(self):
        q = ContextQuery(task="anything")
        assert q.tags == []
        assert q.top_k == 5
        # None, not "default": an unspecified namespace searches every
        # namespace in the caller's workspace, while the literal "default"
        # names one category. See db/vector.namespace_condition.
        assert q.namespace is None

    def test_custom_values(self):
        q = ContextQuery(task="search", tags=["auth", "db"], top_k=10)
        assert q.tags == ["auth", "db"]
        assert q.top_k == 10

    def test_serialization_roundtrip(self):
        q = ContextQuery(task="Fix login bug", tags=["auth"], top_k=3)
        data = q.model_dump()
        assert data == {"task": "Fix login bug", "tags": ["auth"], "top_k": 3, "namespace": None, "include_archived": False, "project": None, "token_budget": 600, "format": "synthesized", "trigger": None}
        q2 = ContextQuery.model_validate(data)
        assert q2 == q

    def test_json_roundtrip(self):
        q = ContextQuery(task="test", tags=["a"])
        json_str = q.model_dump_json()
        q2 = ContextQuery.model_validate_json(json_str)
        assert q2 == q

    def test_missing_required_field(self):
        with pytest.raises(ValidationError):
            ContextQuery()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ActionLog
# ---------------------------------------------------------------------------


class TestActionLog:
    def test_valid_creation(self):
        log = ActionLog(action="increase pool", outcome="timeout resolved")
        assert log.action == "increase pool"
        assert log.outcome == "timeout resolved"

    def test_with_resolution(self):
        log = ActionLog(
            action="a",
            outcome="o",
            resolution="Updated config",
        )
        assert log.resolution == "Updated config"

    def test_without_resolution(self):
        log = ActionLog(action="a", outcome="o")
        assert log.resolution is None

    def test_default_domain(self):
        log = ActionLog(action="a", outcome="o")
        assert log.domain == "general"

    def test_custom_tags_and_domain(self):
        log = ActionLog(action="a", outcome="o", tags=["db"], domain="infra")
        assert log.tags == ["db"]
        assert log.domain == "infra"

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            ActionLog(action="a")  # type: ignore[call-arg]

    def test_serialization(self):
        log = ActionLog(action="a", outcome="o", tags=["x"], domain="test")
        data = log.model_dump()
        assert data["tags"] == ["x"]
        assert data["domain"] == "test"
        assert data["resolution"] is None


# ---------------------------------------------------------------------------
# GenericEventIngest
# ---------------------------------------------------------------------------


class TestGenericEventIngest:
    def test_auto_timestamp_when_none(self):
        before = datetime.now(timezone.utc)
        event = GenericEventIngest(source="ci", payload={"build": "1"})
        after = datetime.now(timezone.utc)
        assert event.timestamp is not None
        assert before <= event.timestamp <= after

    def test_explicit_timestamp_preserved(self):
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = GenericEventIngest(source="ci", payload={}, timestamp=ts)
        assert event.timestamp == ts

    def test_payload_handling(self):
        payload = {"nested": {"key": [1, 2, 3]}, "simple": "val"}
        event = GenericEventIngest(source="test", payload=payload)
        assert event.payload == payload

    def test_empty_payload(self):
        event = GenericEventIngest(source="s", payload={})
        assert event.payload == {}

    def test_default_tags(self):
        event = GenericEventIngest(source="s", payload={})
        assert event.tags == []

    def test_custom_tags(self):
        event = GenericEventIngest(
            source="s", payload={}, tags=["ci", "failure"]
        )
        assert event.tags == ["ci", "failure"]

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            GenericEventIngest(source="s")  # type: ignore[call-arg]

    def test_json_roundtrip(self):
        event = GenericEventIngest(
            source="ci",
            payload={"a": 1},
            tags=["x"],
        )
        json_str = event.model_dump_json()
        restored = GenericEventIngest.model_validate_json(json_str)
        assert restored.source == event.source
        assert restored.payload == event.payload
        assert restored.tags == event.tags


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------


class TestMemorySource:
    def test_valid_stores(self):
        for store in ("graph", "vector", "both"):
            ms = MemorySource(store=store, content="test", score=0.9)
            assert ms.store == store

    def test_invalid_store(self):
        with pytest.raises(ValidationError):
            MemorySource(store="invalid", content="test", score=0.5)

    def test_default_metadata(self):
        ms = MemorySource(store="graph", content="c", score=0.5)
        assert ms.metadata == {}

    def test_custom_metadata(self):
        ms = MemorySource(
            store="vector",
            content="c",
            score=0.8,
            metadata={"source": "ci"},
        )
        assert ms.metadata == {"source": "ci"}


class TestRecallResponse:
    def test_valid_creation(self):
        rr = RecallResponse(context_block="## Memory", sources=[], score=0.0)
        assert rr.context_block == "## Memory"
        assert rr.sources == []
        assert rr.score == 0.0
        assert rr.memory_ids == []

    def test_with_sources(self):
        src = MemorySource(store="graph", content="test", score=0.9)
        rr = RecallResponse(context_block="block", sources=[src], score=0.9)
        assert len(rr.sources) == 1
        assert rr.sources[0].score == 0.9


class TestLearnResponse:
    def test_with_ids(self):
        lr = LearnResponse(status="stored", graph_id="g1", vector_id="v1")
        assert lr.graph_id == "g1"
        assert lr.vector_id == "v1"

    def test_without_ids(self):
        lr = LearnResponse(status="stored")
        assert lr.graph_id is None
        assert lr.vector_id is None


class TestStreamResponse:
    def test_valid(self):
        sr = StreamResponse(status="queued", queued=5)
        assert sr.status == "queued"
        assert sr.queued == 5


class TestServiceStatus:
    def test_connected(self):
        s = ServiceStatus(status="connected")
        assert s.status == "connected"
        assert s.detail is None

    def test_disconnected_with_detail(self):
        s = ServiceStatus(status="disconnected", detail="connection refused")
        assert s.status == "disconnected"
        assert s.detail == "connection refused"


class TestHealthResponse:
    def test_ok_status(self):
        hr = HealthResponse(
            status="ok",
            services={
                "redis": ServiceStatus(status="connected"),
                "graph": ServiceStatus(status="connected"),
            },
        )
        assert hr.status == "ok"
        assert hr.services["redis"].status == "connected"

    def test_degraded_status(self):
        hr = HealthResponse(
            status="degraded",
            services={
                "redis": ServiceStatus(status="connected"),
                "graph": ServiceStatus(status="disconnected", detail="Neo4j down"),
            },
        )
        assert hr.status == "degraded"
        assert hr.services["graph"].detail == "Neo4j down"


# ---------------------------------------------------------------------------
# Field Validation (max_length / max items constraints)
# ---------------------------------------------------------------------------


class TestFieldValidation:
    def test_context_query_task_too_long(self):
        """task > 2000 chars should be rejected."""
        with pytest.raises(ValidationError):
            ContextQuery(task="x" * 2001)

    def test_context_query_task_at_max(self):
        """task exactly 2000 chars should be accepted."""
        q = ContextQuery(task="x" * 2000)
        assert len(q.task) == 2000

    def test_context_query_task_empty(self):
        """Empty task should be rejected (min_length=1)."""
        with pytest.raises(ValidationError):
            ContextQuery(task="")

    def test_action_log_action_too_long(self):
        """action > 5000 chars should be rejected."""
        with pytest.raises(ValidationError):
            ActionLog(action="x" * 5001, outcome="ok")

    def test_action_log_outcome_too_long(self):
        """outcome > 5000 chars should be rejected."""
        with pytest.raises(ValidationError):
            ActionLog(action="ok", outcome="x" * 5001)

    def test_action_log_resolution_too_long(self):
        """resolution > 5000 chars should be rejected."""
        with pytest.raises(ValidationError):
            ActionLog(action="ok", outcome="ok", resolution="x" * 5001)

    def test_action_log_domain_too_long(self):
        """domain > 200 chars should be rejected."""
        with pytest.raises(ValidationError):
            ActionLog(action="ok", outcome="ok", domain="x" * 201)

    def test_action_log_domain_at_max(self):
        """domain exactly 200 chars should be accepted."""
        log = ActionLog(action="ok", outcome="ok", domain="x" * 200)
        assert len(log.domain) == 200

    def test_tags_list_too_many_items(self):
        """tags list > 20 items should be rejected."""
        with pytest.raises(ValidationError):
            ContextQuery(task="test", tags=["tag"] * 21)

    def test_tags_list_at_max(self):
        """tags list with exactly 20 items should be accepted."""
        q = ContextQuery(task="test", tags=[f"tag{i}" for i in range(20)])
        assert len(q.tags) == 20

    def test_action_log_tags_too_many(self):
        """ActionLog tags > 20 items should be rejected."""
        with pytest.raises(ValidationError):
            ActionLog(action="ok", outcome="ok", tags=["t"] * 21)

    def test_individual_tag_too_long(self):
        """A single tag > 100 chars should be rejected."""
        with pytest.raises(ValidationError):
            ContextQuery(task="test", tags=["x" * 101])

    def test_individual_tag_at_max(self):
        """A single tag exactly 100 chars should be accepted."""
        q = ContextQuery(task="test", tags=["x" * 100])
        assert len(q.tags[0]) == 100

    def test_event_ingest_source_too_long(self):
        """source > 500 chars should be rejected."""
        with pytest.raises(ValidationError):
            GenericEventIngest(source="x" * 501, payload={})

    def test_event_ingest_source_at_max(self):
        """source exactly 500 chars should be accepted."""
        e = GenericEventIngest(source="x" * 500, payload={})
        assert len(e.source) == 500

    def test_event_ingest_source_empty(self):
        """Empty source should be rejected (min_length=1)."""
        with pytest.raises(ValidationError):
            GenericEventIngest(source="", payload={})

    def test_event_ingest_tags_too_many(self):
        """GenericEventIngest tags > 20 should be rejected."""
        with pytest.raises(ValidationError):
            GenericEventIngest(source="s", payload={}, tags=["t"] * 21)

    def test_event_ingest_individual_tag_too_long(self):
        """GenericEventIngest individual tag > 100 chars should be rejected."""
        with pytest.raises(ValidationError):
            GenericEventIngest(source="s", payload={}, tags=["x" * 101])


# ---------------------------------------------------------------------------
# Namespace field validation
# ---------------------------------------------------------------------------


class TestNamespaceValidation:
    def test_context_query_default_namespace(self):
        """ContextQuery namespace defaults to None — every namespace.

        The recall model is the ONE place where "default" was overloaded to
        mean both "the default category" and "no scope at all". Splitting them
        is what lets an unscoped recall reach the 146 non-default memories the
        product's own guidance creates.
        """
        q = ContextQuery(task="test")
        assert q.namespace is None

    def test_action_log_default_namespace(self):
        """ActionLog namespace defaults to 'default'."""
        log = ActionLog(action="a", outcome="o")
        assert log.namespace == "default"

    def test_event_ingest_default_namespace(self):
        """GenericEventIngest namespace defaults to 'default'."""
        event = GenericEventIngest(source="s", payload={})
        assert event.namespace == "default"

    def test_recall_response_default_namespace(self):
        """RecallResponse echoes None for a recall that named no namespace.

        Reporting "default" for an unscoped recall would be the same false
        echo the pre-fix vector filter produced.
        """
        rr = RecallResponse(context_block="block", sources=[], score=0.0)
        assert rr.namespace is None

    def test_learn_response_default_namespace(self):
        """LearnResponse namespace defaults to 'default'."""
        lr = LearnResponse(status="stored")
        assert lr.namespace == "default"

    def test_valid_namespace_alphanumeric(self):
        """Alphanumeric namespace should be accepted."""
        q = ContextQuery(task="test", namespace="agent1")
        assert q.namespace == "agent1"

    def test_valid_namespace_with_hyphens(self):
        """Namespace with hyphens should be accepted."""
        q = ContextQuery(task="test", namespace="my-agent-1")
        assert q.namespace == "my_agent_1"

    def test_valid_namespace_with_underscores(self):
        """Namespace with underscores should be accepted."""
        q = ContextQuery(task="test", namespace="my_agent_1")
        assert q.namespace == "my_agent_1"

    def test_namespace_rejects_spaces(self):
        """Namespace with spaces should be rejected."""
        with pytest.raises(ValidationError):
            ContextQuery(task="test", namespace="my agent")

    def test_namespace_rejects_special_chars(self):
        """Namespace with special characters should be rejected."""
        for invalid in ["agent@1", "ns/path", "ns.dot", "ns!bang", "ns#hash"]:
            with pytest.raises(ValidationError):
                ContextQuery(task="test", namespace=invalid)

    def test_namespace_rejects_empty(self):
        """Empty namespace should be rejected (min_length=1)."""
        with pytest.raises(ValidationError):
            ContextQuery(task="test", namespace="")

    def test_namespace_rejects_too_long(self):
        """Namespace > 200 chars should be rejected."""
        with pytest.raises(ValidationError):
            ContextQuery(task="test", namespace="x" * 201)

    def test_namespace_at_max_length(self):
        """Namespace exactly 200 chars should be accepted."""
        q = ContextQuery(task="test", namespace="x" * 200)
        assert len(q.namespace) == 200

    def test_namespace_on_all_request_models(self):
        """All request models should accept custom namespaces."""
        q = ContextQuery(task="test", namespace="tenant-A")
        assert q.namespace == "tenant_a"

        log = ActionLog(action="a", outcome="o", namespace="tenant-B")
        assert log.namespace == "tenant_b"

        event = GenericEventIngest(source="s", payload={}, namespace="tenant-C")
        assert event.namespace == "tenant_c"

    def test_recall_response_custom_namespace(self):
        """RecallResponse should accept custom namespace."""
        rr = RecallResponse(
            context_block="block", sources=[], score=0.0, namespace="agent-1"
        )
        assert rr.namespace == "agent-1"

    def test_learn_response_custom_namespace(self):
        """LearnResponse should accept custom namespace."""
        lr = LearnResponse(status="stored", namespace="agent-2")
        assert lr.namespace == "agent-2"

    def test_namespace_hyphens_normalized_to_underscores(self):
        """Hyphens in namespace should be normalized to underscores."""
        q = ContextQuery(task="test", namespace="automation-portal")
        assert q.namespace == "automation_portal"

    def test_namespace_uppercase_normalized_to_lowercase(self):
        """Uppercase namespace should be normalized to lowercase."""
        q = ContextQuery(task="test", namespace="AutomationPortal")
        assert q.namespace == "automationportal"

    def test_namespace_mixed_case_hyphens_normalized(self):
        """Mixed case with hyphens should be fully normalized."""
        q = ContextQuery(task="test", namespace="My-Agent-1")
        assert q.namespace == "my_agent_1"

    def test_namespace_normalization_on_action_log(self):
        """ActionLog namespace should be normalized."""
        log = ActionLog(action="a", outcome="o", namespace="Tenant-B")
        assert log.namespace == "tenant_b"

    def test_namespace_normalization_on_event_ingest(self):
        """GenericEventIngest namespace should be normalized."""
        event = GenericEventIngest(source="s", payload={}, namespace="Tenant-C")
        assert event.namespace == "tenant_c"

    def test_namespace_default_unchanged(self):
        """An EXPLICIT 'default' passes through unchanged; omitting it is None."""
        assert ContextQuery(task="test", namespace="default").namespace == "default"
        assert ContextQuery(task="test").namespace is None

    def test_namespace_already_normalized_unchanged(self):
        """Already normalized namespace should pass through unchanged."""
        q = ContextQuery(task="test", namespace="my_agent_1")
        assert q.namespace == "my_agent_1"


# ---------------------------------------------------------------------------
# ContextQuery include_archived
# ---------------------------------------------------------------------------


class TestContextQueryIncludeArchived:
    def test_default_false(self):
        q = ContextQuery(task="test")
        assert q.include_archived is False

    def test_set_true(self):
        q = ContextQuery(task="test", include_archived=True)
        assert q.include_archived is True


# ---------------------------------------------------------------------------
# LearnResponse superseded
# ---------------------------------------------------------------------------


class TestLearnResponseSuperseded:
    def test_default_empty(self):
        lr = LearnResponse(status="stored")
        assert lr.superseded == []

    def test_with_superseded_ids(self):
        lr = LearnResponse(status="stored", superseded=["id1", "id2"])
        assert lr.superseded == ["id1", "id2"]


# ---------------------------------------------------------------------------
# DeprecateRequest
# ---------------------------------------------------------------------------


class TestDeprecateRequest:
    def test_valid_creation(self):
        req = DeprecateRequest(
            memory_ids=["m1", "m2"],
            status="deprecated",
            reason="Outdated information",
        )
        assert req.memory_ids == ["m1", "m2"]
        assert req.status == "deprecated"
        assert req.reason == "Outdated information"
        assert req.superseded_by is None

    def test_superseded_status_with_superseded_by(self):
        req = DeprecateRequest(
            memory_ids=["m1"],
            status="superseded",
            reason="Replaced by newer version",
            superseded_by="m2",
        )
        assert req.status == "superseded"
        assert req.superseded_by == "m2"

    def test_archived_status(self):
        req = DeprecateRequest(
            memory_ids=["m1"],
            status="archived",
            reason="No longer relevant",
        )
        assert req.status == "archived"

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            DeprecateRequest(
                memory_ids=["m1"],
                status="invalid",
                reason="test",
            )

    def test_empty_memory_ids_rejected(self):
        with pytest.raises(ValidationError):
            DeprecateRequest(
                memory_ids=[],
                status="deprecated",
                reason="test",
            )

    def test_too_many_memory_ids_rejected(self):
        with pytest.raises(ValidationError):
            DeprecateRequest(
                memory_ids=[f"m{i}" for i in range(51)],
                status="deprecated",
                reason="test",
            )

    def test_empty_reason_rejected(self):
        with pytest.raises(ValidationError):
            DeprecateRequest(
                memory_ids=["m1"],
                status="deprecated",
                reason="",
            )

    def test_reason_too_long_rejected(self):
        with pytest.raises(ValidationError):
            DeprecateRequest(
                memory_ids=["m1"],
                status="deprecated",
                reason="x" * 2001,
            )


# ---------------------------------------------------------------------------
# ConfirmRequest
# ---------------------------------------------------------------------------


class TestConfirmRequest:
    def test_valid_creation(self):
        req = ConfirmRequest(memory_ids=["m1", "m2"])
        assert req.memory_ids == ["m1", "m2"]

    def test_empty_memory_ids_rejected(self):
        with pytest.raises(ValidationError):
            ConfirmRequest(memory_ids=[])

    def test_too_many_memory_ids_rejected(self):
        with pytest.raises(ValidationError):
            ConfirmRequest(memory_ids=[f"m{i}" for i in range(51)])


# ---------------------------------------------------------------------------
# MemoryHistoryResponse
# ---------------------------------------------------------------------------


class TestMemoryHistoryResponse:
    def test_minimal_creation(self):
        resp = MemoryHistoryResponse(
            memory_id="m1",
            status="active",
        )
        assert resp.memory_id == "m1"
        assert resp.status == "active"
        assert resp.superseded_by is None
        assert resp.supersedes == []
        assert resp.confirmed_count == 0
        assert resp.contradicted_count == 0
        assert resp.last_confirmed_at is None

    def test_full_creation(self):
        resp = MemoryHistoryResponse(
            memory_id="m1",
            status="superseded",
            superseded_by={"id": "m2", "text": "newer version"},
            supersedes=[{"id": "m0", "text": "older version"}],
            confirmed_count=5,
            contradicted_count=2,
            last_confirmed_at="2026-03-05T12:00:00+00:00",
        )
        assert resp.superseded_by == {"id": "m2", "text": "newer version"}
        assert len(resp.supersedes) == 1
        assert resp.confirmed_count == 5
        assert resp.contradicted_count == 2
        assert resp.last_confirmed_at == "2026-03-05T12:00:00+00:00"


# ---------------------------------------------------------------------------
# DeprecateResponse / ConfirmResponse
# ---------------------------------------------------------------------------


class TestDeprecateResponse:
    def test_valid(self):
        resp = DeprecateResponse(status="ok", updated=3)
        assert resp.status == "ok"
        assert resp.updated == 3


class TestConfirmResponse:
    def test_valid(self):
        resp = ConfirmResponse(status="ok", confirmed=2)
        assert resp.status == "ok"
        assert resp.confirmed == 2
