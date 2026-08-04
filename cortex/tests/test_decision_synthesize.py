from __future__ import annotations
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.decision.synthesize import synthesize_board
from app.models import MemorySource, RecallResponse


def _recall(sources):
    r = MagicMock(spec=RecallResponse)
    r.sources = sources
    r.context_block = ""
    return r


class _Settings:
    """Plain stub, not a MagicMock — `app.llm` type-guards `LLM_BASE_URL` and
    `LLM_NATIVE_BASE_URL` to `str`, so a MagicMock silently resolves to "no
    native root" and every endpoint assertion below would be testing the
    fallback by accident.

    `LLM_NATIVE_CHAT="never"` pins the default cases to `/v1`, which is the
    shape their fixtures build. On the real default ("auto") these tests would
    fire a live `GET http://ollama:11434/api/version` — which happens to fail in
    CI and happens to land back on `/v1`, so they would pass for a reason
    unrelated to what they assert, and would flip on any dev box running ollama.
    The native path gets its own explicit case below.
    """

    LLM_BASE_URL = "http://ollama:11434/v1"
    LLM_MODEL = "qwen3:4b"
    LLM_API_KEY = ""
    LLM_NATIVE_CHAT = "never"
    DECISION_MAX_QUESTIONS = 8
    DECISION_SYNTH_TIMEOUT_SECONDS = 30.0


def _settings():
    return _Settings()


def _openai_response(content: str, reasoning: str | None = None):
    message: dict = {"content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"choices": [{"message": message}]})
    return resp


def _mock_client(resp=None, side_effect=None):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if side_effect is not None:
        client.post = AsyncMock(side_effect=side_effect)
    else:
        client.post = AsyncMock(return_value=resp)
    return client


def _rag_with_one_source():
    src = MemorySource(store="vector", content="the runbook says restart", score=0.9,
                       metadata={"id": "m1"})
    rag = MagicMock()
    rag.recall = AsyncMock(return_value=_recall([src]))
    return rag


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


# ---------------------------------------------------------------------------
# The real suggestion pass, through the app.llm seam (LLM endpoint phase 2).
#
# Every case above patches `_llm_suggest` out, which is why the silent-success
# bug below survived review: the only code that ever ran it was production.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_suggestions_land_on_their_questions():
    rag = _rag_with_one_source()
    resp = _openai_response(json.dumps({
        "q0": {"suggested_answers": ["Restart the worker"],
               "suggested_actions": ["Run scripts/restart_worker.sh"]},
    }))

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = _mock_client(resp)
        board = await synthesize_board("ctx", ["restart?"], rag_engine=rag, settings=_settings())

    assert board["degraded"] is False
    assert board["note"] == ""
    assert board["questions"][0]["suggested_answers"] == ["Restart the worker"]
    assert board["questions"][0]["suggested_actions"] == ["Run scripts/restart_worker.sh"]


@pytest.mark.asyncio
async def test_connect_error_marks_the_board_degraded_and_keeps_the_evidence():
    """The silent-success regression, and the contract it violated.

    `_llm_suggest` used to answer ANY exception with `{}`, so a generation-less
    deploy — the office embed-only ollama image, a real shipped configuration —
    produced `degraded: false, note: ""` on every board it ever served. A caller
    could not distinguish "the model had no suggestions" from "there is no model".

    The second half of the assertion is the standing contract from the design:
    retrieval must never be blocked by the LLM pass, so evidence and
    knowledge_found survive the failure intact.
    """
    rag = _rag_with_one_source()

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = _mock_client(side_effect=httpx.ConnectError("no route"))
        board = await synthesize_board("ctx", ["restart?"], rag_engine=rag, settings=_settings())

    assert board["degraded"] is True
    assert board["note"] == "retrieval-only"
    q = board["questions"][0]
    assert q["suggested_answers"] == [] and q["suggested_actions"] == []
    assert q["knowledge_found"] is True
    assert q["evidence"][0]["snippet"] == "the runbook says restart"


@pytest.mark.asyncio
async def test_http_error_marks_the_board_degraded():
    rag = _rag_with_one_source()
    err = httpx.HTTPStatusError(
        "500", request=httpx.Request("POST", "http://ollama:11434/v1/chat/completions"),
        response=httpx.Response(500))

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = _mock_client(side_effect=err)
        board = await synthesize_board("ctx", ["q1"], rag_engine=rag, settings=_settings())

    assert board["degraded"] is True


@pytest.mark.asyncio
async def test_unparseable_completion_marks_the_board_degraded():
    rag = _rag_with_one_source()

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = _mock_client(_openai_response("I think you should probably..."))
        board = await synthesize_board("ctx", ["q1"], rag_engine=rag, settings=_settings())

    assert board["degraded"] is True
    assert board["questions"][0]["suggested_answers"] == []


@pytest.mark.asyncio
async def test_json_array_instead_of_object_marks_the_board_degraded():
    """Valid JSON of the wrong shape. It used to `return {}` — indistinguishable
    from success — and now raises, because a board keyed by question id cannot
    be built from a list."""
    rag = _rag_with_one_source()

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = _mock_client(_openai_response('["q0"]'))
        board = await synthesize_board("ctx", ["q1"], rag_engine=rag, settings=_settings())

    assert board["degraded"] is True


@pytest.mark.asyncio
async def test_reasoning_field_is_not_used_as_a_fallback():
    """The deleted rescue. Under JSON mode an empty `content` means the grammar
    blocked the output, so `reasoning` is prose by construction and can never be
    the JSON — the old fallback fed it to json.loads and got a JSONDecodeError
    for its trouble. Same terminal state, one less line pretending to recover."""
    rag = _rag_with_one_source()
    resp = _openai_response("", reasoning=json.dumps({"q0": {"suggested_answers": ["nope"]}}))

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = _mock_client(resp)
        board = await synthesize_board("ctx", ["q1"], rag_engine=rag, settings=_settings())

    assert board["degraded"] is True
    assert board["questions"][0]["suggested_answers"] == []


@pytest.mark.asyncio
async def test_openai_body_carries_no_vendor_flags_and_no_output_cap():
    """`/v1` gets standard OpenAI fields only.

    The `max_tokens` absence is asserted, not incidental: this call is JSON
    mode, so the grammar ends generation on its own and a cap could only
    truncate a valid object into an invalid one. Contrast
    skills/synthesizer.py, whose free-form card has nothing to close it and
    which therefore DOES send one.
    """
    rag = _rag_with_one_source()
    client = _mock_client(_openai_response("{}"))

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = client
        await synthesize_board("ctx", ["q1"], rag_engine=rag, settings=_settings())

    url = client.post.await_args.args[0]
    body = client.post.await_args.kwargs["json"]
    assert url == "http://ollama:11434/v1/chat/completions"
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0.2
    assert "max_tokens" not in body
    assert "think" not in body and "chat_template_kwargs" not in body


@pytest.mark.asyncio
async def test_native_body_when_the_backend_is_ollama():
    """`stream:False` is what stops ollama streaming NDJSON into `resp.json()`;
    `think:False` is the entire reason this endpoint is worth selecting."""
    rag = _rag_with_one_source()
    s = _settings()
    s.LLM_NATIVE_CHAT = "always"
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "message": {"role": "assistant", "content": json.dumps(
            {"q0": {"suggested_answers": ["Restart"], "suggested_actions": []}})},
        "done": True,
    })
    client = _mock_client(resp)

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = client
        board = await synthesize_board("ctx", ["q1"], rag_engine=rag, settings=s)

    assert board["degraded"] is False
    assert board["questions"][0]["suggested_answers"] == ["Restart"]

    url = client.post.await_args.args[0]
    body = client.post.await_args.kwargs["json"]
    assert url == "http://ollama:11434/api/chat"
    assert body["stream"] is False
    assert body["think"] is False
    assert body["format"] == "json"
    assert body["options"] == {"temperature": 0.2}   # no num_predict — see above
    assert "response_format" not in body


@pytest.mark.asyncio
async def test_the_configured_budget_bounds_both_endpoints():
    """One number for both, deliberately: a native sibling could only be LOWER,
    and phase 1 measured that a lower native budget strands non-thinking-model
    deploys, which take the native path and gain nothing from `think:false`."""
    rag = _rag_with_one_source()
    s = _settings()
    s.DECISION_SYNTH_TIMEOUT_SECONDS = 12.5
    client = _mock_client(_openai_response("{}"))

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = client
        await synthesize_board("ctx", ["q1"], rag_engine=rag, settings=s)

    # llm.chat builds its own client with the budget when none is injected.
    assert mc.call_args.kwargs["timeout"] == 12.5
