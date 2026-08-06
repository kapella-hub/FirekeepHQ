"""Tests for the shared knowledge ingestion core (SP3 Task 1)."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.knowledge.ingest_core import ingest_knowledge_document


@pytest.mark.asyncio
async def test_corpus_then_status_then_enqueue():
    vector, redis = MagicMock(), AsyncMock()
    with (
        patch("app.knowledge.ingest_core.corpus_ingest_document", new=AsyncMock()) as mock_corpus,
        patch("app.knowledge.ingest_core.set_ingest_status", new=AsyncMock()) as mock_status,
        patch("app.knowledge.ingest_core.classify_and_draft_from_doc") as mock_task,
    ):
        await ingest_knowledge_document("body", "Doc", "wiki", vector=vector, redis=redis)
    mock_corpus.assert_awaited_once()
    mock_status.assert_awaited_once()
    assert mock_status.await_args.args[1] == "queued"
    # workspace_id/member_id now ride along so the DRAFT SKILLS the classifier
    # fans out are stamped with the ingesting principal's tenancy — a skill
    # written with workspace_id=null is excluded from every recall path.
    mock_task.delay.assert_called_once_with(
        "Doc", "body", "wiki", project=None, namespace="default",
        workspace_id=None, member_id=None,
    )


@pytest.mark.asyncio
async def test_corpus_failure_propagates_and_skips_status_and_enqueue():
    vector, redis = MagicMock(), AsyncMock()
    with (
        patch("app.knowledge.ingest_core.corpus_ingest_document",
              new=AsyncMock(side_effect=RuntimeError("qdrant down"))),
        patch("app.knowledge.ingest_core.set_ingest_status", new=AsyncMock()) as mock_status,
        patch("app.knowledge.ingest_core.classify_and_draft_from_doc") as mock_task,
    ):
        with pytest.raises(RuntimeError):
            await ingest_knowledge_document("body", "Doc", "wiki", vector=vector, redis=redis)
    mock_status.assert_not_awaited()
    mock_task.delay.assert_not_called()
