"""Tests for the async classify+draft Celery task core (SP2.1 Task 2)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers.skill_synthesis import _run_classify_and_draft


def _patches(classify_result, *, status_side_effect=None):
    """Patch classify_document (worker module), draft delay, and the status writer.
    Returns (ctxmanagers as a tuple to enter)."""
    classify = patch(
        "app.workers.skill_synthesis.classify_document",
        new=AsyncMock(return_value=classify_result),
    )
    delay = patch("app.workers.skill_synthesis.draft_skill_from_doc")
    set_status = patch(
        "app.workers.skill_synthesis.set_ingest_status",
        new=AsyncMock(side_effect=status_side_effect),
    )
    redis = patch(
        "app.workers.skill_synthesis.redis.asyncio.from_url",
        return_value=MagicMock(aclose=AsyncMock()),
    )
    return classify, delay, set_status, redis


def _vector_patches():
    """Patch the Qdrant-touching reconcile path (VectorClient + reconcile_source_skills)
    so no real Qdrant I/O runs. Returns (ctxmanagers as a tuple to enter)."""
    vector_client = patch(
        "app.workers.skill_synthesis.VectorClient",
        return_value=MagicMock(close=AsyncMock()),
    )
    reconcile = patch(
        "app.workers.skill_synthesis.reconcile_source_skills",
        new=AsyncMock(),
    )
    return vector_client, reconcile


@pytest.mark.asyncio
async def test_procedural_fans_out_and_marks_classified():
    classify_result = {"primary_type": "procedural",
                       "procedure_titles": ["Restart", "Rotate"], "ok": True, "note": ""}
    classify, delay, set_status, redis = _patches(classify_result)
    vector_client, reconcile = _vector_patches()
    with classify, delay as mock_delay, set_status as mock_set, redis, vector_client, reconcile:
        await _run_classify_and_draft("Runbook", "1. restart 2. rotate", "wiki", None, "default")

    assert mock_delay.delay.call_count == 2
    mock_delay.delay.assert_any_call("Runbook", "Restart", "1. restart 2. rotate",
                                     project=None, namespace="default")
    # classifying written BEFORE classified (crash-diagnosis signal)
    statuses = [c.args[1] for c in mock_set.await_args_list]
    assert statuses == ["classifying", "classified"]
    final = mock_set.await_args_list[-1]
    assert final.kwargs["disposition"] == "procedural"
    assert final.kwargs["skills_queued"] == 2


@pytest.mark.asyncio
async def test_reference_doc_marks_classified_zero_skills():
    classify_result = {"primary_type": "reference",
                       "procedure_titles": [], "ok": True, "note": ""}
    classify, delay, set_status, redis = _patches(classify_result)
    vector_client, reconcile = _vector_patches()
    with classify, delay as mock_delay, set_status as mock_set, redis, vector_client, reconcile:
        await _run_classify_and_draft("Overview", "background", "text", None, "default")

    mock_delay.delay.assert_not_called()
    final = mock_set.await_args_list[-1]
    assert final.args[1] == "classified"
    assert final.kwargs["disposition"] == "reference"
    assert final.kwargs["skills_queued"] == 0


@pytest.mark.asyncio
async def test_classify_failure_marks_failed_no_drafts():
    classify_result = {"primary_type": "reference", "procedure_titles": [],
                       "ok": False, "note": "classification failed"}
    classify, delay, set_status, redis = _patches(classify_result)
    vector_client, reconcile = _vector_patches()
    with classify, delay as mock_delay, set_status as mock_set, redis, vector_client, reconcile:
        await _run_classify_and_draft("Doc", "x", "text", None, "default")

    mock_delay.delay.assert_not_called()
    assert mock_set.await_args_list[-1].args[1] == "failed"


@pytest.mark.asyncio
async def test_backend_unavailable_marks_corpus_only_not_failed():
    """Generation-offline (embed-only deploy) degrades to 'corpus_only' — doc
    searchable, classification deferred — instead of an alarming 'failed'."""
    classify_result = {"primary_type": "reference", "procedure_titles": [],
                       "ok": False, "unavailable": True,
                       "note": "generation backend unavailable — ... searchable ..."}
    classify, delay, set_status, redis = _patches(classify_result)
    vector_client, reconcile = _vector_patches()
    with classify, delay as mock_delay, set_status as mock_set, redis, vector_client, reconcile:
        await _run_classify_and_draft("Doc", "x", "text", None, "default")

    mock_delay.delay.assert_not_called()
    assert mock_set.await_args_list[-1].args[1] == "corpus_only"


@pytest.mark.asyncio
async def test_reconcile_called_on_ok_with_titles():
    classify_result = {"primary_type": "procedural",
                       "procedure_titles": ["A", "B"], "ok": True, "note": ""}
    classify, delay, set_status, redis = _patches(classify_result)
    with classify, delay, set_status, redis, \
         patch("app.workers.skill_synthesis.VectorClient",
               return_value=MagicMock(close=AsyncMock())), \
         patch("app.workers.skill_synthesis.reconcile_source_skills", new=AsyncMock()) as mock_rec:
        await _run_classify_and_draft("Runbook", "x", "wiki", None, "default")
    mock_rec.assert_awaited_once()
    assert mock_rec.await_args.args[0] == "Runbook"
    assert mock_rec.await_args.args[1] == {"A", "B"}


@pytest.mark.asyncio
async def test_reconcile_not_called_on_classify_failure():
    classify_result = {"primary_type": "reference", "procedure_titles": [],
                       "ok": False, "note": "fail"}
    classify, delay, set_status, redis = _patches(classify_result)
    with classify, delay, set_status, redis, \
         patch("app.workers.skill_synthesis.VectorClient",
               return_value=MagicMock(close=AsyncMock())), \
         patch("app.workers.skill_synthesis.reconcile_source_skills", new=AsyncMock()) as mock_rec:
        await _run_classify_and_draft("Doc", "x", "text", None, "default")
    mock_rec.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_never_raises_when_status_write_fails():
    """The sync task wrapper must swallow even a status-write failure (Redis down
    is exactly when the terminal 'failed' write is attempted)."""
    from app.workers.skill_synthesis import classify_and_draft_from_doc
    with patch("app.workers.skill_synthesis._run_classify_and_draft",
               new=AsyncMock(side_effect=RuntimeError("redis down"))):
        result = classify_and_draft_from_doc.run("Doc", "x", "text")  # .run() = sync body
    assert result["status"] in {"error", "failed"}
