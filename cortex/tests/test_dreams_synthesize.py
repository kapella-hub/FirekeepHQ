import json

import httpx
import pytest

from app.dreams import synthesize as syn
from app.dreams.select import Candidate


def _members(n=4):
    return [Candidate(id=f"m{i}", text=f"episode {i}", vector=[1.0], payload={}) for i in range(n)]


class _S:
    """Settings stub for the llm.chat path.

    `LLM_NATIVE_CHAT` defaults to "always" so `is_native` returns True WITHOUT
    probing (see llm.is_native — `always` returns before the cache and before
    any network call). That matters here: the probe builds its own
    httpx.AsyncClient, NOT the MockTransport-backed one these tests inject, so
    a test left on "auto" would make a real DNS lookup for `http://x/api/version`
    and then silently take the /v1 branch for whatever reason it failed. Tests
    that want the /v1 branch ask for it with LLM_NATIVE_CHAT="never".
    """

    def __init__(self, **kw):
        self.LLM_BASE_URL = kw.pop("LLM_BASE_URL", "http://x/v1")
        self.LLM_MODEL = kw.pop("LLM_MODEL", "qwen3:4b")
        self.LLM_API_KEY = kw.pop("LLM_API_KEY", "")
        self.LLM_NATIVE_CHAT = kw.pop("LLM_NATIVE_CHAT", "always")
        self.LLM_NATIVE_PROBE_TTL_SECONDS = kw.pop("LLM_NATIVE_PROBE_TTL_SECONDS", 600.0)
        self.LLM_NATIVE_BASE_URL = kw.pop("LLM_NATIVE_BASE_URL", "")
        self.DREAM_SYNTH_TIMEOUT_SECONDS = kw.pop("DREAM_SYNTH_TIMEOUT_SECONDS", 45.0)
        for k, v in kw.items():
            setattr(self, k, v)


def test_request_body_still_serves_the_unconverted_profile_call():
    """`build_request_body` is no longer synthesize()'s — llm.chat builds that
    body now. It survives because `profile.py` imports it and still posts to
    /v1 itself, so these assertions moved subject rather than expiring: they
    now pin the shape of the PROFILE request. Deleting them would leave that
    call's `think:false` and completion budget unguarded on the strength of a
    conversion that did not touch it."""
    body = syn.build_request_body("qwen3:4b", [{"role": "user", "content": "x"}])
    assert body["think"] is False
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    assert body["response_format"] == {"type": "json_object"}


def test_completion_budget_absorbs_blocked_reasoning_tokens():
    """The flags above are IGNORED on ollama's /v1/chat/completions. Measured
    live against ollama 0.17.5 with this exact body, 3 probes of 3:
    max_tokens=700 gave HTTP 200, finish_reason='length',
    completion_tokens=700, content length ZERO and ~3200 chars of reasoning;
    the same call at 4000 returned correct JSON. Two of three live clusters
    produced no insights at all because of it.

    Asserts the FLOOR as well as the constant: the failure this guards is a
    later "tidy-up" quietly lowering the number back toward the answer size
    (~200-400 tokens), which looks reasonable and silently reinstates the
    starvation.

    Still load-bearing after the llm.chat conversion, on both halves of the
    constant's remaining job: `profile.py` reads it through this body, and
    synthesize() passes the same constant as `llm.chat(max_tokens=...)` — see
    the sibling test that asserts it reaches the wire as `options.num_predict`.
    """
    body = syn.build_request_body("qwen3:4b", [{"role": "user", "content": "x"}])
    assert body["max_tokens"] == syn._MAX_COMPLETION_TOKENS
    assert body["max_tokens"] >= 4000, (
        "4000 is the empirically verified working value; anything below it was "
        "measured returning empty content"
    )


def test_messages_carry_indexed_episodes():
    msgs = syn.build_messages(_members(3))
    joined = " ".join(m["content"] for m in msgs)
    assert "[0]" in joined and "episode 2" in joined


def test_build_messages_states_the_char_budget():
    msgs = syn.build_messages(_members(2), max_chars=450)
    system_msg = next(m["content"] for m in msgs if m["role"] == "system")
    assert "450" in system_msg


def _raw(**kw):
    ins = {"content": "a durable lesson", "memory_type": "procedural", "source_indices": [0, 1]}
    ins.update(kw)
    return json.dumps({"insights": [ins]})


def test_parse_maps_indices_to_real_ids():
    got = syn.parse_insights(_raw(), _members(), max_chars=800)
    assert len(got) == 1
    assert got[0].source_ids == ["m0", "m1"]


def test_parse_rejects_overlong_insight():
    assert syn.parse_insights(_raw(content="x" * 900), _members(), max_chars=800) == []


def test_parse_forces_procedural_never_reference():
    got = syn.parse_insights(_raw(memory_type="reference"), _members(), max_chars=800)
    assert got and got[0].memory_type == "procedural"


def test_parse_rejects_out_of_range_indices():
    assert syn.parse_insights(_raw(source_indices=[99]), _members(), max_chars=800) == []


def test_parse_rejects_empty_content():
    assert syn.parse_insights(_raw(content="   "), _members(), max_chars=800) == []


@pytest.mark.parametrize("bad", ["", "not json", "{}", '{"insights": "nope"}', '{"insights": [1]}'])
def test_parse_never_raises_on_garbage(bad):
    assert syn.parse_insights(bad, _members(), max_chars=800) == []


def test_parse_caps_at_three_insights():
    many = json.dumps({"insights": [
        {"content": f"c{i}", "memory_type": "procedural", "source_indices": [0]} for i in range(9)
    ]})
    assert len(syn.parse_insights(many, _members(), max_chars=800)) == 3


@pytest.mark.asyncio
async def test_synthesize_sends_think_false_to_ollamas_native_endpoint():
    """The conversion, asserted where it actually pays: the URL.

    `think:false` was already being SENT before this change — it was being sent
    to `/v1/chat/completions`, which throws it away. Measured on the VPS
    2026-08-04 with the smallest cluster this pass ever attempts (4 members, a
    2,595-char prompt): >400s on /v1 against a 45s budget, versus 22.5s native.
    So asserting the flag alone is not enough and never was; the endpoint is
    the assertion that distinguishes working from inoperable.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": _raw()}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await syn.synthesize(_members(), settings=_S(), max_chars=800, client=client)

    assert seen["url"] == "http://x/api/chat"
    body = seen["body"]
    assert body["think"] is False
    # MEASURED (llm.py probe C): omit `stream` and ollama answers NDJSON,
    # resp.json() raises, and the call fails for a reason unrelated to dreams.
    assert body["stream"] is False
    assert body["format"] == "json"
    # Native tuning lives in `options`; the /v1 spellings must not leak here.
    assert body["options"] == {"temperature": 0.2, "num_predict": syn._MAX_COMPLETION_TOKENS}
    for absent in ("max_tokens", "response_format", "chat_template_kwargs"):
        assert absent not in body
    assert len(out) == 1


@pytest.mark.asyncio
async def test_synthesize_falls_back_to_v1_for_a_non_ollama_backend():
    """The native endpoint is the optimisation, /v1 is the contract. A backend
    that does not confirm as ollama must still get a well-formed, STANDARD
    OpenAI body — no `think`, no `chat_template_kwargs`, which real OpenAI 400s
    on. This is a capability the old code did not have: it sent the vendor
    flags unconditionally."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": _raw()}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await syn.synthesize(
            _members(), settings=_S(LLM_NATIVE_CHAT="never"), max_chars=800, client=client,
        )

    assert seen["url"] == "http://x/v1/chat/completions"
    body = seen["body"]
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == syn._MAX_COMPLETION_TOKENS
    for absent in ("think", "chat_template_kwargs", "format", "options", "stream"):
        assert absent not in body
    assert len(out) == 1


@pytest.mark.asyncio
async def test_synthesize_bounds_the_call_with_the_configured_dream_budget():
    """DREAM_SYNTH_TIMEOUT_SECONDS is read off `settings` now that the caller
    no longer passes a timeout, and it bounds BOTH endpoints — there is
    deliberately no native sibling (see synthesize()'s docstring: a lower
    native budget is what strands a non-thinking-model ollama deploy)."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"message": {"content": _raw()}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await syn.synthesize(
            _members(), settings=_S(DREAM_SYNTH_TIMEOUT_SECONDS=45.0),
            max_chars=800, client=client,
        )
    assert seen["timeout"]["read"] == 45.0


@pytest.mark.asyncio
async def test_empty_content_yields_no_insights_and_is_never_rescued_by_reasoning():
    """REPLACES test_synthesize_reads_reasoning_when_content_empty. That
    behaviour is REMOVED, not merely untested, so the assertion is inverted
    rather than deleted: a response whose `content` is empty and whose
    `reasoning` holds perfectly good JSON must now yield [].

    The rationale is knowledge/classifier.py's, applied unchanged: under JSON
    mode, empty content means the grammar BLOCKED the output, so `reasoning` is
    prose by construction and json.loads rejects it — the fallback read as a
    recovery mechanism while never recovering anything. This test builds the
    one payload that could have exercised it (valid JSON in `reasoning`) and
    pins that we no longer look there.

    Empty content is malformed JSON, so it takes the retry arm: two calls, not
    one. That is the pre-existing rule preserved, not a new behaviour.
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"message": {"content": "", "thinking": _raw()}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await syn.synthesize(_members(), settings=_S(), max_chars=800, client=client)

    assert out == []
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_malformed_model_json_is_retried_exactly_once():
    """Half of the retry contract: only the model's OWN OUTPUT is worth asking
    twice for. First call returns valid-HTTP garbage, second returns real JSON
    — one insight, two calls."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        content = "not json at all" if len(calls) == 1 else _raw()
        return httpx.Response(200, json={"message": {"content": content}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await syn.synthesize(_members(), settings=_S(), max_chars=800, client=client)

    assert len(out) == 1
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_synthesize_returns_empty_on_backend_error_never_raises():
    """The other half: a non-2xx is NOT retried, and does not escape.

    `llm.chat` raises `httpx.HTTPStatusError` where this function used to call
    `raise_for_status()` itself — a new exception type crossing the guard, so
    "never raises" is re-verified against it rather than assumed. Note llm.chat
    itself performs no fallback here: 500 is not in `_DEMOTE_STATUS_CODES`
    (those are pre-generation 4xx; a 5xx may arrive mid-generation), so exactly
    one request goes out.
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await syn.synthesize(
            _members(), settings=_S(), max_chars=800, client=client,
        ) == []
    assert len(calls) == 1, "a backend failure is not the model's fault — no retry"


@pytest.mark.asyncio
async def test_synthesize_returns_empty_when_settings_are_unusable():
    """`llm.chat` reads `settings.LLM_MODEL` when it builds the body, so a
    settings object missing it raises AttributeError from INSIDE the call —
    an exception class that could not previously reach this function. The guard
    is Exception-shaped, not type-enumerated, so it lands in `return []` like
    everything else; asserted rather than assumed."""
    class _Broken:
        LLM_BASE_URL = "http://x/v1"
        LLM_NATIVE_CHAT = "never"

    assert await syn.synthesize(_members(), settings=_Broken(), max_chars=800) == []


@pytest.mark.asyncio
async def test_synthesize_never_raises_on_malformed_candidate():
    """A cluster member with a corrupt payload (non-string .text, e.g. from a
    bad Qdrant record) must degrade to [] rather than raising — the Task 6
    orchestrator loops many clusters in a background pass and one bad payload
    must not kill the run.

    Deliberately passes NO client: build_messages raises before any request is
    made, which is what proves the guard still wraps request construction and
    not just the call."""
    bad = [Candidate(id="m0", text=None, vector=[1.0], payload={})]
    out = await syn.synthesize(bad, settings=_S(), max_chars=800)
    assert out == []
