"""Tests for Garbage Collection worker (app.workers.gc)."""

from __future__ import annotations

import json as _json
from unittest.mock import MagicMock, patch


from app.workers.gc import prune_memories, _prune, _prune_qdrant, _prune_neo4j_orphans


# ---------------------------------------------------------------------------
# prune_memories — top-level Celery task
# ---------------------------------------------------------------------------


class TestPruneMemories:
    @patch("app.workers.gc._prune")
    def test_successful_prune(self, mock_prune):
        mock_prune.return_value = {
            "status": "completed",
            "pruned_vector": 5,
            "pruned_graph": 3,
        }
        result = prune_memories()
        assert result["status"] == "completed"
        assert result["pruned_vector"] == 5
        assert result["pruned_graph"] == 3

    @patch("app.workers.gc._prune")
    def test_unhandled_error_returns_error_status(self, mock_prune):
        mock_prune.side_effect = RuntimeError("unexpected")
        result = prune_memories()
        assert result["status"] == "error"
        assert result["pruned_vector"] == 0
        assert result["pruned_graph"] == 0


# ---------------------------------------------------------------------------
# _prune_qdrant
# ---------------------------------------------------------------------------


class TestPruneQdrant:
    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_prunes_high_score_unconfirmed_memories(self, mock_get_client, mock_get_redis):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_redis.return_value = MagicMock()

        # Old episodic memory with no access/confidence → high eviction score
        old_low_value = MagicMock()
        old_low_value.id = "point-1"
        old_low_value.payload = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "memory_type": "episodic",
            "access_count": 0,
            "importance_score": 0.0,
            "confidence": 0.3,
            "confirmed_count": 0,
        }

        # Old episodic memory but confirmed — must not be evicted
        confirmed = MagicMock()
        confirmed.id = "point-2"
        confirmed.payload = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "memory_type": "episodic",
            "access_count": 0,
            "importance_score": 0.0,
            "confidence": 0.3,
            "confirmed_count": 1,
        }

        mock_client.scroll.return_value = ([old_low_value, confirmed], None)

        mock_settings = MagicMock()
        mock_settings.QDRANT_COLLECTION = "test_collection"
        mock_settings.EVICTION_THRESHOLD = 1.5

        result = _prune_qdrant(mock_settings)

        # Only the unconfirmed old memory should be pruned
        assert result == 1
        mock_client.delete.assert_called_once()
        mock_client.close.assert_called_once()

    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_preserves_frequently_accessed_memories(self, mock_get_client, mock_get_redis):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_redis.return_value = MagicMock()

        # High-value memory: frequently accessed, confident
        high_value = MagicMock()
        high_value.id = "point-1"
        high_value.payload = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "memory_type": "episodic",
            "access_count": 20,
            "importance_score": 0.8,
            "confidence": 0.7,
            "confirmed_count": 0,
        }

        mock_client.scroll.return_value = ([high_value], None)

        mock_settings = MagicMock()
        mock_settings.QDRANT_COLLECTION = "test_collection"
        mock_settings.EVICTION_THRESHOLD = 1.5

        result = _prune_qdrant(mock_settings)

        # High-value memory should survive
        assert result == 0
        mock_client.delete.assert_not_called()

    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_no_memories(self, mock_get_client, mock_get_redis):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_redis.return_value = MagicMock()
        mock_client.scroll.return_value = ([], None)

        mock_settings = MagicMock()
        mock_settings.QDRANT_COLLECTION = "test_collection"
        mock_settings.EVICTION_THRESHOLD = 1.5

        result = _prune_qdrant(mock_settings)
        assert result == 0
        mock_client.delete.assert_not_called()

    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_qdrant_error_returns_zero(self, mock_get_client, mock_get_redis):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_redis.return_value = MagicMock()
        mock_client.scroll.side_effect = RuntimeError("Qdrant down")

        mock_settings = MagicMock()
        mock_settings.QDRANT_COLLECTION = "test_collection"
        mock_settings.EVICTION_THRESHOLD = 1.5

        result = _prune_qdrant(mock_settings)
        assert result == 0


# ---------------------------------------------------------------------------
# _prune_neo4j_orphans
# ---------------------------------------------------------------------------


class TestPruneNeo4jOrphans:
    @patch("app.workers.gc._get_neo4j_driver")
    def test_deletes_orphaned_nodes(self, mock_get_driver):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_record = MagicMock()
        mock_record.__getitem__ = MagicMock(return_value=7)

        mock_result.single.return_value = mock_record
        mock_session.run.return_value = mock_result
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session
        mock_get_driver.return_value = mock_driver

        result = _prune_neo4j_orphans()
        assert result == 7

    @patch("app.workers.gc._get_neo4j_driver")
    def test_neo4j_error_returns_zero(self, mock_get_driver):
        mock_get_driver.side_effect = RuntimeError("Neo4j down")
        result = _prune_neo4j_orphans()
        assert result == 0


# ---------------------------------------------------------------------------
# _prune — integration of both stores
# ---------------------------------------------------------------------------


class TestPrune:
    @patch("app.workers.gc._prune_neo4j_orphans")
    @patch("app.workers.gc._prune_qdrant")
    @patch("app.workers.gc.get_settings")
    def test_combines_both_stores(self, mock_settings, mock_prune_qdrant, mock_prune_neo4j):
        mock_settings.return_value = MagicMock(MAX_MEMORY_AGE_DAYS=180)
        mock_prune_qdrant.return_value = 10
        mock_prune_neo4j.return_value = 5

        result = _prune()
        assert result["status"] == "completed"
        assert result["pruned_vector"] == 10
        assert result["pruned_graph"] == 5


# ---------------------------------------------------------------------------
# SP0 B2 — GC reads reality (defect #4)
# ---------------------------------------------------------------------------



def _old_point(pid, payload):
    p = MagicMock()
    p.id = pid
    base = {"timestamp": "2020-01-01T00:00:00+00:00", "confirmed_count": 0}
    base.update(payload)
    p.payload = base
    return p


class TestGcReadsReality:
    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_nested_memory_type_respected(self, mock_get_client, mock_get_redis):
        """A pre-migration reference memory (memory_type only nested under
        metadata) must not be scored as episodic and evicted."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_redis.return_value = MagicMock()

        legacy_reference = _old_point("ref-1", {
            "metadata": {"memory_type": "reference"},
        })
        mock_client.scroll.return_value = ([legacy_reference], None)

        mock_settings = MagicMock()
        mock_settings.QDRANT_COLLECTION = "test_collection"
        mock_settings.EVICTION_THRESHOLD = 1.5

        assert _prune_qdrant(mock_settings) == 0
        mock_client.delete.assert_not_called()

    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_skill_never_evicted(self, mock_get_client, mock_get_redis):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_redis.return_value = MagicMock()

        old_skill = _old_point("skill-1", {"memory_type": "skill"})
        mock_client.scroll.return_value = ([old_skill], None)

        mock_settings = MagicMock()
        mock_settings.QDRANT_COLLECTION = "test_collection"
        mock_settings.EVICTION_THRESHOLD = 1.5

        assert _prune_qdrant(mock_settings) == 0
        mock_client.delete.assert_not_called()

    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_corpus_chunk_never_evicted(self, mock_get_client, mock_get_redis):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_get_redis.return_value = MagicMock()

        old_chunk = _old_point("chunk-1", {"source": "corpus"})
        mock_client.scroll.return_value = ([old_chunk], None)

        mock_settings = MagicMock()
        mock_settings.QDRANT_COLLECTION = "test_collection"
        mock_settings.EVICTION_THRESHOLD = 1.5

        assert _prune_qdrant(mock_settings) == 0
        mock_client.delete.assert_not_called()

    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_eviction_writes_audit_log(self, mock_get_client, mock_get_redis):
        """Every eviction appends to gc:eviction:log, trimmed to 1000."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_redis.hgetall.return_value = {}
        mock_get_redis.return_value = mock_redis

        doomed = _old_point("old-1", {"memory_type": "episodic", "confidence": 0.3})
        mock_client.scroll.return_value = ([doomed], None)

        mock_settings = MagicMock()
        mock_settings.QDRANT_COLLECTION = "test_collection"
        mock_settings.EVICTION_THRESHOLD = 1.5

        assert _prune_qdrant(mock_settings) == 1

        from app.workers.gc import GC_EVICTION_LOG_KEY
        assert GC_EVICTION_LOG_KEY == "gc:eviction:log"
        lpush_calls = mock_pipe.lpush.call_args_list
        assert len(lpush_calls) == 1
        key, entry_json = lpush_calls[0][0]
        assert key == "gc:eviction:log"
        entry = _json.loads(entry_json)
        assert entry["id"] == "old-1"
        assert entry["memory_type"] == "episodic"
        assert "eviction_score" in entry and "evicted_at" in entry
        mock_pipe.ltrim.assert_called_once_with("gc:eviction:log", 0, 999)
        mock_pipe.execute.assert_called_once()
