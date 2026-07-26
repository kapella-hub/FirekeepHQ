"""Tests for POST /decision/synthesize (SP4 Task 3)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.decision.api import create_decision_router


def _make_app(mock_rag):
    app = FastAPI()
    router = create_decision_router()
    app.include_router(router)
    from app.main import get_rag_engine
    app.dependency_overrides[get_rag_engine] = lambda: mock_rag
    return app


@pytest.fixture
def mock_rag():
    return MagicMock()


@pytest.fixture
def client(mock_rag):
    return TestClient(_make_app(mock_rag))


def _board(**overrides):
    board = {
        "questions": [
            {"id": "q0", "text": "What owns billing retries?", "knowledge_found": True,
             "evidence": [{"source": "vector", "snippet": "retry worker", "ref": {}}],
             "suggested_answers": ["Restart the ingest worker"],
             "suggested_actions": ["Run scripts/restart_worker.sh"]},
        ],
        "generated_at": "2026-07-11T00:00:00+00:00",
        "degraded": False,
        "note": "",
    }
    board.update(overrides)
    return board


def test_synthesize_returns_200_with_board_id_and_questions(client):
    with patch("app.decision.api.synthesize_board", new=AsyncMock(return_value=_board())) as mock_synth:
        resp = client.post(
            "/decision/synthesize",
            json={"context": "billing retries are failing", "draft_questions": ["What owns billing retries?"],
                  "agent_id": "codex"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "board_id" in data
    assert isinstance(data["board_id"], str) and len(data["board_id"]) == 32
    int(data["board_id"], 16)  # hex
    assert data["questions"] == _board()["questions"]
    mock_synth.assert_awaited_once()
    assert mock_synth.await_args.args == ("billing retries are failing", ["What owns billing retries?"])


def test_synthesize_degraded_board_still_returns_200(client):
    degraded_board = _board(degraded=True, note="retrieval-only")
    with patch("app.decision.api.synthesize_board", new=AsyncMock(return_value=degraded_board)):
        resp = client.post(
            "/decision/synthesize",
            json={"context": "billing retries are failing", "draft_questions": []},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["degraded"] is True
    assert data["note"] == "retrieval-only"
    assert "board_id" in data


def test_default_draft_questions_and_agent_id(client):
    with patch("app.decision.api.synthesize_board", new=AsyncMock(return_value=_board())) as mock_synth:
        resp = client.post("/decision/synthesize", json={"context": "just context"})

    assert resp.status_code == 200
    assert mock_synth.await_args.args == ("just context", [])


def test_empty_context_returns_422(client):
    with patch("app.decision.api.synthesize_board", new=AsyncMock()) as mock_synth:
        resp = client.post("/decision/synthesize", json={"context": "", "draft_questions": []})

    assert resp.status_code == 422
    mock_synth.assert_not_called()


def test_missing_context_returns_422(client):
    with patch("app.decision.api.synthesize_board", new=AsyncMock()) as mock_synth:
        resp = client.post("/decision/synthesize", json={"draft_questions": ["q"]})

    assert resp.status_code == 422
    mock_synth.assert_not_called()


def test_synthesize_endpoint_returns_200_degraded_when_synthesize_raises(client):
    with patch("app.decision.api.synthesize_board", new=AsyncMock(side_effect=RuntimeError("boom"))):
        resp = client.post(
            "/decision/synthesize",
            json={"context": "c", "draft_questions": ["A?", "B?"]},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["degraded"] is True
    assert "board_id" in data and data["board_id"]
    assert len(data["questions"]) == 2
    assert data["note"] == "synthesize-failed"
    for i, q in enumerate(data["questions"]):
        assert q["id"] == f"q{i}"
        assert q["knowledge_found"] is False
        assert q["evidence"] == []
        assert q["suggested_answers"] == []
        assert q["suggested_actions"] == []
