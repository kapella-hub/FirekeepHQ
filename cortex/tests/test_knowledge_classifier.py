import json

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.knowledge.classifier import classify_document


class FakeSettings:
    """Plain settings stub (not MagicMock) so getattr(..., default) works as
    designed for KNOWLEDGE_MAX_PROCEDURES, which isn't defined here on purpose —
    Task 6 adds the real config field."""

    LLM_BASE_URL = "http://ollama:11434/v1"
    LLM_MODEL = "qwen2.5:7b"
    LLM_API_KEY = ""


def _llm_response(payload: dict | None = None, raw_content: str | None = None, reasoning: str | None = None):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    message: dict = {}
    if raw_content is not None:
        message["content"] = raw_content
    elif payload is not None:
        message["content"] = json.dumps(payload)
    if reasoning is not None:
        message["reasoning"] = reasoning
    resp.json = MagicMock(return_value={"choices": [{"message": message}]})
    return resp


def _mock_client(resp=None, side_effect=None):
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    if side_effect is not None:
        mock_http.post = AsyncMock(side_effect=side_effect)
    else:
        mock_http.post = AsyncMock(return_value=resp)
    return mock_http


@pytest.mark.asyncio
async def test_reference_document_classified():
    resp = _llm_response({"primary_type": "reference", "procedure_titles": []})
    mock_http = _mock_client(resp)

    with patch("app.knowledge.classifier.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document(
            "This document describes the X subsystem in general terms.",
            settings=FakeSettings(),
        )

    assert result == {"primary_type": "reference", "procedure_titles": [], "ok": True, "note": ""}


@pytest.mark.asyncio
async def test_multi_procedure_runbook_returns_titles():
    resp = _llm_response({
        "primary_type": "procedural",
        "procedure_titles": ["Restart the service", "Rotate the API key"],
    })
    mock_http = _mock_client(resp)

    with patch("app.knowledge.classifier.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("Runbook content with two procedures...", settings=FakeSettings())

    assert result["ok"] is True
    assert result["primary_type"] == "procedural"
    assert result["procedure_titles"] == ["Restart the service", "Rotate the API key"]


@pytest.mark.asyncio
async def test_malformed_json_triggers_fail_loud_fallback():
    resp = _llm_response(raw_content="not valid json {{{")
    mock_http = _mock_client(resp)

    with patch("app.knowledge.classifier.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("some content", settings=FakeSettings())

    from app.knowledge.classifier import _FAIL_LOUD_NOTE
    assert result["primary_type"] == "reference"
    assert result["procedure_titles"] == []
    assert result["ok"] is False
    assert result["note"].startswith(_FAIL_LOUD_NOTE)
    # the concrete reason is surfaced so the dashboard says WHY (a malformed
    # JSON body → a JSON decode error type name)
    assert "reason:" in result["note"]
    assert "JSONDecodeError" in result["note"] or "ValueError" in result["note"]


@pytest.mark.asyncio
async def test_reasoning_field_fallback_when_content_empty():
    payload = {"primary_type": "mixed", "procedure_titles": ["Do the thing"]}
    resp = _llm_response(raw_content="", reasoning=json.dumps(payload))
    mock_http = _mock_client(resp)

    with patch("app.knowledge.classifier.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())

    assert result["ok"] is True
    assert result["primary_type"] == "mixed"
    assert result["procedure_titles"] == ["Do the thing"]


@pytest.mark.asyncio
async def test_whitespace_only_content_falls_back_to_reasoning():
    """Mirrors sleep_cycle.py's .strip()-gated fallback: whitespace-only
    `content` (truthy but empty after strip) must still fall back to `reasoning`."""
    payload = {"primary_type": "reference", "procedure_titles": []}
    resp = _llm_response(raw_content="   ", reasoning=json.dumps(payload))
    mock_http = _mock_client(resp)

    with patch("app.knowledge.classifier.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())

    assert result["ok"] is True
    assert result["primary_type"] == "reference"


@pytest.mark.asyncio
async def test_non_string_and_empty_titles_dropped():
    resp = _llm_response({
        "primary_type": "procedural",
        "procedure_titles": ["Valid Title", 42, "", "   ", None, "Another Valid"],
    })
    mock_http = _mock_client(resp)

    with patch("app.knowledge.classifier.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())

    assert result["ok"] is True
    assert result["procedure_titles"] == ["Valid Title", "Another Valid"]
    assert "dropped" in result["note"]
    assert "4" in result["note"]


@pytest.mark.asyncio
async def test_invalid_primary_type_coerced_to_mixed():
    resp = _llm_response({"primary_type": "essay", "procedure_titles": []})
    mock_http = _mock_client(resp)

    with patch("app.knowledge.classifier.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())

    assert result["ok"] is True
    assert result["primary_type"] == "mixed"


@pytest.mark.asyncio
async def test_cap_enforced_at_default_of_ten():
    titles = [f"Procedure {i}" for i in range(15)]
    resp = _llm_response({"primary_type": "procedural", "procedure_titles": titles})
    mock_http = _mock_client(resp)

    with patch("app.knowledge.classifier.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())

    assert result["ok"] is True
    assert result["procedure_titles"] == titles[:10]
    assert "capped" in result["note"]


@pytest.mark.asyncio
async def test_cap_enforced_reads_settings_override():
    titles = [f"Procedure {i}" for i in range(15)]
    resp = _llm_response({"primary_type": "procedural", "procedure_titles": titles})
    mock_http = _mock_client(resp)

    class CappedSettings(FakeSettings):
        KNOWLEDGE_MAX_PROCEDURES = 3

    with patch("app.knowledge.classifier.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("content", settings=CappedSettings())

    assert result["procedure_titles"] == titles[:3]
    assert "capped" in result["note"]


@pytest.mark.asyncio
async def test_network_error_degrades_to_backend_unavailable():
    """A ConnectError to the LLM means the generation backend is unreachable —
    that's the backend-unavailable (corpus_only) case, not a generic fail."""
    mock_http = _mock_client(side_effect=httpx.ConnectError("boom"))

    with patch("app.knowledge.classifier.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())

    assert result["ok"] is False
    assert result["unavailable"] is True
    assert "corpus" in result["note"].lower() and "searchable" in result["note"].lower()


@pytest.mark.asyncio
async def test_fail_note_is_searchability_reassuring_and_bounded():
    """The failure note must (a) reassure that the doc IS in the corpus and
    searchable, and (b) bound the reason length so a huge error body can't
    bloat the Redis status hash."""
    huge = "x" * 5000
    mock_http = _mock_client(side_effect=RuntimeError(huge))

    with patch("app.knowledge.classifier.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())

    assert "corpus" in result["note"].lower()
    assert "searchable" in result["note"].lower()
    assert len(result["note"]) < 400  # reason is truncated, not the whole 5000 chars


@pytest.mark.asyncio
async def test_http_status_error_triggers_fail_loud_fallback():
    resp = MagicMock()
    resp.status_code = 500
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("server error", request=MagicMock(), response=resp)
    )
    mock_http = _mock_client(resp)

    with patch("app.knowledge.classifier.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())

    assert result["ok"] is False
    assert result["primary_type"] == "reference"


@pytest.mark.asyncio
async def test_classify_llm_timeout_is_configurable():
    """The classify LLM call must honor settings.KNOWLEDGE_CLASSIFY_TIMEOUT_SECONDS
    (sized for slow CPU Ollama, ~150-200s), not a hardcoded value — otherwise a
    slow-but-successful classify times out and skill drafting never fires."""
    resp = _llm_response({"primary_type": "reference", "procedure_titles": []})
    mock_http = _mock_client(resp)

    class SettingsWithTimeout(FakeSettings):
        KNOWLEDGE_CLASSIFY_TIMEOUT_SECONDS = 275.0

    with patch("app.knowledge.classifier.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        await classify_document("content", settings=SettingsWithTimeout())

    mock_client_cls.assert_called_once_with(timeout=275.0)


@pytest.mark.asyncio
async def test_classify_llm_timeout_defaults_when_unset():
    """When the setting is absent (older config), classify falls back to the
    CPU-sized 300s default rather than raising or using a tight timeout."""
    resp = _llm_response({"primary_type": "reference", "procedure_titles": []})
    mock_http = _mock_client(resp)

    with patch("app.knowledge.classifier.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        await classify_document("content", settings=FakeSettings())

    mock_client_cls.assert_called_once_with(timeout=300.0)


@pytest.mark.asyncio
async def test_backend_unavailable_flagged_on_connect_error():
    """A ConnectError to the LLM (generation backend down/absent) is flagged
    unavailable=True so the ingest degrades to corpus_only, not failed."""
    mock_http = _mock_client(side_effect=httpx.ConnectError("connection refused"))
    with patch("app.knowledge.classifier.httpx.AsyncClient") as mc:
        mc.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())
    assert result["ok"] is False
    assert result["unavailable"] is True


@pytest.mark.asyncio
async def test_backend_unavailable_flagged_on_404_model_not_found():
    """Ollama with only an embedding model returns 404 for a chat model — the
    embed-only office deploy's exact case. Must flag unavailable=True."""
    resp = MagicMock()
    resp.status_code = 404
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("not found", request=MagicMock(), response=resp)
    )
    mock_http = _mock_client(resp)
    with patch("app.knowledge.classifier.httpx.AsyncClient") as mc:
        mc.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())
    assert result["ok"] is False
    assert result["unavailable"] is True


@pytest.mark.asyncio
async def test_genuine_classify_error_not_flagged_unavailable():
    """Malformed JSON from a WORKING generation backend is a real classify error,
    NOT a backend-unavailable case — must stay unavailable=False (→ 'failed')."""
    resp = _llm_response(raw_content="not valid json {{{")
    mock_http = _mock_client(resp)
    with patch("app.knowledge.classifier.httpx.AsyncClient") as mc:
        mc.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())
    assert result["ok"] is False
    assert result.get("unavailable") is False
