"""SP0 end-to-end memory-reliability smoke tests for FirekeepBridge (spec §6).

Scenarios:
1. complete_session enqueues distillation (queued, not inline) and defers TTL
2. distillate is attributed: X-Session-Id / X-Agent-Id headers + project in body
3. project omitted when the session declared none (never fabricated)
4. proactive recall queries namespace "default" (defect #9 regression) with
   format "raw" (no LLM synthesis on the hot path, C6)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.distiller import Distiller
from app.proactive_recall import fetch_relevant_memories
from app.session import SessionManager


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def manager(mock_redis, settings):
    return SessionManager(mock_redis, settings)


def _learn_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "stored", "vector_id": "v-1"}
    resp.raise_for_status = MagicMock()
    return resp


class TestQueuedDistillation:
    @pytest.mark.asyncio
    async def test_complete_session_enqueues_and_defers_ttl(self, manager, mock_redis):
        mock_redis.hgetall.return_value = {
            "session_id": "s1", "agent_id": "alice", "status": "active",
            "goal": "ship SP0", "project": "firekeep",
        }
        mock_redis.get.return_value = "s1"

        result = await manager.complete_session(
            session_id="s1", outcome="done", agent_id="alice"
        )
        assert result["status"] == "completed"

        # D1: distillation is queued to the Redis stream, not run inline.
        # Final-review fix: the XADD is issued on the same transaction
        # pipeline as the "queued" state hset (atomic commit), so it shows up
        # on mock_redis._pipeline.xadd rather than mock_redis.xadd directly.
        pipe = mock_redis._pipeline
        pipe.xadd.assert_called_once()
        assert pipe.xadd.call_args.args[0] == "nb:distill:queue"
        fields = pipe.xadd.call_args.args[1]
        assert fields["session_id"] == "s1"
        mock_redis.xadd.assert_not_called()

        # Session-key TTL is set ONLY by the worker after confirmed
        # distillation (or DLQ move) — never at completion time.
        mock_redis._pipeline.expire.assert_not_called()
        mock_redis.expire.assert_not_called()


class TestAttributedDistillate:
    @pytest.mark.asyncio
    async def test_distillate_carries_identity_headers_and_project(self):
        d = Distiller(Settings())
        d._client = AsyncMock()
        d._client.post = AsyncMock(return_value=_learn_response())

        data = {
            "session_id": "s1",
            "agent_id": "alice",
            "project": "firekeep",
            "goal": "ship SP0",
            "plan": "",
            "decisions": [{"timestamp": "t", "content": "queued distillation"}],
            "progress": [{"timestamp": "t", "content": "all tests green"}],
            "tags": ["sp0"],
        }
        result = await d.distill(data, outcome="done")
        assert result["status"] == "success"

        kwargs = d._client.post.await_args.kwargs
        headers = kwargs["headers"]
        assert headers["X-Session-Id"] == "s1"
        assert headers["X-Agent-Id"] == "alice"
        assert kwargs["json"]["project"] == "firekeep"

    @pytest.mark.asyncio
    async def test_project_omitted_when_session_declared_none(self):
        d = Distiller(Settings())
        d._client = AsyncMock()
        d._client.post = AsyncMock(return_value=_learn_response())

        data = {
            "session_id": "s2",
            "agent_id": "bob",
            # no "project" key — the session never declared one
            "goal": "quick fix",
            "plan": "",
            "decisions": [],
            "progress": [],
            "tags": [],
        }
        result = await d.distill(data, outcome="done")
        assert result["status"] == "success"

        body = d._client.post.await_args.kwargs["json"]
        # D2: omit rather than fabricate
        assert body.get("project") is None


class TestProactiveRecallNamespace:
    def test_default_namespace_is_default(self):
        """C1 regression: Bridge must no longer default to 'firekeepbridge'."""
        assert Settings().FIREKEEP_NAMESPACE == "default"

    @pytest.mark.asyncio
    async def test_proactive_recall_surfaces_agent_learned_memory(self):
        """Defect #9 regression: the recall request hits namespace 'default',
        where memory_learn stores — so agent-learned memories can surface.

        Adaptation from the brief: the mocked /memory/recall response must
        carry metadata.raw_score, matching the real RAGEngine contract
        (cortex/app/engine/rag.py:507, _normalize_vector). Without it,
        fetch_relevant_memories' floor filter (proactive_recall.py:61-63)
        treats the source as a bare graph node and skips it — see the
        existing test_skips_sources_without_raw_score regression in
        test_proactive_recall.py, which pins this exact behavior.
        """
        captured = {}
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "sources": [{
                "content": "agent-learned memory",
                "score": 0.8,
                "metadata": {"raw_score": 0.8},
            }]
        }

        client = AsyncMock()

        async def _post(url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            return resp

        client.post = AsyncMock(side_effect=_post)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.proactive_recall.httpx.AsyncClient", return_value=client):
            s = Settings()
            memories = await fetch_relevant_memories(
                context="working on the qdrant snapshot cron for backups",
                api_url="http://cortex",
                namespace=s.FIREKEEP_NAMESPACE,
            )

        assert memories == [{"content": "agent-learned memory", "score": 0.8}]
        assert captured["json"]["namespace"] == "default"
        # C6: proactive recall skips LLM synthesis on the hot path
        assert captured["json"]["format"] == "raw"
