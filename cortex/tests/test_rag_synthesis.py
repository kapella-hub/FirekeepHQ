import pytest
from unittest.mock import AsyncMock, MagicMock, patch

def test_estimate_tokens():
    from app.engine.rag import estimate_tokens
    assert estimate_tokens("hello world") == 2  # 11 chars // 4 = 2

def test_trim_to_budget_keeps_minimum_two():
    from app.engine.rag import trim_to_budget
    entries = [
        {"score": 0.9, "content": "x" * 800},
        {"score": 0.8, "content": "y" * 800},
        {"score": 0.7, "content": "z" * 800},
    ]
    trimmed = trim_to_budget(entries, budget=10)
    assert len(trimmed) == 2

def test_trim_to_budget_respects_budget():
    from app.engine.rag import trim_to_budget, estimate_tokens
    entries = [
        {"score": 0.9, "content": "a" * 400},
        {"score": 0.8, "content": "b" * 400},
        {"score": 0.7, "content": "c" * 400},
        {"score": 0.6, "content": "d" * 400},
    ]
    trimmed = trim_to_budget(entries, budget=250)
    total = sum(estimate_tokens(e["content"]) for e in trimmed)
    assert total <= 250
    assert len(trimmed) >= 2

@pytest.mark.asyncio
async def test_synthesis_calls_llm_and_returns_paragraph():
    from app.engine.rag import synthesize_memories

    mock_response = MagicMock()
    mock_response.json = MagicMock(return_value={
        "choices": [{"message": {"content": "This is the synthesis."}}]
    })

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
        result = await synthesize_memories(
            task="auth bugs",
            entries=[{"content": "Fixed JWT expiry", "score": 0.9}],
            llm_base_url="http://localhost:11434/v1",
            llm_model="llama3",
            llm_api_key="",
        )
    assert result == "This is the synthesis."

@pytest.mark.asyncio
async def test_synthesis_falls_back_on_llm_failure():
    from app.engine.rag import synthesize_memories
    import httpx

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=httpx.RequestError("fail", request=None)):
        result = await synthesize_memories(
            task="auth bugs",
            entries=[{"content": "Fixed JWT expiry", "score": 0.9}],
            llm_base_url="http://localhost:11434/v1",
            llm_model="llama3",
            llm_api_key="",
        )
    assert result is None
