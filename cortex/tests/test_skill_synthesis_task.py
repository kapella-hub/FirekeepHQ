import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.workers.skill_synthesis import _run_synthesis, draft_skill_from_doc


@pytest.mark.asyncio
async def test_run_synthesis_calls_scorer_and_synthesizer():
    mock_score = MagicMock()
    mock_score.triggered = True
    mock_score.total = 0.8

    with (
        patch("app.workers.skill_synthesis.compute_skill_score", new=AsyncMock(return_value=mock_score)),
        patch("app.workers.skill_synthesis.SkillSynthesizer") as mock_synth_cls,
    ):
        mock_synth = AsyncMock()
        mock_synth.synthesize = AsyncMock(return_value={"status": "ok", "skill_id": "abc", "trigger": "Fix X"})
        mock_synth_cls.return_value = mock_synth

        result = await _run_synthesis("ses1", skill_worthy=False)

    assert result["status"] == "ok"
    assert result["skill_id"] == "abc"


@pytest.mark.asyncio
async def test_run_synthesis_below_threshold_skips():
    mock_score = MagicMock()
    mock_score.triggered = False
    mock_score.total = 0.1

    with patch("app.workers.skill_synthesis.compute_skill_score", new=AsyncMock(return_value=mock_score)):
        result = await _run_synthesis("ses2", skill_worthy=False)

    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_run_synthesis_error_returns_error_status():
    with patch(
        "app.workers.skill_synthesis.compute_skill_score",
        new=AsyncMock(side_effect=Exception("boom")),
    ):
        result = await _run_synthesis("ses3", skill_worthy=False)

    assert result["status"] == "error"


def test_draft_skill_from_doc_returns_synthesizer_result():
    with patch("app.workers.skill_synthesis.SkillSynthesizer") as mock_synth_cls:
        mock_synth = AsyncMock()
        mock_synth.synthesize_from_document = AsyncMock(
            return_value={"status": "drafted", "id": "abc123"}
        )
        mock_synth_cls.return_value = mock_synth

        result = draft_skill_from_doc(
            source_name="wiki:runbook",
            procedure_title="Restart the widget",
            doc_content="1. Do this. 2. Do that.",
            project="acme",
            namespace="default",
        )

    assert result == {"status": "drafted", "id": "abc123"}
    mock_synth.synthesize_from_document.assert_awaited_once_with(
        source_name="wiki:runbook",
        procedure_title="Restart the widget",
        doc_content="1. Do this. 2. Do that.",
        project="acme",
        namespace="default",
        # Tenancy now travels with the draft. Writing a skill point with
        # workspace_id=null put it outside memory_recall's hard workspace
        # filter — stored, listed in the review queue, and matched by nothing.
        workspace_id=None,
        member_id=None,
    )


def test_draft_skill_from_doc_synthesis_exception_returns_draft_failed():
    with patch("app.workers.skill_synthesis.SkillSynthesizer") as mock_synth_cls:
        mock_synth = AsyncMock()
        mock_synth.synthesize_from_document = AsyncMock(side_effect=Exception("qdrant down"))
        mock_synth_cls.return_value = mock_synth

        result = draft_skill_from_doc(
            source_name="wiki:runbook",
            procedure_title="Restart the widget",
            doc_content="1. Do this. 2. Do that.",
        )

    assert result["status"] == "draft_failed"
    assert result["source_doc"] == "wiki:runbook"
    assert result["procedure_title"] == "Restart the widget"
    assert "qdrant down" in result["error"]
