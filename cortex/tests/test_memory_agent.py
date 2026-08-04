"""Tests for Memory Agent worker (app.workers.memory_agent)."""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.workers.memory_agent import (
    AGENT_LOCK_KEY,
    _cosine_similarity,
    cluster_coherence_pass,
    deep_contradiction_pass,
    duplicate_detection_pass,
    orphan_cleanup_pass,
    run_memory_agent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides):
    """Create a mock settings object with agent defaults."""
    defaults = {
        "QDRANT_HOST": "localhost",
        "QDRANT_PORT": 6333,
        "QDRANT_COLLECTION": "firekeep_memories",
        "REDIS_URL": "redis://localhost:6379",
        "REDIS_STREAM_KEY": "firekeep:event_stream",
        "LLM_BASE_URL": "http://localhost:11434/v1",
        "LLM_MODEL": "test-model",
        "LLM_API_KEY": "test-key",
        "AGENT_ENABLED": True,
        "AGENT_SCHEDULE_HOURS": 6,
        "AGENT_DUPLICATE_THRESHOLD": 0.9,
        "DEDUP_SIMILARITY_THRESHOLD": 0.78,
        "DEDUP_ENABLED": True,
        "EMBEDDING_MODEL": "test-embed",
        "AGENT_BATCH_LIMIT": 100,
        "GC_PURGE_ENABLED": False,
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


def _make_point(pid, payload=None, vector=None, score=None):
    """Create a mock Qdrant point."""
    p = MagicMock()
    p.id = pid
    p.payload = payload or {}
    p.vector = vector or [0.1, 0.2, 0.3]
    if score is not None:
        p.score = score
    return p


def _make_query_result(points):
    """Create a mock QueryResponse with .points attribute."""
    result = MagicMock()
    result.points = points
    return result


# ---------------------------------------------------------------------------
# Test 1: Duplicate Detection & Merge
# ---------------------------------------------------------------------------


def _fake_merge_lifecycle(existing, fresh):
    """Contract-faithful stand-in for app.db.vector._merge_lifecycle so these
    tests do not depend on A3's internals."""
    if not existing:
        return dict(fresh)
    merged = dict(fresh)
    for key in ("created_at", "agent_id", "project"):
        if existing.get(key) is not None:
            merged[key] = existing[key]
    merged["confirmed_count"] = max(
        existing.get("confirmed_count", 0) or 0, fresh.get("confirmed_count", 0) or 0
    )
    return merged


class TestDuplicateDetection:
    @patch("app.workers.memory_agent._merge_lifecycle", side_effect=_fake_merge_lifecycle)
    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_llm_merge_reembeds_and_rekeys(
        self, mock_settings, mock_qdrant, mock_neo4j, mock_webhook, mock_ml
    ):
        """LLM path: merged text is re-embedded, upserted under uuid5(merged_text),
        and ALL cluster members (keeper included) are superseded by the new id."""
        settings = _make_settings()
        mock_settings.return_value = settings

        client = MagicMock()
        mock_qdrant.return_value = client

        # confirmed_count is 0 on both members deliberately. A human-confirmed
        # memory can no longer reach this pass at all — `_dedup_scope_filter`
        # excludes it — so seeding one here would describe a state production
        # cannot produce. The lifecycle fold is asserted below on created_at
        # instead; see tests/test_memory_agent_confirmed.py for the guard.
        p1 = _make_point("id-1", {"text": "Use Postgres for storage", "status": "active",
                                    "domain": "db", "tags": [], "confirmed_count": 0,
                                    "contradicted_count": 0, "timestamp": "2026-01-01T00:00:00+00:00",
                                    "created_at": "2025-06-01T00:00:00+00:00"},
                         [0.9, 0.1, 0.0])
        p2 = _make_point("id-2", {"text": "Postgres is the storage backend", "status": "active",
                                    "domain": "db", "tags": [], "confirmed_count": 0,
                                    "contradicted_count": 0, "timestamp": "2026-02-01T00:00:00+00:00"},
                         [0.89, 0.11, 0.01])
        client.scroll.return_value = ([p1, p2], None)
        sp2 = _make_point("id-2", score=0.95)
        client.query_points.return_value = _make_query_result([sp2])

        merged_text = "Use Postgres as the primary storage backend"
        from app.db.vector import FIREKEEP_UUID_NAMESPACE
        expected_id = str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, merged_text))

        with patch("app.workers.memory_agent.httpx.post") as mock_httpx:
            llm_resp = MagicMock()
            llm_resp.raise_for_status = MagicMock()
            llm_resp.json.return_value = {
                "choices": [{"message": {"content": json.dumps({
                    "text": merged_text, "domain": "db", "tags": ["postgres"]
                })}}]
            }
            embed_resp = MagicMock()
            embed_resp.raise_for_status = MagicMock()
            embed_resp.json.return_value = {"data": [{"embedding": [0.5] * 3}]}
            mock_httpx.side_effect = [llm_resp, embed_resp]  # 1st call = LLM merge, 2nd = re-embed

            mock_session = MagicMock()
            mock_neo4j.return_value.session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_neo4j.return_value.session.return_value.__exit__ = MagicMock(return_value=False)

            result = duplicate_detection_pass()

        assert result["merged"] == 1
        detail = result["details"][0]
        assert detail["method"] == "llm"
        assert detail["merged_into"] == expected_id
        assert sorted(detail["superseded"]) == ["id-1", "id-2"]

        # New point upserted with the re-embedded vector under the new id
        upsert_call = client.upsert.call_args
        point = upsert_call.kwargs["points"][0]
        assert str(point.id) == expected_id
        assert point.vector == [0.5] * 3
        assert point.payload["text"] == merged_text
        # Lifecycle is folded, not discarded: the EARLIEST member's created_at
        # survives (folding runs newest-first, so the oldest folds last).
        # This assertion used to read `confirmed_count == 1  # max of cluster`,
        # which documented the confirmed-memory hole as correct behaviour —
        # the merged point inheriting a human's confirmation onto LLM-written
        # text. That input is now excluded from the pass.
        assert point.payload["created_at"] == "2025-06-01T00:00:00+00:00"
        assert point.payload["status"] == "active"

        # Both old points superseded by the new id
        for old_id in ("id-1", "id-2"):
            client.set_payload.assert_any_call(
                collection_name="firekeep_memories",
                payload={"status": "superseded", "superseded_by": expected_id},
                points=[old_id],
            )
        # The old corrupting behavior — bare text overwrite — must be gone
        for c in client.set_payload.call_args_list:
            assert set(c.kwargs["payload"].keys()) != {"text"}

    @patch("app.workers.memory_agent._merge_lifecycle", side_effect=_fake_merge_lifecycle)
    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_fallback_keeps_keeper_without_reembed(
        self, mock_settings, mock_qdrant, mock_neo4j, mock_webhook, mock_ml
    ):
        """Fallback path: merged text == keeper text, so no re-embed/re-key —
        only losers are superseded, pointing at the existing keeper."""
        settings = _make_settings()
        mock_settings.return_value = settings

        client = MagicMock()
        mock_qdrant.return_value = client

        # The keeper is differentiated by contradicted_count, not by
        # confirmed_count: a confirmed memory is out of this pass's scope
        # entirely (`_dedup_scope_filter`), so it can no longer be the member
        # that wins the confidence comparison. Confidence here is
        # (1+0)/(1+2)=0.33 for id-1 against (1+0)/(1+0)=1.0 for id-2.
        p1 = _make_point("id-1", {"text": "Short", "status": "active", "domain": "db",
                                    "tags": [], "confirmed_count": 0, "contradicted_count": 2},
                         [0.9, 0.1, 0.0])
        p2 = _make_point("id-2", {"text": "Better memory with more detail", "status": "active",
                                    "domain": "db", "tags": [], "confirmed_count": 0,
                                    "contradicted_count": 0},
                         [0.89, 0.11, 0.01])
        client.scroll.return_value = ([p1, p2], None)
        sp2 = _make_point("id-2", score=0.95)
        client.query_points.return_value = _make_query_result([sp2])

        with patch("app.workers.memory_agent.httpx.post") as mock_httpx:
            mock_httpx.side_effect = Exception("LLM down")
            mock_session = MagicMock()
            mock_neo4j.return_value.session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_neo4j.return_value.session.return_value.__exit__ = MagicMock(return_value=False)

            result = duplicate_detection_pass()

        assert result["merged"] == 1
        assert result["details"][0]["method"] == "fallback"
        assert result["details"][0]["merged_into"] == "id-2"
        assert result["details"][0]["superseded"] == ["id-1"]
        client.upsert.assert_not_called()  # no re-embed when text unchanged
        client.set_payload.assert_called_once_with(
            collection_name="firekeep_memories",
            payload={"status": "superseded", "superseded_by": "id-2"},
            points=["id-1"],
        )


class TestDedupSafety:
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_disabled_by_default(self, mock_settings, mock_qdrant):
        """DEDUP_ENABLED=False (the shipped default) short-circuits the pass
        before touching Qdrant."""
        settings = _make_settings(DEDUP_ENABLED=False)
        mock_settings.return_value = settings

        result = duplicate_detection_pass()

        assert result == {"status": "disabled", "merged": 0, "details": []}
        mock_qdrant.assert_not_called()

    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_corpus_points_excluded_from_scope(self, mock_settings, mock_qdrant, mock_webhook):
        """Both the scroll and the per-memory similarity query must exclude
        source=corpus points (defect #3: document shredding via chunk overlap).

        NOTE: two non-corpus points are seeded (not one) so the cluster loop
        actually reaches query_points — with a single scrolled memory the
        pass short-circuits via the `len(memories) < 2` guard before ever
        querying, which would make the query_filter assertion below
        unreachable (call_args is None). This is a deviation from the task
        brief's literal test body; see task-6-report.md concerns.
        """
        settings = _make_settings()
        mock_settings.return_value = settings

        client = MagicMock()
        mock_qdrant.return_value = client
        p1 = _make_point("id-1", {"text": "A memory", "status": "active", "domain": "db",
                                    "tags": [], "confirmed_count": 0, "contradicted_count": 0},
                         [0.9, 0.1, 0.0])
        p2 = _make_point("id-2", {"text": "A different memory", "status": "active", "domain": "db",
                                    "tags": [], "confirmed_count": 0, "contradicted_count": 0},
                         [0.1, 0.9, 0.0])
        client.scroll.return_value = ([p1, p2], None)
        client.query_points.return_value = _make_query_result([])

        duplicate_detection_pass()

        scroll_filter = client.scroll.call_args.kwargs["scroll_filter"]
        assert scroll_filter.must_not, "scroll filter must exclude corpus"
        assert scroll_filter.must_not[0].key == "source"
        assert scroll_filter.must_not[0].match.value == "corpus"

        query_filter = client.query_points.call_args.kwargs["query_filter"]
        assert query_filter.must_not, "similarity query must exclude corpus"
        assert query_filter.must_not[0].key == "source"
        assert query_filter.must_not[0].match.value == "corpus"

    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_cross_domain_similars_not_merged(self, mock_settings, mock_qdrant, mock_webhook):
        """0.95-similar points in different domains must NOT cluster."""
        settings = _make_settings()
        mock_settings.return_value = settings

        client = MagicMock()
        mock_qdrant.return_value = client
        p1 = _make_point("id-1", {"text": "Deploy with docker compose", "status": "active",
                                    "domain": "infra", "tags": [], "confirmed_count": 0,
                                    "contradicted_count": 0}, [0.9, 0.1, 0.0])
        p2 = _make_point("id-2", {"text": "Deploy using docker compose", "status": "active",
                                    "domain": "workflow", "tags": [], "confirmed_count": 0,
                                    "contradicted_count": 0}, [0.89, 0.11, 0.01])
        client.scroll.return_value = ([p1, p2], None)
        sp2 = _make_point("id-2", score=0.95)
        sp1 = _make_point("id-1", score=0.95)
        client.query_points.side_effect = [
            _make_query_result([sp2]),
            _make_query_result([sp1]),
        ]

        result = duplicate_detection_pass()

        assert result["merged"] == 0
        client.set_payload.assert_not_called()
        client.upsert.assert_not_called()

    @patch("app.workers.memory_agent._merge_lifecycle", side_effect=_fake_merge_lifecycle)
    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_embed_failure_aborts_merge_without_corruption(
        self, mock_settings, mock_qdrant, mock_neo4j, mock_webhook, mock_ml
    ):
        """If re-embedding the merged text fails, the merge is aborted entirely:
        no supersession, no upsert, no text overwrite (fail loudly, never silently)."""
        settings = _make_settings()
        mock_settings.return_value = settings

        client = MagicMock()
        mock_qdrant.return_value = client
        # confirmed_count 0: a confirmed memory is out of this pass's scope.
        p1 = _make_point("id-1", {"text": "Use Postgres for storage", "status": "active",
                                    "domain": "db", "tags": [], "confirmed_count": 0,
                                    "contradicted_count": 0}, [0.9, 0.1, 0.0])
        p2 = _make_point("id-2", {"text": "Postgres is the storage backend", "status": "active",
                                    "domain": "db", "tags": [], "confirmed_count": 0,
                                    "contradicted_count": 0}, [0.89, 0.11, 0.01])
        client.scroll.return_value = ([p1, p2], None)
        sp2 = _make_point("id-2", score=0.95)
        client.query_points.return_value = _make_query_result([sp2])

        with patch("app.workers.memory_agent.httpx.post") as mock_httpx:
            llm_resp = MagicMock()
            llm_resp.raise_for_status = MagicMock()
            llm_resp.json.return_value = {
                "choices": [{"message": {"content": json.dumps({
                    "text": "Use Postgres as the primary storage backend",
                    "domain": "db", "tags": []
                })}}]
            }
            mock_httpx.side_effect = [llm_resp, Exception("embedding endpoint down")]

            result = duplicate_detection_pass()

        assert result["merged"] == 0
        client.upsert.assert_not_called()
        client.set_payload.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: Orphan Cleanup
# ---------------------------------------------------------------------------


class TestOrphanCleanup:
    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent.get_settings")
    def test_orphan_cleanup(self, mock_settings, mock_neo4j, mock_webhook):
        """Orphan nodes (degree-0) are deleted and webhook fired."""
        settings = _make_settings(GC_PURGE_ENABLED=True)
        mock_settings.return_value = settings

        # Neo4j returns orphan nodes
        mock_record1 = {"label": "Concept", "name": "stale_concept"}
        mock_record2 = {"label": "Action", "name": "orphan_action"}
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([mock_record1, mock_record2]))

        mock_tx = MagicMock()
        mock_tx.run.return_value = mock_result
        mock_tx.commit = MagicMock()

        mock_session = MagicMock()
        mock_session.begin_transaction.return_value.__enter__ = MagicMock(return_value=mock_tx)
        mock_session.begin_transaction.return_value.__exit__ = MagicMock(return_value=False)

        mock_neo4j.return_value.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_neo4j.return_value.session.return_value.__exit__ = MagicMock(return_value=False)

        result = orphan_cleanup_pass()

        assert result["status"] == "ok"
        assert len(result["nodes_removed"]) == 2
        assert result["nodes_removed"][0]["label"] == "Concept"
        assert result["nodes_removed"][1]["name"] == "orphan_action"

        # Verify webhook
        mock_webhook.assert_called_once_with(
            settings.REDIS_URL,
            "agent.orphan_cleaned",
            {"nodes_removed": result["nodes_removed"]},
        )

    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent.get_settings")
    def test_orphan_cleanup_requires_explicit_purge_opt_in(
        self, mock_settings, mock_neo4j
    ):
        mock_settings.return_value = _make_settings(GC_PURGE_ENABLED=False)

        result = orphan_cleanup_pass()

        assert result == {"status": "disabled", "nodes_removed": []}
        mock_neo4j.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: Deep Contradiction Scan
# ---------------------------------------------------------------------------


class TestDeepContradiction:
    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._has_supersedes_link", return_value=False)
    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_deep_contradiction_found(
        self, mock_settings, mock_qdrant, mock_neo4j, mock_link, mock_webhook
    ):
        """Contradictory memories in 0.85-0.95 range auto-supersede stale side."""
        settings = _make_settings()
        mock_settings.return_value = settings

        client = MagicMock()
        mock_qdrant.return_value = client

        p1 = _make_point("mem-1", {
            "text": "Use MySQL", "status": "active", "domain": "db",
            "timestamp": "2025-01-01T00:00:00Z",
            "confirmed_count": 0, "contradicted_count": 0,
        }, [0.5, 0.5, 0.0])
        p2 = _make_point("mem-2", {
            "text": "Use Postgres instead of MySQL", "status": "active", "domain": "db",
            "timestamp": "2026-01-01T00:00:00Z",
            "confirmed_count": 2, "contradicted_count": 0,
        }, [0.5, 0.5, 0.01])

        client.scroll.return_value = ([p1, p2], None)

        # p1 finds p2 as similar with 0.90 score
        sp2 = _make_point("mem-2", score=0.90)
        # p2 finds p1 but pair already processed
        sp1 = _make_point("mem-1", score=0.90)
        client.query_points.side_effect = [
            _make_query_result([sp2]),
            _make_query_result([sp1]),
        ]

        mock_session = MagicMock()
        mock_neo4j.return_value.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_neo4j.return_value.session.return_value.__exit__ = MagicMock(return_value=False)

        result = deep_contradiction_pass()

        assert result["status"] == "ok"
        assert result["contradictions_found"] == 1
        detail = result["details"][0]
        # mem-1 is stale (lower confidence, older), mem-2 is kept
        assert detail["kept"] == "mem-2"
        assert detail["superseded"] == "mem-1"
        assert detail["similarity"] == 0.9

        # Verify stale memory superseded in Qdrant
        client.set_payload.assert_called_with(
            collection_name="firekeep_memories",
            payload={"status": "superseded", "superseded_by": "mem-2"},
            points=["mem-1"],
        )

        # Verify webhook
        mock_webhook.assert_called_once()
        wh_payload = mock_webhook.call_args[0][2]
        assert wh_payload["kept"] == "mem-2"
        assert wh_payload["superseded"] == "mem-1"

    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_contradiction_pass_excludes_corpus(
        self, mock_settings, mock_qdrant, mock_neo4j, mock_webhook
    ):
        """Both the scroll and the per-memory similarity query must exclude
        source=corpus points — corpus chunks are document fragments, not
        competing memories to auto-supersede (SP0 B1 follow-up).

        Two dissimilar points are seeded so the per-memory query loop
        definitely fires query_points; empty query results keep the pass
        side-effect free."""
        settings = _make_settings()
        mock_settings.return_value = settings

        client = MagicMock()
        mock_qdrant.return_value = client
        p1 = _make_point("mem-1", {"text": "Use MySQL", "status": "active", "domain": "db",
                                    "timestamp": "2026-01-01T00:00:00Z",
                                    "confirmed_count": 0, "contradicted_count": 0},
                         [0.9, 0.1, 0.0])
        p2 = _make_point("mem-2", {"text": "Ship on Fridays", "status": "active",
                                    "domain": "workflow",
                                    "timestamp": "2026-01-02T00:00:00Z",
                                    "confirmed_count": 0, "contradicted_count": 0},
                         [0.1, 0.9, 0.0])
        client.scroll.return_value = ([p1, p2], None)
        client.query_points.return_value = _make_query_result([])

        result = deep_contradiction_pass()

        assert result["contradictions_found"] == 0
        client.set_payload.assert_not_called()

        scroll_filter = client.scroll.call_args.kwargs["scroll_filter"]
        assert scroll_filter.must_not, "scroll filter must exclude corpus"
        assert scroll_filter.must_not[0].key == "source"
        assert scroll_filter.must_not[0].match.value == "corpus"

        query_filter = client.query_points.call_args.kwargs["query_filter"]
        assert query_filter.must_not, "similarity query must exclude corpus"
        assert query_filter.must_not[0].key == "source"
        assert query_filter.must_not[0].match.value == "corpus"


# ---------------------------------------------------------------------------
# Test 5: Cluster Coherence
# ---------------------------------------------------------------------------


class TestClusterCoherence:
    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_reclassify_outlier(self, mock_settings, mock_qdrant, mock_neo4j, mock_webhook):
        """An outlier memory gets reclassified to a better-fit domain."""
        settings = _make_settings()
        mock_settings.return_value = settings

        client = MagicMock()
        mock_qdrant.return_value = client

        # Domain "db" has 3 memories, one is an outlier closer to "infra"
        db_vec = [1.0, 0.0, 0.0]
        db_vec2 = [0.99, 0.01, 0.0]
        db_vec3 = [0.98, 0.02, 0.0]
        outlier_vec = [0.1, 0.9, 0.0]  # very different from db cluster

        # Domain "infra" has 3 memories closer to the outlier
        infra_vec = [0.1, 0.95, 0.0]
        infra_vec2 = [0.12, 0.93, 0.0]
        infra_vec3 = [0.08, 0.97, 0.0]

        points = [
            _make_point("db-1", {"text": "DB1", "status": "active", "domain": "db"}, db_vec),
            _make_point("db-2", {"text": "DB2", "status": "active", "domain": "db"}, db_vec2),
            _make_point("db-3", {"text": "DB3", "status": "active", "domain": "db"}, db_vec3),
            _make_point("outlier", {"text": "Infra stuff", "status": "active", "domain": "db"}, outlier_vec),
            _make_point("infra-1", {"text": "I1", "status": "active", "domain": "infra"}, infra_vec),
            _make_point("infra-2", {"text": "I2", "status": "active", "domain": "infra"}, infra_vec2),
            _make_point("infra-3", {"text": "I3", "status": "active", "domain": "infra"}, infra_vec3),
        ]

        client.scroll.return_value = (points, None)

        # Neo4j mock for domain update
        mock_tx = MagicMock()
        mock_tx.run = MagicMock()
        mock_tx.commit = MagicMock()

        mock_session = MagicMock()
        mock_session.begin_transaction.return_value.__enter__ = MagicMock(return_value=mock_tx)
        mock_session.begin_transaction.return_value.__exit__ = MagicMock(return_value=False)

        mock_neo4j.return_value.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_neo4j.return_value.session.return_value.__exit__ = MagicMock(return_value=False)

        result = cluster_coherence_pass()

        assert result["status"] == "ok"
        assert result["reclassified"] >= 1

        # Find the outlier reclassification
        reclassified = [r for r in result["details"] if r["memory_id"] == "outlier"]
        assert len(reclassified) == 1
        assert reclassified[0]["from_domain"] == "db"
        assert reclassified[0]["to_domain"] == "infra"

        # Verify Qdrant domain updated
        client.set_payload.assert_any_call(
            collection_name="firekeep_memories",
            payload={"domain": "infra"},
            points=["outlier"],
        )

        # Verify webhook fired
        mock_webhook.assert_called()
        wh_calls = [c for c in mock_webhook.call_args_list
                     if c[0][1] == "agent.reclassified"]
        assert len(wh_calls) >= 1

    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._get_neo4j_driver")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_coherence_pass_excludes_corpus(
        self, mock_settings, mock_qdrant, mock_neo4j, mock_webhook
    ):
        """The coherence scroll must exclude source=corpus points — corpus
        chunks must never be reclassified to a different domain by centroid
        outlier detection (SP0 B1 follow-up).

        Two same-domain points (below the 3-per-domain centroid minimum) keep
        the pass side-effect free while still exercising the scroll."""
        settings = _make_settings()
        mock_settings.return_value = settings

        client = MagicMock()
        mock_qdrant.return_value = client
        points = [
            _make_point("db-1", {"text": "DB1", "status": "active", "domain": "db"},
                        [1.0, 0.0, 0.0]),
            _make_point("db-2", {"text": "DB2", "status": "active", "domain": "db"},
                        [0.99, 0.01, 0.0]),
        ]
        client.scroll.return_value = (points, None)

        result = cluster_coherence_pass()

        assert result["reclassified"] == 0
        client.set_payload.assert_not_called()

        scroll_filter = client.scroll.call_args.kwargs["scroll_filter"]
        assert scroll_filter.must_not, "scroll filter must exclude corpus"
        assert scroll_filter.must_not[0].key == "source"
        assert scroll_filter.must_not[0].match.value == "corpus"


# ---------------------------------------------------------------------------
# Test 7: Lock Guard
# ---------------------------------------------------------------------------


class TestLockGuard:
    @patch("app.workers.memory_agent.get_settings")
    @patch("app.workers.memory_agent._get_redis_client")
    def test_lock_prevents_concurrent_runs(self, mock_get_redis, mock_settings):
        """Redis SETNX lock prevents a second concurrent run."""
        settings = _make_settings()
        mock_settings.return_value = settings

        mock_redis = MagicMock()
        # Lock already held — SETNX returns False
        mock_redis.set.return_value = False
        mock_get_redis.return_value = mock_redis

        result = run_memory_agent()

        assert result["status"] == "locked"
        # Verify SETNX called with correct key and TTL
        mock_redis.set.assert_called_once_with(
            AGENT_LOCK_KEY, "1", nx=True, ex=6 * 3600,
        )


# ---------------------------------------------------------------------------
# Test 8: Agent Disabled
# ---------------------------------------------------------------------------


class TestAgentDisabled:
    @patch("app.workers.memory_agent.get_settings")
    def test_agent_disabled_skips_all(self, mock_settings):
        """AGENT_ENABLED=false skips all passes entirely."""
        settings = _make_settings(AGENT_ENABLED=False)
        mock_settings.return_value = settings

        result = run_memory_agent()

        assert result["status"] == "disabled"


# ---------------------------------------------------------------------------
# Test 9: Batch Limit
# ---------------------------------------------------------------------------


class TestBatchLimit:
    @patch("app.workers.memory_agent._fire_webhook_sync")
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_scroll_respects_batch_limit(self, mock_settings, mock_qdrant, mock_webhook):
        """Scroll pagination stops at AGENT_BATCH_LIMIT."""
        batch_limit = 5
        settings = _make_settings(AGENT_BATCH_LIMIT=batch_limit)
        mock_settings.return_value = settings

        client = MagicMock()
        mock_qdrant.return_value = client

        # First scroll returns batch_limit points, second should not happen
        points = [
            _make_point(f"mem-{i}", {
                "text": f"Memory {i}", "status": "active",
                "domain": "test", "tags": [],
                "confirmed_count": 0, "contradicted_count": 0,
            }, [float(i) / 10, 0.5, 0.5])
            for i in range(batch_limit)
        ]
        client.scroll.return_value = (points, None)
        client.query_points.return_value = _make_query_result([])

        duplicate_detection_pass()

        # scroll limit should be capped by batch_limit
        scroll_call = client.scroll.call_args
        assert scroll_call.kwargs.get("limit", scroll_call[1].get("limit", 999)) <= batch_limit


# ---------------------------------------------------------------------------
# Test 10: Pass Isolation
# ---------------------------------------------------------------------------


class TestPassIsolation:
    @patch("app.workers.memory_agent.get_settings")
    @patch("app.workers.memory_agent._get_redis_client")
    def test_one_pass_failure_does_not_block_others(self, mock_get_redis, mock_settings):
        """A failing pass should not prevent subsequent passes from running."""
        settings = _make_settings()
        mock_settings.return_value = settings

        mock_redis = MagicMock()
        mock_redis.set.return_value = True  # Lock acquired
        mock_get_redis.return_value = mock_redis

        # Patch all five passes: first and third raise, rest succeed
        with patch("app.workers.memory_agent.duplicate_detection_pass") as p1, \
             patch("app.workers.memory_agent.orphan_cleanup_pass") as p2, \
             patch("app.workers.memory_agent.deep_contradiction_pass") as p3, \
             patch("app.workers.memory_agent.cluster_coherence_pass") as p4, \
             patch("app.workers.memory_agent.flush_access_counts") as p5:

            p1.side_effect = RuntimeError("Pass 1 exploded")
            p2.return_value = {"status": "ok", "nodes_removed": []}
            p3.side_effect = RuntimeError("Pass 3 exploded")
            p4.return_value = {"status": "ok", "reclassified": 0, "details": []}
            p5.return_value = {"status": "ok", "flushed": 0, "stale": 0}

            result = run_memory_agent()

        assert result["status"] == "ok"
        # Failed passes should be recorded as errors
        assert result["passes"]["duplicate_detection"]["status"] == "error"
        assert result["passes"]["deep_contradiction"]["status"] == "error"
        # Successful passes should have their results
        assert result["passes"]["orphan_cleanup"]["status"] == "ok"
        assert result["passes"]["cluster_coherence"]["status"] == "ok"
        assert result["passes"]["access_count_flush"]["status"] == "ok"

        # All 5 passes should have been attempted
        p1.assert_called_once()
        p2.assert_called_once()
        p3.assert_called_once()
        p4.assert_called_once()
        p5.assert_called_once()


# ---------------------------------------------------------------------------
# Utility tests
# ---------------------------------------------------------------------------


class TestCoseSimilarity:
    def test_identical_vectors(self):
        assert _cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine_similarity([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)

    def test_zero_vector(self):
        assert _cosine_similarity([0, 0, 0], [1, 0, 0]) == 0.0


# ---------------------------------------------------------------------------
# SP0 B2 — flush_access_counts (Redis hash -> Qdrant payloads)
# ---------------------------------------------------------------------------

import redis as redis_lib

from app.workers.memory_agent import flush_access_counts


class TestFlushAccessCounts:
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent._get_redis_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_flush_adds_deltas_to_payload(self, mock_settings, mock_redis, mock_qdrant):
        settings = _make_settings()
        mock_settings.return_value = settings

        r = MagicMock()
        r.exists.return_value = False          # no crashed previous flush
        r.hgetall.return_value = {"mem-1": "3"}
        mock_redis.return_value = r

        client = MagicMock()
        point = MagicMock()
        point.payload = {"access_count": 2}
        client.retrieve.return_value = [point]
        mock_qdrant.return_value = client

        result = flush_access_counts()

        assert result["status"] == "ok"
        assert result["flushed"] == 1
        r.rename.assert_called_once_with(
            "memory:access_counts", "memory:access_counts:flushing"
        )
        client.set_payload.assert_called_once_with(
            collection_name="firekeep_memories",
            payload={"access_count": 5},        # 2 persisted + 3 delta
            points=["mem-1"],
        )
        r.hdel.assert_called_once_with("memory:access_counts:flushing", "mem-1")

    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent._get_redis_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_flush_nothing_pending(self, mock_settings, mock_redis, mock_qdrant):
        settings = _make_settings()
        mock_settings.return_value = settings

        r = MagicMock()
        r.exists.return_value = False
        r.rename.side_effect = redis_lib.ResponseError("no such key")
        mock_redis.return_value = r
        mock_qdrant.return_value = MagicMock()

        result = flush_access_counts()

        assert result == {"status": "ok", "flushed": 0, "stale": 0}

    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent._get_redis_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_flush_recovers_crashed_previous_run(self, mock_settings, mock_redis, mock_qdrant):
        """A leftover :flushing key from a crashed prior run is processed
        exactly once; the live accumulator hash is NOT rotated this run —
        its deltas wait for the next rotation."""
        settings = _make_settings()
        mock_settings.return_value = settings

        r = MagicMock()
        r.exists.return_value = True           # crashed flush left :flushing behind
        r.hgetall.return_value = {"mem-1": "3"}
        mock_redis.return_value = r

        client = MagicMock()
        point = MagicMock()
        point.payload = {"access_count": 2}
        client.retrieve.return_value = [point]
        mock_qdrant.return_value = client

        result = flush_access_counts()

        assert result == {"status": "ok", "flushed": 1, "stale": 0}
        # Live hash untouched: no rotation while recovering the crashed batch.
        r.rename.assert_not_called()
        r.hgetall.assert_called_once_with("memory:access_counts:flushing")
        # Leftover delta applied exactly once.
        client.set_payload.assert_called_once_with(
            collection_name="firekeep_memories",
            payload={"access_count": 5},        # 2 persisted + 3 leftover delta
            points=["mem-1"],
        )
        r.hdel.assert_called_once_with("memory:access_counts:flushing", "mem-1")

    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent._get_redis_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_flush_hdel_precedes_set_payload(self, mock_settings, mock_redis, mock_qdrant):
        """Crash-idempotency: the delta must be removed from the :flushing
        hash BEFORE it is written to the payload. A crash between the two
        then drops the delta (benign undercount of a best-effort signal)
        instead of double-applying it on the next recovery run."""
        settings = _make_settings()
        mock_settings.return_value = settings

        r = MagicMock()
        r.exists.return_value = False
        r.hgetall.return_value = {"mem-1": "3"}
        mock_redis.return_value = r

        client = MagicMock()
        point = MagicMock()
        point.payload = {"access_count": 2}
        client.retrieve.return_value = [point]
        mock_qdrant.return_value = client

        manager = MagicMock()
        manager.attach_mock(r.hdel, "hdel")
        manager.attach_mock(client.set_payload, "set_payload")

        flush_access_counts()

        call_names = [name for name, _args, _kwargs in manager.mock_calls]
        assert "hdel" in call_names and "set_payload" in call_names
        assert call_names.index("hdel") < call_names.index("set_payload")

    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent._get_redis_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_flush_drops_deleted_memories(self, mock_settings, mock_redis, mock_qdrant):
        """A delta for a memory GC already deleted is dropped, not an error."""
        settings = _make_settings()
        mock_settings.return_value = settings

        r = MagicMock()
        r.exists.return_value = False
        r.hgetall.return_value = {"gone-1": "7"}
        mock_redis.return_value = r

        client = MagicMock()
        client.retrieve.return_value = []       # point no longer exists
        mock_qdrant.return_value = client

        result = flush_access_counts()

        assert result["status"] == "ok"
        assert result["stale"] == 1
        client.set_payload.assert_not_called()
        r.hdel.assert_called_once_with("memory:access_counts:flushing", "gone-1")


# ---------------------------------------------------------------------------
# Skill staleness support: flush_last_recalled + run_memory_agent registration
# ---------------------------------------------------------------------------

from app.workers.memory_agent import flush_last_recalled


class TestFlushLastRecalled:
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent._get_redis_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_flush_writes_last_recalled_at_payload(self, mock_settings, mock_redis, mock_qdrant):
        mock_settings.return_value = _make_settings()
        r = MagicMock()
        r.exists.return_value = False
        r.hgetall.return_value = {"mem-1": "2026-07-16T00:00:00+00:00"}
        mock_redis.return_value = r
        client = MagicMock()
        mock_qdrant.return_value = client

        result = flush_last_recalled()

        assert result["status"] == "ok"
        assert result["flushed"] == 1
        r.rename.assert_called_once_with(
            "memory:last_recalled", "memory:last_recalled:flushing"
        )
        client.set_payload.assert_called_once_with(
            collection_name="firekeep_memories",
            payload={"last_recalled_at": "2026-07-16T00:00:00+00:00"},
            points=["mem-1"],
        )
        r.hdel.assert_called_once_with("memory:last_recalled:flushing", "mem-1")

    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent._get_redis_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_flush_nothing_pending(self, mock_settings, mock_redis, mock_qdrant):
        mock_settings.return_value = _make_settings()
        r = MagicMock()
        r.exists.return_value = False
        r.rename.side_effect = redis_lib.ResponseError("no such key")
        mock_redis.return_value = r
        mock_qdrant.return_value = MagicMock()

        result = flush_last_recalled()

        assert result == {"status": "ok", "flushed": 0}


class TestStalenessPassRegistered:
    def test_run_memory_agent_includes_staleness_after_flush(self):
        """The staleness sweep must run in run_memory_agent AFTER the
        last-recalled flush, so freshness timestamps are current when it
        evaluates. Assert both are wired and ordered."""
        import inspect
        from app.workers import memory_agent
        src = inspect.getsource(memory_agent.run_memory_agent)
        assert '"last_recalled_flush"' in src
        assert '"skill_staleness"' in src
        # the staleness PASS is registered after the last-recalled flush PASS
        assert src.index('"last_recalled_flush"') < src.index('"skill_staleness"')


class TestFlushLastRecalledRecovery:
    @patch("app.workers.memory_agent._get_qdrant_client")
    @patch("app.workers.memory_agent._get_redis_client")
    @patch("app.workers.memory_agent.get_settings")
    def test_recovers_crashed_previous_run_without_rotating(self, mock_settings, mock_redis, mock_qdrant):
        """A leftover :flushing key from a crashed prior run must be drained
        WITHOUT rotating the live hash (else deltas double-apply or vanish).
        Mirrors flush_access_counts' recovery guard — a mutant removing the
        exists() check (always rename) would fail this."""
        mock_settings.return_value = _make_settings()
        r = MagicMock()
        r.exists.return_value = True  # crashed flush left :flushing behind
        r.hgetall.return_value = {"mem-1": "2026-07-16T00:00:00+00:00"}
        mock_redis.return_value = r
        client = MagicMock()
        mock_qdrant.return_value = client

        result = flush_last_recalled()

        assert result["status"] == "ok"
        r.rename.assert_not_called()  # live hash NOT rotated during recovery
        r.hgetall.assert_called_once_with("memory:last_recalled:flushing")
        client.set_payload.assert_called_once()


class TestNewPassesExecuted:
    def test_run_memory_agent_executes_flush_then_staleness(self, monkeypatch):
        """Drive run_memory_agent with every pass patched to a recorder and assert
        the two new passes actually RUN, in order (flush before staleness) — the
        source-text test can't catch a broken func binding or a swapped no-op."""
        import app.workers.memory_agent as ma
        from app.skills import staleness as staleness_mod

        calls = []

        def rec(name):
            def _f(*a, **k):
                calls.append(name)
                return {"status": "ok"}
            return _f

        settings = _make_settings()
        monkeypatch.setattr(ma, "get_settings", lambda: settings)
        redis_client = MagicMock()
        redis_client.set.return_value = True  # lock acquired
        monkeypatch.setattr(ma, "_get_redis_client", lambda: redis_client)
        for name in ("duplicate_detection_pass", "orphan_cleanup_pass",
                     "deep_contradiction_pass", "cluster_coherence_pass",
                     "flush_access_counts", "flush_last_recalled"):
            monkeypatch.setattr(ma, name, rec(name))
        monkeypatch.setattr(staleness_mod, "skill_staleness_pass", rec("skill_staleness_pass"))
        monkeypatch.setattr(
            "app.workers.skill_synthesis.skill_synthesis_pass", rec("skill_synthesis"), raising=False,
        )

        ma.run_memory_agent()

        assert "flush_last_recalled" in calls
        assert "skill_staleness_pass" in calls
        assert calls.index("flush_last_recalled") < calls.index("skill_staleness_pass")
