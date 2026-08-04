import json

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.knowledge.classifier import classify_document


class FakeSettings:
    """Plain settings stub (not MagicMock) so getattr(..., default) works as
    designed for KNOWLEDGE_MAX_PROCEDURES, which isn't defined here on purpose —
    Task 6 adds the real config field.

    LLM_NATIVE_CHAT="never" pins these cases to the OpenAI-shaped `/v1` path,
    which is what every `_llm_response` fixture below builds. Without it the
    default "auto" would derive a native root from the `/v1` suffix and fire a
    real `GET http://ollama:11434/api/version` — which happens to fail in CI and
    happens to land back on `/v1`, so the suite would pass for a reason that has
    nothing to do with what it asserts, and would break on any machine running
    ollama locally. The native path gets its own explicit coverage in
    test_llm.py and in test_native_endpoint_is_used_when_probe_confirms below.
    """

    LLM_BASE_URL = "http://ollama:11434/v1"
    LLM_MODEL = "qwen2.5:7b"
    LLM_API_KEY = ""
    LLM_NATIVE_CHAT = "never"


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

    with patch("app.llm.httpx.AsyncClient") as mock_client_cls:
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

    with patch("app.llm.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("Runbook content with two procedures...", settings=FakeSettings())

    assert result["ok"] is True
    assert result["primary_type"] == "procedural"
    assert result["procedure_titles"] == ["Restart the service", "Rotate the API key"]


@pytest.mark.asyncio
async def test_malformed_json_triggers_fail_loud_fallback():
    resp = _llm_response(raw_content="not valid json {{{")
    mock_http = _mock_client(resp)

    with patch("app.llm.httpx.AsyncClient") as mock_client_cls:
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
async def test_reasoning_field_is_not_parsed_as_the_answer():
    """REPLACES test_reasoning_field_fallback_when_content_empty, which asserted
    behaviour this change deliberately deletes.

    The old code fed `message["reasoning"]` to json.loads when content was
    empty. Under JSON mode that cannot help BY CONSTRUCTION — if content is
    empty the grammar blocked it, so `reasoning` holds prose, not JSON (the
    measured /v1 call returned 4357 chars of it). The fixture below is the
    charitable case the old test relied on, where reasoning happens to contain
    valid JSON; even so, the result is now an honest failure rather than a
    rescue, because a real backend does not put the answer there.

    Terminal state is unchanged either way in the field (JSONDecodeError on the
    empty content vs on the prose), which is why deleting the fallback is safe.
    """
    payload = {"primary_type": "mixed", "procedure_titles": ["Do the thing"]}
    resp = _llm_response(raw_content="", reasoning=json.dumps(payload))
    mock_http = _mock_client(resp)

    with patch("app.llm.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())

    assert result["ok"] is False
    assert result["primary_type"] == "reference"
    assert result["procedure_titles"] == []
    # A working-but-unhelpful backend is a genuine classify error, not an
    # "unavailable" one — it must not degrade the ingest to corpus_only.
    assert result["unavailable"] is False
    assert "JSONDecodeError" in result["note"] or "ValueError" in result["note"]


@pytest.mark.asyncio
async def test_whitespace_only_content_is_a_failure_not_a_reasoning_rescue():
    """Companion to the above for whitespace-only content (truthy, empty after
    strip). json.loads("   ") raises, so this is a fail-loud classify error."""
    payload = {"primary_type": "reference", "procedure_titles": []}
    resp = _llm_response(raw_content="   ", reasoning=json.dumps(payload))
    mock_http = _mock_client(resp)

    with patch("app.llm.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())

    assert result["ok"] is False
    assert result["unavailable"] is False


@pytest.mark.asyncio
async def test_non_string_and_empty_titles_dropped():
    resp = _llm_response({
        "primary_type": "procedural",
        "procedure_titles": ["Valid Title", 42, "", "   ", None, "Another Valid"],
    })
    mock_http = _mock_client(resp)

    with patch("app.llm.httpx.AsyncClient") as mock_client_cls:
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

    with patch("app.llm.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())

    assert result["ok"] is True
    assert result["primary_type"] == "mixed"


@pytest.mark.asyncio
async def test_cap_enforced_at_default_of_ten():
    titles = [f"Procedure {i}" for i in range(15)]
    resp = _llm_response({"primary_type": "procedural", "procedure_titles": titles})
    mock_http = _mock_client(resp)

    with patch("app.llm.httpx.AsyncClient") as mock_client_cls:
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

    with patch("app.llm.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        result = await classify_document("content", settings=CappedSettings())

    assert result["procedure_titles"] == titles[:3]
    assert "capped" in result["note"]


@pytest.mark.asyncio
async def test_network_error_degrades_to_backend_unavailable():
    """A ConnectError to the LLM means the generation backend is unreachable —
    that's the backend-unavailable (corpus_only) case, not a generic fail."""
    mock_http = _mock_client(side_effect=httpx.ConnectError("boom"))

    with patch("app.llm.httpx.AsyncClient") as mock_client_cls:
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

    with patch("app.llm.httpx.AsyncClient") as mock_client_cls:
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

    with patch("app.llm.httpx.AsyncClient") as mock_client_cls:
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

    with patch("app.llm.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        await classify_document("content", settings=SettingsWithTimeout())

    mock_client_cls.assert_called_once_with(timeout=275.0)


@pytest.mark.asyncio
async def test_classify_llm_timeout_defaults_when_unset():
    """When the setting is absent (older config), classify falls back to the
    CPU-sized 300s default rather than raising or using a tight timeout."""
    resp = _llm_response({"primary_type": "reference", "procedure_titles": []})
    mock_http = _mock_client(resp)

    with patch("app.llm.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        await classify_document("content", settings=FakeSettings())

    mock_client_cls.assert_called_once_with(timeout=300.0)


@pytest.mark.asyncio
async def test_backend_unavailable_flagged_on_connect_error():
    """A ConnectError to the LLM (generation backend down/absent) is flagged
    unavailable=True so the ingest degrades to corpus_only, not failed."""
    mock_http = _mock_client(side_effect=httpx.ConnectError("connection refused"))
    with patch("app.llm.httpx.AsyncClient") as mc:
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
    with patch("app.llm.httpx.AsyncClient") as mc:
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
    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())
    assert result["ok"] is False
    assert result.get("unavailable") is False


# ---------------------------------------------------------------------------
# Read timeout is NOT "backend unavailable" (the mislabelling this change fixes)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_timeout_is_failed_not_backend_unavailable():
    """THE BUG THIS CHANGE FIXES, pinned.

    `httpx.ReadTimeout` subclasses `httpx.TimeoutException`, which the old
    isinstance tuple named — so a classify that ran out its budget against a
    backend that was deployed, reachable and answering was recorded terminal
    `corpus_only`, with a note promising classification "will run automatically
    once a generation model is deployed". Nothing ever re-enqueues a
    `corpus_only` source, so the document stayed corpus-only forever on a
    perfectly healthy deploy. Not a corner case: the pre-fix /v1 classify
    measured 288.9s against a 300.0s budget.
    """
    mock_http = _mock_client(side_effect=httpx.ReadTimeout("timed out"))

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())

    assert result["ok"] is False
    assert result["unavailable"] is False, (
        "a slow-but-working backend must not be reported as absent"
    )
    # The note must not claim the backend is missing or that this will retry.
    assert "unavailable" not in result["note"].lower()
    assert "once a generation model is deployed" not in result["note"]
    # It must say something actionable instead: reachable, just too slow.
    assert "too slow" in result["note"] and "not absent" in result["note"]


@pytest.mark.asyncio
async def test_connect_timeout_is_still_backend_unavailable():
    """ConnectTimeout ALSO subclasses TimeoutException but means nothing
    answered at all — that genuinely is an unavailable backend, and it must
    keep degrading to corpus_only. It has to be named explicitly in the
    isinstance tuple or removing the bare TimeoutException loses it."""
    mock_http = _mock_client(side_effect=httpx.ConnectTimeout("no route"))

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = mock_http
        result = await classify_document("content", settings=FakeSettings())

    assert result["ok"] is False
    assert result["unavailable"] is True


# ---------------------------------------------------------------------------
# Endpoint selection, end to end through classify_document
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_native_endpoint_is_used_when_probe_confirms():
    """With LLM_NATIVE_CHAT=always the classify posts ollama's NATIVE body to
    `{root}/api/chat` and reads the native response shape — no /v1, no
    response_format, and `stream`/`think` present."""

    class NativeSettings(FakeSettings):
        LLM_NATIVE_CHAT = "always"

    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "message": {"role": "assistant", "content": json.dumps(
            {"primary_type": "procedural", "procedure_titles": ["Restart the worker"]}
        )},
        "done": True,
    })
    mock_http = _mock_client(resp)

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = mock_http
        result = await classify_document("runbook", settings=NativeSettings())

    assert result["ok"] is True
    assert result["procedure_titles"] == ["Restart the worker"]

    url = mock_http.post.await_args.args[0]
    body = mock_http.post.await_args.kwargs["json"]
    assert url == "http://ollama:11434/api/chat"
    assert body["stream"] is False        # omit it and ollama streams NDJSON
    assert body["think"] is False         # the entire point of the native path
    assert body["format"] == "json"
    assert "response_format" not in body
