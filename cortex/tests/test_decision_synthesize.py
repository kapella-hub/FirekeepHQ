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

    `response_format` now carries the SCHEMA envelope, not the bare
    `{"type": "json_object"}` this asserted before structured outputs: measured
    on the VPS, `json_object` let qwen3:4b answer 0/3 questions on both runs by
    mirroring the user message back. `json_schema` is standard OpenAI, so this
    is still "standard fields only" — the vendor-flag half of the assertion is
    unchanged and is the half that guards `dreams/synthesize.py`'s mistake.

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
    rf = body["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "decision_suggestions"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["required"] == ["q0"]
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
    # `format` carries the SCHEMA OBJECT, not the string "json" — that string is
    # the setting under which the live board answered 0/3 questions twice.
    assert isinstance(body["format"], dict)
    assert body["format"]["required"] == ["q0"]
    assert body["options"] == {"temperature": 0.2}   # no num_predict — see above
    assert "response_format" not in body


@pytest.mark.parametrize("native_chat,expected_url", [
    ("never", "http://ollama:11434/v1/chat/completions"),
    ("always", "http://ollama:11434/api/chat"),
])
@pytest.mark.asyncio
async def test_the_configured_budget_bounds_both_endpoints(native_chat, expected_url):
    """One number for both, deliberately: a native sibling could only be LOWER,
    and phase 1 measured that a lower native budget strands non-thinking-model
    deploys, which take the native path and gain nothing from `think:false`.

    Parametrised over BOTH endpoints on purpose. Asserting only `/v1` would have
    left the "both" in the name unearned, since `_Settings` pins
    `LLM_NATIVE_CHAT="never"` and nothing else here overrides it.
    """
    rag = _rag_with_one_source()
    s = _settings()
    s.LLM_NATIVE_CHAT = native_chat
    s.DECISION_SYNTH_TIMEOUT_SECONDS = 12.5
    # Native reads `message.content`, /v1 reads `choices[0].message.content`;
    # a body carrying both is valid for whichever path runs.
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "message": {"role": "assistant", "content": "{}"},
        "choices": [{"message": {"content": "{}"}}],
    })
    client = _mock_client(resp)

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = client
        await synthesize_board("ctx", ["q1"], rag_engine=rag, settings=s)

    assert client.post.await_args.args[0] == expected_url
    # llm.chat builds its own client with the budget when none is injected.
    assert mc.call_args.kwargs["timeout"] == 12.5


# ---------------------------------------------------------------------------
# Structured outputs, and the detector for a payload that grounds nothing.
#
# THE PRODUCTION FAILURE THIS CLOSES, measured on the VPS 2026-08-04: under
# `format:"json"` the board completed in 15.07s, reported `degraded: False`, and
# every question came back `answers=0 actions=0`. The model had mirrored the
# USER MESSAGE back — `{"context": ..., "questions": [...]}` — so `.get("q0")`
# missed on every question, nothing raised, and a board that produced nothing
# reported itself healthy. Two separate defects: no shape constraint, and no
# check that the shape arrived.
# ---------------------------------------------------------------------------

def _schema_from(client):
    """Pull the schema out of whichever endpoint's body was actually posted."""
    body = client.post.await_args.kwargs["json"]
    if "format" in body:
        return body["format"]
    return body["response_format"]["json_schema"]["schema"]


@pytest.mark.asyncio
async def test_the_schema_names_every_question_id_in_properties_and_required():
    """`required` is the load-bearing half. `properties` alone describes a shape
    the model may decline to produce; naming every id in `required` is what
    makes the mirrored-input answer ungrammatical rather than merely
    discouraged."""
    rag = _rag_with_one_source()
    client = _mock_client(_openai_response("{}"))

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = client
        await synthesize_board("ctx", ["a?", "b?", "c?"], rag_engine=rag, settings=_settings())

    schema = _schema_from(client)
    assert sorted(schema["properties"]) == ["q0", "q1", "q2"]
    assert schema["required"] == ["q0", "q1", "q2"]
    assert schema["additionalProperties"] is False
    per_q = schema["properties"]["q0"]
    assert per_q["required"] == ["suggested_answers", "suggested_actions"]
    assert per_q["properties"]["suggested_answers"] == {
        "type": "array", "items": {"type": "string"}}


@pytest.mark.asyncio
async def test_the_schema_sets_no_minimum_item_count():
    """Measured and rejected: `minItems:1` cost 24.51s against 14.81–16.55s for
    the identical 3/3 result. Adherence was already total without it, so it buys
    latency plus pressure to invent a suggestion where the model has none."""
    rag = _rag_with_one_source()
    client = _mock_client(_openai_response("{}"))

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = client
        await synthesize_board("ctx", ["a?"], rag_engine=rag, settings=_settings())

    assert "minItems" not in json.dumps(_schema_from(client))


@pytest.mark.asyncio
async def test_a_board_with_no_questions_sends_no_schema():
    """A schema built from zero ids constrains output to the literal `{}` and is
    also the one shape OpenAI's strict mode has no use for. Plain json mode is
    the honest request for 'nothing to ask'."""
    rag = MagicMock()
    rag.recall = AsyncMock(return_value=_recall([]))
    client = _mock_client(_openai_response("{}"))

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = client
        board = await synthesize_board("ctx", [], rag_engine=rag, settings=_settings())

    assert client.post.await_args.kwargs["json"]["response_format"] == {"type": "json_object"}
    # ...and no questions means nothing was omitted, so the board is NOT degraded.
    assert board["degraded"] is False
    assert board["note"] == ""


@pytest.mark.asyncio
async def test_a_mirrored_input_payload_is_degraded_not_healthy():
    """The exact production payload. Well-formed JSON, HTTP 200, nothing raised
    — and it answers a different question than the one asked. A board keyed by
    `q0..qN` cannot be built from `{context, questions}`, and saying so is the
    difference between a bug that took three phases to find and one that names
    itself in the first log line."""
    rag = _rag_with_one_source()
    mirrored = json.dumps({
        "context": "Rolling out memory consolidation to production.",
        "questions": [{"id": "q0", "text": "restart?", "evidence_snippets": []}],
    })

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = _mock_client(_openai_response(mirrored))
        board = await synthesize_board("ctx", ["restart?"], rag_engine=rag, settings=_settings())

    assert board["degraded"] is True
    assert board["note"] == "suggestions-unusable"
    q = board["questions"][0]
    assert q["suggested_answers"] == [] and q["suggested_actions"] == []
    # Retrieval is never blocked by the suggestion pass — the standing contract.
    assert q["knowledge_found"] is True
    assert q["evidence"][0]["snippet"] == "the runbook says restart"


@pytest.mark.asyncio
async def test_ids_that_match_but_carry_nothing_are_degraded_as_empty():
    """A distinct note, because it needs a distinct response: `unusable` means
    the model answered a different question, `empty` means it answered this one
    with nothing. Both are boards that produced nothing, and neither may report
    itself healthy — that shape is the whole defect."""
    rag = _rag_with_one_source()
    payload = json.dumps({"q0": {"suggested_answers": [], "suggested_actions": []}})

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = _mock_client(_openai_response(payload))
        board = await synthesize_board("ctx", ["restart?"], rag_engine=rag, settings=_settings())

    assert board["degraded"] is True
    assert board["note"] == "suggestions-empty"


@pytest.mark.asyncio
async def test_one_grounded_question_out_of_several_is_not_degraded():
    """The detector fires on a board that grounded NOTHING, not on a model that
    had nothing for one question. Over-reporting `degraded` would make the flag
    mean 'a board was served' and cost it the meaning this change gives it."""
    rag = _rag_with_one_source()
    payload = json.dumps({
        "q0": {"suggested_answers": [], "suggested_actions": []},
        "q1": {"suggested_answers": ["Ship it behind a flag"], "suggested_actions": []},
    })

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = _mock_client(_openai_response(payload))
        board = await synthesize_board("ctx", ["a?", "b?"], rag_engine=rag, settings=_settings())

    assert board["degraded"] is False
    assert board["note"] == ""
    assert board["questions"][1]["suggested_answers"] == ["Ship it behind a flag"]


@pytest.mark.asyncio
async def test_one_malformed_entry_does_not_abandon_the_other_questions():
    """`suggestions.get(id) or {}` followed by `.get` raised AttributeError on a
    non-dict value, halfway through the loop — leaving the questions before it
    assigned and every one after it untouched, with no record of where it
    stopped. One bad entry is not a reason to drop the good ones."""
    rag = _rag_with_one_source()
    payload = json.dumps({
        "q0": ["not", "a", "dict"],
        "q1": {"suggested_answers": ["Deploy to staging first"], "suggested_actions": []},
    })

    with patch("app.llm.httpx.AsyncClient") as mc:
        mc.return_value = _mock_client(_openai_response(payload))
        board = await synthesize_board("ctx", ["a?", "b?"], rag_engine=rag, settings=_settings())

    assert board["questions"][0]["suggested_answers"] == []
    assert board["questions"][1]["suggested_answers"] == ["Deploy to staging first"]
    assert board["degraded"] is False
