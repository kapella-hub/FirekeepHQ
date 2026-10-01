"""EMBED_QUERY_PREFIX: query-side text only, never a stored document.

Asymmetric embedders (Qwen3-Embedding, e5, nomic) are trained with an
instruction on the query side and none on the document side. Getting the
sides wrong silently degrades every recall, so each side is pinned here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import get_settings
from app.db.vector import VectorClient

QWEN = "Instruct: Given a task or question, retrieve memories relevant to it\nQuery:"


def _client(prefix: str) -> VectorClient:
    settings = get_settings().model_copy(update={
        "QDRANT_COLLECTION": "t", "EMBEDDING_DIM": 4, "EMBED_QUERY_PREFIX": prefix,
    })
    vc = VectorClient(settings)
    vc._client = AsyncMock()
    vc._client.query_points = AsyncMock(return_value=MagicMock(points=[]))
    vc._client.retrieve = AsyncMock(return_value=[])
    vc._http_client = AsyncMock()
    return vc


@pytest.mark.asyncio
async def test_search_embeds_the_prefixed_query_with_a_real_newline():
    vc = _client(QWEN)
    with patch.object(vc, "_embed", new_callable=AsyncMock, return_value=[0.1] * 4) as emb:
        await vc.search("what did I buy", workspace_id="ws")
    emb.assert_awaited_once_with(
        "Instruct: Given a task or question, retrieve memories relevant to it\nQuery:what did I buy"
    )


@pytest.mark.asyncio
async def test_semantic_memory_listing_is_a_query_too():
    vc = _client("query: ")
    with patch.object(vc, "_embed", new_callable=AsyncMock, return_value=[0.1] * 4) as emb:
        await vc.list_memories(query="deploy")
    emb.assert_awaited_once_with("query: deploy")


@pytest.mark.asyncio
async def test_a_stored_document_never_gets_the_prefix():
    vc = _client("query: ")
    vc._client.upsert = AsyncMock()
    with patch.object(vc, "_embed", new_callable=AsyncMock, return_value=[0.1] * 4) as emb:
        await vc.upsert(text="bought a blender", metadata={"workspace_id": "ws"}, namespace="default")
    assert [c.args[0] for c in emb.await_args_list] == ["bought a blender"]


@pytest.mark.asyncio
async def test_empty_prefix_embeds_the_query_unchanged():
    vc = _client("")
    with patch.object(vc, "_embed", new_callable=AsyncMock, return_value=[0.1] * 4) as emb:
        await vc.search("what did I buy", workspace_id="ws")
    emb.assert_awaited_once_with("what did I buy")


def test_default_is_empty_so_mxbai_queries_are_unchanged():
    assert get_settings().model_fields["EMBED_QUERY_PREFIX"].default == ""
