"""Tests for Garbage Collection worker (app.workers.gc)."""

from __future__ import annotations

import json as _json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


from app.workers.gc import (
    prune_memories,
    preview_memories,
    _prune,
    _prune_qdrant,
    _prune_neo4j_orphans,
)


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
    def test_archives_high_score_unconfirmed_memories(self, mock_get_client, mock_get_redis):
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

        result = _prune_qdrant(mock_settings, return_stats=True)

        # Only the unconfirmed old memory is archived; first-pass GC never deletes it.
        assert result["archived_vector"] == 1
        assert result["pruned_vector"] == 0
        mock_client.set_payload.assert_called_once()
        assert mock_client.set_payload.call_args.kwargs["points"] == ["point-1"]
        archive_payload = mock_client.set_payload.call_args.kwargs["payload"]
        assert archive_payload["status"] == "archived"
        assert datetime.fromisoformat(archive_payload["purge_eligible_at"]) > datetime.now(
            timezone.utc
        ) + timedelta(days=89)
        mock_client.delete.assert_not_called()
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
        mock_settings.return_value = MagicMock(
            GC_ENABLED=True,
            GC_DRY_RUN=False,
            GC_PURGE_ENABLED=True,
        )
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
    def test_archive_writes_action_audit_log(self, mock_get_client, mock_get_redis):
        """Every archive appends an action event to the retained audit key."""
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

        result = _prune_qdrant(mock_settings, return_stats=True)
        assert result["archived_vector"] == 1
        assert result["pruned_vector"] == 0

        from app.workers.gc import GC_EVICTION_LOG_KEY
        assert GC_EVICTION_LOG_KEY == "gc:eviction:log"
        lpush_calls = mock_pipe.lpush.call_args_list
        assert len(lpush_calls) == 1
        key, entry_json = lpush_calls[0][0]
        assert key == "gc:eviction:log"
        entry = _json.loads(entry_json)
        assert entry["id"] == "old-1"
        assert entry["action"] == "archived"
        assert entry["memory_type"] == "episodic"
        assert "eviction_score" in entry and "occurred_at" in entry
        assert entry["archived_at"] == entry["occurred_at"]
        mock_pipe.ltrim.assert_called_once_with("gc:eviction:log", 0, 999)
        mock_pipe.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Archive-first GC, explicit purge, preview, and kill switches
# ---------------------------------------------------------------------------


def _gc_settings(**overrides):
    settings = MagicMock()
    settings.QDRANT_COLLECTION = "test_collection"
    settings.EVICTION_THRESHOLD = 1.5
    for name, value in overrides.items():
        setattr(settings, name, value)
    return settings


class TestArchiveFirstGc:
    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_gc_origin_archive_purges_only_after_grace(
        self, mock_get_client, mock_get_redis
    ):
        client = MagicMock()
        mock_get_client.return_value = client
        redis = MagicMock()
        redis.hgetall.return_value = {}
        mock_get_redis.return_value = redis
        archived_at = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
        point = _old_point("archive-1", {
            "status": "archived",
            "archive_source": "gc",
            "archived_at": archived_at,
            "memory_type": "episodic",
        })
        client.scroll.return_value = ([point], None)

        result = _prune_qdrant(
            _gc_settings(GC_PURGE_ENABLED=True, GC_ARCHIVE_GRACE_DAYS=90),
            return_stats=True,
        )

        assert result["pruned_vector"] == 1
        assert result["archived_vector"] == 0
        client.delete.assert_called_once()
        client.set_payload.assert_not_called()
        entry = _json.loads(redis.pipeline.return_value.lpush.call_args.args[1])
        assert entry["action"] == "purged"
        assert entry["evicted_at"] == entry["occurred_at"]

    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_gc_archive_inside_grace_is_retained(self, mock_get_client, mock_get_redis):
        client = MagicMock()
        mock_get_client.return_value = client
        mock_get_redis.return_value = MagicMock()
        point = _old_point("archive-1", {
            "status": "archived",
            "archive_source": "gc",
            "archived_at": (datetime.now(timezone.utc) - timedelta(days=89)).isoformat(),
        })
        client.scroll.return_value = ([point], None)

        result = _prune_qdrant(
            _gc_settings(GC_PURGE_ENABLED=True, GC_ARCHIVE_GRACE_DAYS=90),
            return_stats=True,
        )

        assert result["pruned_vector"] == 0
        client.delete.assert_not_called()

    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_recorded_recovery_boundary_survives_later_config_change(
        self, mock_get_client, mock_get_redis
    ):
        client = MagicMock()
        mock_get_client.return_value = client
        mock_get_redis.return_value = MagicMock()
        point = _old_point("archive-1", {
            "status": "archived",
            "archive_source": "gc",
            "archived_at": "2020-01-01T00:00:00+00:00",
            "purge_eligible_at": (
                datetime.now(timezone.utc) + timedelta(days=30)
            ).isoformat(),
        })
        client.scroll.return_value = ([point], None)

        result = _prune_qdrant(
            _gc_settings(GC_PURGE_ENABLED=True, GC_ARCHIVE_GRACE_DAYS=1),
            return_stats=True,
        )

        assert result["pruned_vector"] == 0
        client.delete.assert_not_called()

    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_purge_requires_explicit_real_bool(self, mock_get_client, mock_get_redis):
        """An absent MagicMock attribute must not accidentally enable deletion."""
        client = MagicMock()
        mock_get_client.return_value = client
        mock_get_redis.return_value = MagicMock()
        point = _old_point("archive-1", {
            "status": "archived",
            "archive_source": "gc",
            "archived_at": "2020-01-01T00:00:00+00:00",
        })
        client.scroll.return_value = ([point], None)

        assert _prune_qdrant(_gc_settings()) == 0
        client.delete.assert_not_called()

    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_manual_legacy_and_malformed_archives_never_purge(
        self, mock_get_client, mock_get_redis
    ):
        client = MagicMock()
        mock_get_client.return_value = client
        mock_get_redis.return_value = MagicMock()
        points = [
            _old_point("manual", {
                "status": "archived", "archive_source": "manual",
                "archived_at": "2020-01-01T00:00:00+00:00",
            }),
            _old_point("legacy", {"status": "archived"}),
            _old_point("malformed", {
                "status": "archived", "archive_source": "gc",
                "archived_at": "not-a-date",
            }),
            _old_point("malformed-boundary", {
                "status": "archived", "archive_source": "gc",
                "archived_at": "2020-01-01T00:00:00+00:00",
                "purge_eligible_at": "not-a-date",
            }),
            _old_point("future", {
                "status": "archived", "archive_source": "gc",
                "archived_at": "2999-01-01T00:00:00+00:00",
            }),
        ]
        client.scroll.return_value = (points, None)

        result = _prune_qdrant(
            _gc_settings(GC_PURGE_ENABLED=True, GC_ARCHIVE_GRACE_DAYS=90),
            return_stats=True,
        )

        assert result["pruned_vector"] == 0
        client.delete.assert_not_called()

    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_only_active_memories_are_automatically_archived(
        self, mock_get_client, mock_get_redis
    ):
        client = MagicMock()
        mock_get_client.return_value = client
        mock_get_redis.return_value = MagicMock()
        client.scroll.return_value = ([
            _old_point("active", {"status": "active", "confidence": 0.3}),
            _old_point("deprecated", {"status": "deprecated", "confidence": 0.3}),
            _old_point("superseded", {"status": "superseded", "confidence": 0.3}),
        ], None)

        result = _prune_qdrant(_gc_settings(), return_stats=True)

        assert result["archived_vector"] == 1
        assert client.set_payload.call_args.kwargs["points"] == ["active"]

    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_decay_settings_drive_archive_score(self, mock_get_client, mock_get_redis):
        client = MagicMock()
        mock_get_client.return_value = client
        mock_get_redis.return_value = MagicMock()
        client.scroll.return_value = ([
            _old_point("episodic", {"memory_type": "episodic", "confidence": 0.3}),
        ], None)

        long_lived = _prune_qdrant(
            _gc_settings(DECAY_EPISODIC_DAYS=10000), return_stats=True
        )
        assert long_lived["archived_vector"] == 0

        short_lived = _prune_qdrant(
            _gc_settings(DECAY_EPISODIC_DAYS=1), return_stats=True
        )
        assert short_lived["archived_vector"] == 1


class TestGcSafetyModes:
    def test_gc_enabled_false_stops_vector_and_graph_work(self):
        settings = _gc_settings(GC_ENABLED=False)
        with (
            patch("app.workers.gc.get_settings", return_value=settings),
            patch("app.workers.gc._prune_qdrant") as qdrant,
            patch("app.workers.gc._prune_neo4j_orphans") as graph,
        ):
            result = _prune()

        assert result["status"] == "disabled"
        assert result["pruned_vector"] == 0
        qdrant.assert_not_called()
        graph.assert_not_called()

    def test_scheduled_dry_run_skips_graph_mutation(self):
        settings = _gc_settings(GC_ENABLED=True, GC_DRY_RUN=True)
        vector_stats = {
            "archived_vector": 0,
            "pruned_vector": 0,
            "would_archive_vector": 2,
            "would_purge_vector": 1,
        }
        with (
            patch("app.workers.gc.get_settings", return_value=settings),
            patch("app.workers.gc._prune_qdrant", return_value=vector_stats),
            patch("app.workers.gc._prune_neo4j_orphans") as graph,
        ):
            result = _prune()

        assert result["dry_run"] is True
        assert result["would_archive_vector"] == 2
        assert result["would_purge_vector"] == 1
        graph.assert_not_called()

    def test_archive_only_mode_skips_graph_mutation(self):
        settings = _gc_settings(
            GC_ENABLED=True,
            GC_DRY_RUN=False,
            GC_PURGE_ENABLED=False,
        )
        vector_stats = {
            "archived_vector": 2,
            "pruned_vector": 0,
            "would_archive_vector": 0,
            "would_purge_vector": 0,
        }
        with (
            patch("app.workers.gc.get_settings", return_value=settings),
            patch("app.workers.gc._prune_qdrant", return_value=vector_stats),
            patch("app.workers.gc._prune_neo4j_orphans") as graph,
        ):
            result = _prune()

        assert result["archived_vector"] == 2
        assert result["pruned_graph"] == 0
        graph.assert_not_called()

    def test_explicit_purge_mode_allows_graph_cleanup(self):
        settings = _gc_settings(
            GC_ENABLED=True,
            GC_DRY_RUN=False,
            GC_PURGE_ENABLED=True,
        )
        vector_stats = {
            "archived_vector": 0,
            "pruned_vector": 1,
            "would_archive_vector": 0,
            "would_purge_vector": 0,
        }
        with (
            patch("app.workers.gc.get_settings", return_value=settings),
            patch("app.workers.gc._prune_qdrant", return_value=vector_stats),
            patch("app.workers.gc._prune_neo4j_orphans", return_value=3) as graph,
        ):
            result = _prune()

        assert result["pruned_graph"] == 3
        graph.assert_called_once_with()

    @patch("app.workers.gc._get_redis_client")
    @patch("app.workers.gc._get_qdrant_client")
    def test_preview_reports_candidates_without_mutation_or_audit(
        self, mock_get_client, mock_get_redis
    ):
        client = MagicMock()
        mock_get_client.return_value = client
        redis = MagicMock()
        redis.hgetall.return_value = {}
        mock_get_redis.return_value = redis
        client.scroll.return_value = ([
            _old_point("active", {"status": "active", "confidence": 0.3}),
            _old_point("archive", {
                "status": "archived", "archive_source": "gc",
                "archived_at": "2020-01-01T00:00:00+00:00",
            }),
        ], None)

        result = preview_memories(
            _gc_settings(
                GC_ENABLED=True,
                GC_PURGE_ENABLED=True,
                GC_ARCHIVE_GRACE_DAYS=90,
            ),
            limit=10,
        )

        assert result["status"] == "preview"
        assert result["would_archive_vector"] == 1
        assert result["would_purge_vector"] == 1
        assert {item["action"] for item in result["candidates"]} == {
            "would_archive", "would_purge",
        }
        client.set_payload.assert_not_called()
        client.delete.assert_not_called()
        redis.pipeline.assert_not_called()
