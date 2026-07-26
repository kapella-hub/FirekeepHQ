import uuid

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.skills.synthesizer import (
    parse_skill_content, build_skill_content,
    SkillSynthesizer, SKILL_NS,
)


def test_build_skill_content_roundtrip():
    content = build_skill_content(
        trigger="Neo4j connection refused on startup",
        symptoms="ServiceUnavailable: Unable to connect",
        domain="neo4j",
        verified_on="firekeep/2026-05",
        body="## What's happening\nPort not bound.\n\n## Steps\n1. Check docker compose.\n\n## Gotchas\n- None.\n\n## Example\ndocker compose ps",
    )
    assert "trigger:" in content
    assert "Neo4j connection" in content
    assert "## Steps" in content


def test_parse_skill_content_valid():
    raw = """trigger: Fix Neo4j on startup
symptoms: Connection refused
domain: neo4j
verified_on: project/2026
---
## What's happening
Port not open.

## Steps
1. Check compose.

## Gotchas
- Check logs.

## Example
docker compose ps"""
    parsed = parse_skill_content(raw)
    assert parsed["trigger"] == "Fix Neo4j on startup"
    assert parsed["symptoms"] == "Connection refused"
    assert parsed["domain"] == "neo4j"
    assert "## Steps" in parsed["body"]


def test_parse_skill_content_missing_separator_returns_fallback():
    parsed = parse_skill_content("no separator here")
    assert parsed["trigger"] != ""  # fallback fills it
    assert "body" in parsed


@pytest.mark.asyncio
async def test_synthesize_calls_llm_and_stores():
    settings = MagicMock()
    settings.LLM_BASE_URL = "http://ollama:11434/v1"
    settings.LLM_MODEL = "qwen2.5:7b"
    settings.LLM_API_KEY = ""
    settings.QDRANT_HOST = "localhost"
    settings.QDRANT_PORT = 6333
    settings.QDRANT_COLLECTION = "firekeep_memory"
    settings.BRIDGE_URL = "http://bridge:8070"
    settings.REDIS_URL = "redis://redis:6379/0"
    settings.EMBEDDING_MODEL = "nomic-embed-text"

    mock_session = {
        "session_id": "s1", "goal": "fix neo4j",
        "shadow": {"scratch": {"k": "finally fixed by binding port"}, "decision": []},
    }
    llm_response = MagicMock()
    llm_response.status_code = 200
    llm_response.json = MagicMock(return_value={
        "choices": [{"message": {"content": (
            "trigger: Fix Neo4j on startup\n"
            "symptoms: Connection refused\n"
            "domain: neo4j\n"
            "verified_on: test/2026\n"
            "---\n## What's happening\nPort.\n\n## Steps\n1. Check.\n\n## Gotchas\n- None.\n\n## Example\nps"
        )}}]
    })
    embed_response = MagicMock()
    embed_response.status_code = 200
    embed_response.json = MagicMock(return_value={"data": [{"embedding": [0.1] * 768}]})

    with (
        patch("app.skills.synthesizer.httpx.AsyncClient") as mock_client_cls,
        patch("app.skills.synthesizer.AsyncQdrantClient") as mock_qdrant_cls,
        patch("app.skills.synthesizer.redis.asyncio.from_url") as mock_redis,
    ):
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = AsyncMock(return_value=MagicMock(status_code=200, json=MagicMock(return_value=mock_session)))
        mock_http.post = AsyncMock(side_effect=[llm_response, embed_response])
        mock_client_cls.return_value = mock_http

        mock_qdrant = AsyncMock()
        mock_qdrant.upsert = AsyncMock()
        mock_qdrant.close = AsyncMock()
        mock_qdrant_cls.return_value = mock_qdrant

        mock_r = AsyncMock()
        mock_r.set = AsyncMock()
        mock_r.aclose = AsyncMock()
        mock_redis.return_value = mock_r

        synth = SkillSynthesizer(settings)
        from app.skills.scorer import SkillScore
        score = SkillScore("s1", 0.8, 0.5, 0.3, 0.8, False, True)
        result = await synth.synthesize("s1", score, project="test", agent_id="me", namespace="default")

    assert result["status"] == "ok"
    assert result["skill_id"]
    mock_qdrant.upsert.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_session_data_sends_x_api_key_header_when_internal_key_set():
    """SP1a final-review FIX 3: the cortex->bridge session fetch must carry
    X-API-Key when settings.FIREKEEP_INTERNAL_KEY is configured, otherwise
    Bridge 401s the request under AUTH_ENABLED=true and Skill Synthesis
    silently goes dark."""
    settings = MagicMock()
    settings.BRIDGE_URL = "http://bridge:8070"
    settings.FIREKEEP_INTERNAL_KEY = "nxs_internal_test_key"

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.get = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            json=MagicMock(return_value={"goal": "g", "outcome": "o", "shadow": {}}),
        )
    )

    with patch("app.skills.synthesizer.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        synth = SkillSynthesizer(settings)
        await synth._fetch_session_data("s1")

    mock_http.get.assert_awaited_once_with(
        "http://bridge:8070/sessions/s1",
        headers={"X-API-Key": "nxs_internal_test_key"},
    )


@pytest.mark.asyncio
async def test_fetch_session_data_omits_header_when_internal_key_unset():
    """Personal-VPS default (FIREKEEP_INTERNAL_KEY unset): no X-API-Key header,
    byte-identical to pre-fix behavior."""
    settings = MagicMock()
    settings.BRIDGE_URL = "http://bridge:8070"
    settings.FIREKEEP_INTERNAL_KEY = None

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.get = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            json=MagicMock(return_value={"goal": "g", "outcome": "o", "shadow": {}}),
        )
    )

    with patch("app.skills.synthesizer.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_http
        synth = SkillSynthesizer(settings)
        await synth._fetch_session_data("s1")

    assert mock_http.get.await_args.kwargs["headers"] == {}


# ---------------------------------------------------------------------------
# synthesize_from_document — SP2 Task 4 (deterministic-ID upsert + active-guard)
# ---------------------------------------------------------------------------

_DOC_LLM_RAW = (
    "trigger: Restart the widget\n"
    "symptoms: Widget stuck in degraded state\n"
    "domain: widgets\n"
    "verified_on: test/2026\n"
    "---\n## What's happening\nWidget wedged.\n\n"
    "## Steps\n1. Restart the widget service.\n\n"
    "## Gotchas\n- Don't just reboot the host.\n\n"
    "## Example\nsystemctl restart widget"
)


def _doc_settings():
    settings = MagicMock()
    settings.LLM_BASE_URL = "http://ollama:11434/v1"
    settings.LLM_MODEL = "qwen2.5:7b"
    settings.LLM_API_KEY = ""
    settings.QDRANT_HOST = "localhost"
    settings.QDRANT_PORT = 6333
    settings.QDRANT_COLLECTION = "firekeep_memory"
    settings.EMBEDDING_MODEL = "nomic-embed-text"
    return settings


def _mock_embed_client():
    embed_response = MagicMock()
    embed_response.status_code = 200
    embed_response.json = MagicMock(return_value={"data": [{"embedding": [0.1] * 768}]})
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = AsyncMock(return_value=embed_response)
    return mock_http


@pytest.mark.asyncio
async def test_synthesize_from_document_produces_draft_with_provenance():
    settings = _doc_settings()

    with (
        patch("app.skills.synthesizer.httpx.AsyncClient") as mock_client_cls,
        patch("app.skills.synthesizer.AsyncQdrantClient") as mock_qdrant_cls,
        patch.object(SkillSynthesizer, "_call_llm_doc", AsyncMock(return_value=_DOC_LLM_RAW)),
    ):
        mock_client_cls.return_value = _mock_embed_client()

        mock_qdrant = AsyncMock()
        mock_qdrant.retrieve = AsyncMock(return_value=[])  # no existing point
        mock_qdrant.set_payload = AsyncMock()
        mock_qdrant.upsert = AsyncMock()
        mock_qdrant.close = AsyncMock()
        mock_qdrant_cls.return_value = mock_qdrant

        synth = SkillSynthesizer(settings)
        result = await synth.synthesize_from_document(
            source_name="wiki-runbook",
            procedure_title="Restart the widget",
            doc_content="full document text ...",
            project="test",
            namespace="default",
        )

    assert result["status"] == "drafted"
    mock_qdrant.upsert.assert_called_once()
    point = mock_qdrant.upsert.call_args.kwargs["points"][0]
    payload = point.payload
    assert payload["skill_status"] == "draft"
    assert payload["source_type"] == "document"
    assert payload["content_class"] == "procedural"
    assert payload["source_doc"] == "wiki-runbook"
    assert payload["procedure_title"] == "Restart the widget"
    assert payload["source_session_id"] is None
    assert payload["agent_id"] is None
    assert payload["needs_rereview"] is False


@pytest.mark.asyncio
async def test_synthesize_from_document_deterministic_id():
    settings = _doc_settings()
    expected_id = str(uuid.uuid5(SKILL_NS, "wiki-runbook::Restart the widget"))

    with (
        patch("app.skills.synthesizer.httpx.AsyncClient") as mock_client_cls,
        patch("app.skills.synthesizer.AsyncQdrantClient") as mock_qdrant_cls,
        patch.object(SkillSynthesizer, "_call_llm_doc", AsyncMock(return_value=_DOC_LLM_RAW)),
    ):
        mock_client_cls.return_value = _mock_embed_client()

        mock_qdrant = AsyncMock()
        mock_qdrant.retrieve = AsyncMock(return_value=[])
        mock_qdrant.set_payload = AsyncMock()
        mock_qdrant.upsert = AsyncMock()
        mock_qdrant.close = AsyncMock()
        mock_qdrant_cls.return_value = mock_qdrant

        synth = SkillSynthesizer(settings)
        result = await synth.synthesize_from_document(
            source_name="wiki-runbook",
            procedure_title="Restart the widget",
            doc_content="full document text ...",
        )

    assert result["id"] == expected_id
    point = mock_qdrant.upsert.call_args.kwargs["points"][0]
    assert str(point.id) == expected_id


@pytest.mark.asyncio
async def test_synthesize_from_document_reingest_upserts_in_place():
    """Re-ingesting the same source_name::procedure_title twice must target the
    same deterministic id (upsert), never create a second point."""
    settings = _doc_settings()
    expected_id = str(uuid.uuid5(SKILL_NS, "wiki-runbook::Restart the widget"))

    with (
        patch("app.skills.synthesizer.httpx.AsyncClient") as mock_client_cls,
        patch("app.skills.synthesizer.AsyncQdrantClient") as mock_qdrant_cls,
        patch.object(SkillSynthesizer, "_call_llm_doc", AsyncMock(return_value=_DOC_LLM_RAW)),
    ):
        mock_client_cls.side_effect = lambda *a, **k: _mock_embed_client()

        mock_qdrant = AsyncMock()
        # Second call's retrieve sees the draft point the first call wrote —
        # still a draft, so the active-guard does not engage.
        mock_qdrant.retrieve = AsyncMock(
            side_effect=[
                [],
                [MagicMock(id=expected_id, payload={"skill_status": "draft"})],
            ]
        )
        mock_qdrant.set_payload = AsyncMock()
        mock_qdrant.upsert = AsyncMock()
        mock_qdrant.close = AsyncMock()
        mock_qdrant_cls.return_value = mock_qdrant

        synth = SkillSynthesizer(settings)
        r1 = await synth.synthesize_from_document(
            source_name="wiki-runbook",
            procedure_title="Restart the widget",
            doc_content="v1 of the doc",
        )
        r2 = await synth.synthesize_from_document(
            source_name="wiki-runbook",
            procedure_title="Restart the widget",
            doc_content="v2 of the doc, updated",
        )

    assert r1["status"] == "drafted"
    assert r2["status"] == "drafted"
    assert r1["id"] == r2["id"] == expected_id
    assert mock_qdrant.upsert.call_count == 2
    ids_used = {str(c.kwargs["points"][0].id) for c in mock_qdrant.upsert.call_args_list}
    assert ids_used == {expected_id}  # one logical point, upserted twice


@pytest.mark.asyncio
async def test_synthesize_from_document_active_guard_blocks_overwrite():
    """A point already promoted to skill_status=active must not be clobbered
    by a re-ingest draft — it gets flagged needs_rereview=True instead."""
    settings = _doc_settings()
    expected_id = str(uuid.uuid5(SKILL_NS, "wiki-runbook::Restart the widget"))
    original_content = "ORIGINAL APPROVED CONTENT — do not touch"

    with (
        patch("app.skills.synthesizer.httpx.AsyncClient") as mock_client_cls,
        patch("app.skills.synthesizer.AsyncQdrantClient") as mock_qdrant_cls,
        patch.object(SkillSynthesizer, "_call_llm_doc", AsyncMock(return_value=_DOC_LLM_RAW)),
    ):
        mock_client_cls.return_value = _mock_embed_client()

        mock_qdrant = AsyncMock()
        mock_qdrant.retrieve = AsyncMock(
            return_value=[
                MagicMock(
                    id=expected_id,
                    payload={"skill_status": "active", "content": original_content},
                )
            ]
        )
        mock_qdrant.set_payload = AsyncMock()
        mock_qdrant.upsert = AsyncMock()
        mock_qdrant.close = AsyncMock()
        mock_qdrant_cls.return_value = mock_qdrant

        synth = SkillSynthesizer(settings)
        result = await synth.synthesize_from_document(
            source_name="wiki-runbook",
            procedure_title="Restart the widget",
            doc_content="a newer revision of the doc",
        )

    assert result == {"status": "rereview_flagged", "id": expected_id}
    mock_qdrant.upsert.assert_not_called()  # content never overwritten
    mock_qdrant.set_payload.assert_called_once()
    _, kwargs = mock_qdrant.set_payload.call_args
    assert kwargs["points"] == [expected_id]
    assert kwargs["payload"]["needs_rereview"] is True
    # SAFETY (never overwrite an approved skill): the merge must touch ONLY the
    # re-review flag — it must NOT carry the fresh draft's content/status/trigger/
    # symptoms, or set_payload's merge would silently clobber the approved skill.
    assert "content" not in kwargs["payload"]
    assert "skill_status" not in kwargs["payload"]
    assert "trigger" not in kwargs["payload"]
    assert "symptoms" not in kwargs["payload"]
