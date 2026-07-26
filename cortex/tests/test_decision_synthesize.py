from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.decision.synthesize import synthesize_board
from app.models import MemorySource, RecallResponse


def _recall(sources):
    r = MagicMock(spec=RecallResponse)
    r.sources = sources
    r.context_block = ""
    return r

def _settings():
    s = MagicMock()
    s.DECISION_MAX_QUESTIONS = 8
    s.DECISION_SYNTH_TIMEOUT_SECONDS = 20.0
    return s


@pytest.mark.asyncio
async def test_recall_is_global_no_project_filter():
    rag = MagicMock()
    captured = {}
    async def _rec(q):
        captured["q"] = q
        return _recall([])
    rag.recall = AsyncMock(side_effect=_rec)
    with patch("app.decision.synthesize._llm_suggest", new=AsyncMock(return_value={})):
        await synthesize_board("ctx", ["q1"], rag_engine=rag, settings=_settings())
    # every issued ContextQuery must have project None (global — teammates not excluded)
    assert captured["q"].project is None


@pytest.mark.asyncio
async def test_evidence_from_sources_and_knowledge_found():
    src = MemorySource(store="vector", content="the runbook says restart", score=0.9,
                       metadata={"id": "m1", "source": "corpus"})
    rag = MagicMock()
    rag.recall = AsyncMock(return_value=_recall([src]))
    with patch("app.decision.synthesize._llm_suggest", new=AsyncMock(return_value={})):
        board = await synthesize_board("ctx", ["restart?"], rag_engine=rag, settings=_settings())
    q = board["questions"][0]
    assert q["knowledge_found"] is True
    assert q["evidence"][0]["snippet"] == "the runbook says restart"
    assert q["evidence"][0]["ref"] == {"id": "m1", "source": "corpus"}


@pytest.mark.asyncio
async def test_no_vector_hit_knowledge_not_found():
    rag = MagicMock()
    rag.recall = AsyncMock(return_value=_recall([]))
    with patch("app.decision.synthesize._llm_suggest", new=AsyncMock(return_value={})):
        board = await synthesize_board("ctx", ["q1"], rag_engine=rag, settings=_settings())
    assert board["questions"][0]["knowledge_found"] is False


@pytest.mark.asyncio
async def test_llm_timeout_degrades_to_retrieval_only():
    src = MemorySource(store="vector", content="x", score=0.9, metadata={})
    rag = MagicMock()
    rag.recall = AsyncMock(return_value=_recall([src]))
    import asyncio
    async def _slow(*a, **k):
        await asyncio.sleep(5)
        return {}
    s = _settings()
    s.DECISION_SYNTH_TIMEOUT_SECONDS = 0.01
    with patch("app.decision.synthesize._llm_suggest", new=_slow):
        board = await synthesize_board("ctx", ["q1"], rag_engine=rag, settings=s)
    assert board["degraded"] is True
    assert board["questions"][0]["suggested_answers"] == []


@pytest.mark.asyncio
async def test_max_questions_caps_recalls():
    rag = MagicMock()
    rag.recall = AsyncMock(return_value=_recall([]))
    s = _settings()
    s.DECISION_MAX_QUESTIONS = 2
    with patch("app.decision.synthesize._llm_suggest", new=AsyncMock(return_value={})):
        await synthesize_board("ctx", ["a", "b", "c", "d"], rag_engine=rag, settings=s)
    # 1 context recall + at most DECISION_MAX_QUESTIONS question recalls
    assert rag.recall.await_count <= 1 + 2


@pytest.mark.asyncio
async def test_synthesize_recall_failure_returns_degraded_board():
    rag = MagicMock()
    rag.recall = AsyncMock(side_effect=RuntimeError("qdrant down"))
    with patch("app.decision.synthesize._llm_suggest", new=AsyncMock(return_value={})) as mock_llm:
        board = await synthesize_board("some context", ["Q one?", "Q two?"],
                                        rag_engine=rag, settings=_settings())
    assert board["degraded"] is True
    questions = board["questions"]
    assert len(questions) == 2
    assert [q["text"] for q in questions] == ["Q one?", "Q two?"]
    for i, q in enumerate(questions):
        assert q["id"] == f"q{i}"
        assert q["knowledge_found"] is False
        assert q["evidence"] == []
        assert q["suggested_answers"] == []
        assert q["suggested_actions"] == []
    assert board["note"] == "retrieval-unavailable"
    # recall failing means the Cortex path is degraded — LLM pass must be skipped entirely
    mock_llm.assert_not_awaited()
