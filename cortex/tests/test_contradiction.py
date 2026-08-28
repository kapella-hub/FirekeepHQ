"""Tests for contradiction detection."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.contradiction import SIMILARITY_THRESHOLD, detect_and_supersede


class TestContradictionDetection:
    @pytest.mark.asyncio
    async def test_no_similar_memories_returns_empty(self):
        vector = AsyncMock()
        graph = AsyncMock()
        vector.find_similar = AsyncMock(return_value=[])

        result = await detect_and_supersede(
            vector, graph, "new text", "new-id", "graph-id", "domain",
            workspace_id="ws-1",
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_similar_memory_gets_superseded(self):
        vector = AsyncMock()
        graph = AsyncMock()
        vector.find_similar = AsyncMock(
            return_value=[
                {
                    "id": "old-id",
                    "score": 0.92,
                    "text": "old text",
                    "domain": "test",
                    "namespace": "default",
                    "metadata": {},
                }
            ]
        )
        vector.update_status = AsyncMock()
        graph.create_supersession = AsyncMock()

        result = await detect_and_supersede(
            vector, graph, "new text", "new-id", "graph-id", "test",
            workspace_id="ws-1",
        )
        assert result == ["old-id"]
        # count_as_contradiction=False: this path decides supersession on cosine
        # similarity alone, so a RESTATEMENT and a correction arrive identically.
        # Bumping contradicted_count for a memory nothing contradicted is a false
        # claim that feeds compute_confidence, the recall confidence factor, and
        # the memory agent's tie-breaks.
        vector.update_status.assert_called_once_with(
            memory_id="old-id", status="superseded", superseded_by="new-id",
            reason="near-duplicate", count_as_contradiction=False,
        )
        graph.create_supersession.assert_called_once()
        # identity-v2 D4: workspace_id must reach find_similar unchanged —
        # this is the whole mechanism that closes cross-workspace supersession.
        assert vector.find_similar.await_args.kwargs["workspace_id"] == "ws-1"

    @pytest.mark.asyncio
    async def test_skips_same_memory_id(self):
        vector = AsyncMock()
        graph = AsyncMock()
        vector.find_similar = AsyncMock(
            return_value=[
                {
                    "id": "new-id",
                    "score": 1.0,
                    "text": "same",
                    "domain": "test",
                    "namespace": "default",
                    "metadata": {},
                }
            ]
        )

        result = await detect_and_supersede(
            vector, graph, "new text", "new-id", "graph-id", "test",
            workspace_id="ws-1",
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_continues_on_update_failure(self):
        vector = AsyncMock()
        graph = AsyncMock()
        vector.find_similar = AsyncMock(
            return_value=[
                {
                    "id": "old-1",
                    "score": 0.9,
                    "text": "old",
                    "domain": "test",
                    "namespace": "default",
                    "metadata": {},
                },
                {
                    "id": "old-2",
                    "score": 0.88,
                    "text": "old2",
                    "domain": "test",
                    "namespace": "default",
                    "metadata": {},
                },
            ]
        )
        vector.update_status = AsyncMock(
            side_effect=[RuntimeError("fail"), None]
        )
        graph.create_supersession = AsyncMock()

        result = await detect_and_supersede(
            vector, graph, "new text", "new-id", "graph-id", "test",
            workspace_id="ws-1",
        )
        assert result == ["old-2"]

    @pytest.mark.asyncio
    async def test_works_without_graph_id(self):
        vector = AsyncMock()
        graph = AsyncMock()
        vector.find_similar = AsyncMock(
            return_value=[
                {
                    "id": "old-id",
                    "score": 0.95,
                    "text": "old",
                    "domain": "test",
                    "namespace": "default",
                    "metadata": {},
                }
            ]
        )
        vector.update_status = AsyncMock()

        result = await detect_and_supersede(
            vector, graph, "new text", "new-id", None, "test",
            workspace_id="ws-1",
        )
        assert result == ["old-id"]
        graph.create_supersession.assert_not_called()

    @pytest.mark.asyncio
    async def test_find_similar_failure_returns_empty(self):
        vector = AsyncMock()
        graph = AsyncMock()
        vector.find_similar = AsyncMock(
            side_effect=RuntimeError("connection error")
        )

        result = await detect_and_supersede(
            vector, graph, "new text", "new-id", "graph-id", "test",
            workspace_id="ws-1",
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_multiple_memories_superseded(self):
        """All similar memories (not self) should be superseded."""
        vector = AsyncMock()
        graph = AsyncMock()
        vector.find_similar = AsyncMock(
            return_value=[
                {"id": "old-1", "score": 0.95, "text": "a", "domain": "d", "namespace": "default", "metadata": {}},
                {"id": "old-2", "score": 0.90, "text": "b", "domain": "d", "namespace": "default", "metadata": {}},
                {"id": "old-3", "score": 0.87, "text": "c", "domain": "d", "namespace": "default", "metadata": {}},
            ]
        )
        vector.update_status = AsyncMock()
        graph.create_supersession = AsyncMock()

        result = await detect_and_supersede(
            vector, graph, "new text", "new-id", "graph-id", "d",
            workspace_id="ws-1",
        )
        assert result == ["old-1", "old-2", "old-3"]
        assert vector.update_status.call_count == 3
        assert graph.create_supersession.call_count == 3

    @pytest.mark.asyncio
    async def test_graph_edge_failure_does_not_prevent_supersession(self):
        """Graph edge creation failure should not remove from superseded list."""
        vector = AsyncMock()
        graph = AsyncMock()
        vector.find_similar = AsyncMock(
            return_value=[
                {"id": "old-id", "score": 0.91, "text": "old", "domain": "test", "namespace": "default", "metadata": {}}
            ]
        )
        vector.update_status = AsyncMock()
        graph.create_supersession = AsyncMock(side_effect=RuntimeError("graph down"))

        result = await detect_and_supersede(
            vector, graph, "new text", "new-id", "graph-id", "test",
            workspace_id="ws-1",
        )
        assert result == ["old-id"]
        vector.update_status.assert_called_once()


class TestWorkspaceScoping:
    """Identity-v2 D4: detect_and_supersede must always have a workspace to
    forward to find_similar — there is no unscoped contradiction search."""

    @pytest.mark.asyncio
    async def test_workspace_id_is_a_required_keyword(self):
        vector = AsyncMock()
        graph = AsyncMock()
        vector.find_similar = AsyncMock(return_value=[])

        with pytest.raises(TypeError):
            await detect_and_supersede(  # type: ignore[call-arg]
                vector, graph, "new text", "new-id", "graph-id", "test"
            )

    @pytest.mark.asyncio
    async def test_workspace_id_forwarded_verbatim_to_find_similar(self):
        vector = AsyncMock()
        graph = AsyncMock()
        vector.find_similar = AsyncMock(return_value=[])

        await detect_and_supersede(
            vector, graph, "new text", "new-id", "graph-id", "test",
            namespace="infra", workspace_id="ws-scoped",
        )

        vector.find_similar.assert_awaited_once_with(
            text="new text", namespace="infra", domain="test",
            threshold=SIMILARITY_THRESHOLD, top_k=4, workspace_id="ws-scoped",
        )
