import contextlib
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import llm
from app.dreams import select as sel
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
        self.DREAM_SYNTH_TIMEOUT_SECONDS = kw.pop("DREAM_SYNTH_TIMEOUT_SECONDS", 90.0)
        for k, v in kw.items():
            setattr(self, k, v)


def test_build_request_body_is_gone_now_that_nothing_builds_a_v1_body_by_hand():
    """REPLACES test_request_body_still_serves_the_unconverted_profile_call.

    That test guarded ONE thing: that the hand-rolled `/v1` body builder kept
    carrying `think:false` and the completion budget for `profile.py`, the last
    caller left after synthesize() moved to llm.chat. profile.py has now been
    converted too, so the function has no callers and is deleted — and the
    property it guarded did not evaporate, it MOVED, to three places that now
    own it:

      * `think:false` -> unconditional in `llm.build_native_body`, pinned by
        test_llm.py::test_every_native_body_sets_stream_false (parametrized,
        asserts `think is False` on every argument combination) and by this
        file's own over-the-wire native test;
      * the completion budget -> `_MAX_COMPLETION_TOKENS`, still asserted below
        and asserted on the wire as `options.num_predict` for BOTH callers;
      * "no vendor flags on /v1" -> llm.build_openai_body, pinned by
        test_llm.py::test_openai_body_sends_only_standard_openai_fields and by
        the /v1-fallback tests here and in test_dreams_profile.py.

    Asserting the absence is the point: a resurrected hand-built body is how
    this module's docstring came to lie about which endpoint it posted to, and
    a deleted guard leaves no trace of why."""
    assert not hasattr(syn, "build_request_body")


def test_completion_budget_absorbs_blocked_reasoning_tokens():
    """Measured live against ollama 0.17.5 on `/v1`, 3 probes of 3:
    max_tokens=700 gave HTTP 200, finish_reason='length',
    completion_tokens=700, content length ZERO and ~3200 chars of reasoning;
    the same call at 4000 returned correct JSON. Two of three live clusters
    produced no insights at all because of it — the flags that were supposed to
    prevent the reasoning are ignored on that endpoint.

    Asserts the FLOOR as well as the constant: the failure this guards is a
    later "tidy-up" quietly lowering the number back toward the answer size
    (~200-400 tokens), which looks reasonable and silently reinstates the
    starvation. Still live because `/v1` is still reachable — any non-ollama
    backend, or an ollama demoted by a pre-generation 4xx.

    Now asserted on the CONSTANT directly rather than through the deleted body
    builder. Both dreams callers pass it to `llm.chat(max_tokens=...)`; the
    sibling tests here and in test_dreams_profile.py assert it reaches the wire.
    """
    assert syn._MAX_COMPLETION_TOKENS >= 4000, (
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
            _members(), settings=_S(DREAM_SYNTH_TIMEOUT_SECONDS=90.0),
            max_chars=800, client=client,
        )
    assert seen["timeout"]["read"] == 90.0


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


# ---------------------------------------------------------------------------
# The PRODUCTION default, LLM_NATIVE_CHAT="auto"
#
# Every test above pins `always`, which `llm.is_native` answers BEFORE the probe
# and before the cache. That was the right call for them — the probe builds its
# own httpx.AsyncClient, not the MockTransport one they inject — but it left the
# mode dreams actually ships with unexercised from this side, and with it the
# native->/v1 demote path, which is the safety net that keeps an older ollama
# working. The probe client is patched here the same way test_llm.py patches it.
#
# `tests/conftest.py` has an autouse fixture that resets llm.py's module-global
# verdict cache around every test. Without it a verdict decided by one of these
# tests would leak into every later test in the process, order-dependently.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _probe(*responses):
    """Patch the native-endpoint probe's own client. Yields the probed URLs.

    Lifted from test_llm.py rather than imported: a cross-test-module import
    would make this file's coverage hostage to a helper it does not own. Each
    element is a Response or an Exception; the last repeats.
    """
    urls: list[str] = []

    def _factory(*_a, **_k):
        c = AsyncMock()
        c.__aenter__ = AsyncMock(return_value=c)
        c.__aexit__ = AsyncMock(return_value=False)

        async def _get(url, **_kw):
            urls.append(url)
            item = responses[min(len(urls) - 1, len(responses) - 1)]
            if isinstance(item, Exception):
                raise item
            return item

        c.get = _get
        return c

    with patch("app.llm.httpx.AsyncClient", side_effect=_factory):
        yield urls


@pytest.mark.asyncio
async def test_auto_mode_probes_and_takes_the_native_endpoint_on_a_confirmed_ollama():
    """The shipped default. `auto` derives the root from LLM_BASE_URL, probes it
    once, and — on a 2xx carrying a JSON object with a `version` key — sends the
    synthesis to /api/chat, which is the whole point of the conversion."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"message": {"content": _raw()}})

    # The transport-backed client MUST be built OUTSIDE `_probe`: that patch
    # replaces httpx.AsyncClient itself (app.llm.httpx IS the httpx module), so
    # a client constructed inside it is an AsyncMock, not a transport.
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with _probe(httpx.Response(200, json={"version": "0.32.4"})) as probed:
            out = await syn.synthesize(
                _members(), settings=_S(LLM_NATIVE_CHAT="auto"), max_chars=800,
                client=client,
            )

    assert probed == ["http://x/api/version"]
    assert seen["url"] == "http://x/api/chat"
    assert len(out) == 1


@pytest.mark.asyncio
async def test_auto_mode_falls_back_to_v1_when_the_probe_does_not_confirm_ollama():
    """Failing toward /v1 is the contract; native is the optimisation. A vLLM /
    LiteLLM / OpenAI backend never confirms and must still get a synthesis — on
    the standard endpoint, with no ollama vendor flags in the body."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": _raw()}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with _probe(httpx.ConnectError("no ollama here")) as probed:
            out = await syn.synthesize(
                _members(), settings=_S(LLM_NATIVE_CHAT="auto"), max_chars=800,
                client=client,
            )

    assert probed == ["http://x/api/version"]
    assert seen["url"] == "http://x/v1/chat/completions"
    for absent in ("think", "chat_template_kwargs", "format", "options"):
        assert absent not in seen["body"]
    assert len(out) == 1


@pytest.mark.asyncio
async def test_a_pre_generation_4xx_on_native_demotes_and_the_retry_lands_on_v1():
    """The escape hatch for an ollama old enough to reject `think`. The probe
    confirms the backend IS ollama, so the call goes native and is refused with
    a pre-generation 400; llm.chat demotes the cached verdict and retries once
    on /v1. Dreams must come back with its insight, not with [].

    Exercised from THIS side, not only in test_llm.py, because what a failed
    demote costs is dreams-specific: `synthesize` never retries on a transport
    or HTTP failure, so if that fallback did not fire the whole cluster would
    produce nothing and be re-attempted on every later tick."""
    urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.path == "/api/chat":
            return httpx.Response(400, text="unknown field think")
        return httpx.Response(200, json={"choices": [{"message": {"content": _raw()}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with _probe(httpx.Response(200, json={"version": "0.11.0"})):
            out = await syn.synthesize(
                _members(), settings=_S(LLM_NATIVE_CHAT="auto"), max_chars=800,
                client=client,
            )

    assert urls == ["http://x/api/chat", "http://x/v1/chat/completions"]
    assert len(out) == 1, "the /v1 fallback must rescue the cluster, not lose it"
    # The demotion is what makes this cheap on the NEXT call rather than one
    # wasted native round trip per synthesis forever.
    assert llm._probe_cache["http://x"][0] is False


def test_the_hardcoded_timeout_fallback_tracks_the_config_default():
    """`synthesize()` and `synthesize_profile()` both read the budget as
    `getattr(settings, "DREAM_SYNTH_TIMEOUT_SECONDS", 90.0)` — a literal copy of
    config.py's default, which can silently drift if that default is ever
    changed. Keeping the literal is deliberate: the fallback is unreachable in
    production, where `settings` is always the real Settings object, so it
    exists only for duck-typed stubs, and importing Settings into these modules
    to source it would add a config dependency for a value production never
    reads. What is NOT acceptable is the drift being silent, so it is pinned
    here instead — the same mechanism as the compose/.env default-drift guards
    in test_decision_config.py."""
    from app.config import Settings

    assert Settings.model_fields["DREAM_SYNTH_TIMEOUT_SECONDS"].default == 90.0, (
        "config default moved; update the literal fallback in "
        "dreams/synthesize.py and dreams/profile.py to match"
    )


@pytest.mark.asyncio
async def test_a_settings_stub_without_the_budget_field_falls_back_to_the_config_default():
    """The behavioural half of the guard above: prove the literal is what a
    settings object lacking the field actually produces on the wire, so the two
    assertions together cover the path rather than the constant alone."""
    from app.config import Settings

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"message": {"content": _raw()}})

    class _NoBudget:
        LLM_BASE_URL = "http://x/v1"
        LLM_MODEL = "qwen3:4b"
        LLM_API_KEY = ""
        LLM_NATIVE_CHAT = "always"
        LLM_NATIVE_BASE_URL = ""

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await syn.synthesize(_members(), settings=_NoBudget(), max_chars=800, client=client)

    assert seen["timeout"]["read"] == (
        Settings.model_fields["DREAM_SYNTH_TIMEOUT_SECONDS"].default
    )


# --------------------------------------------------------------------------
# Cluster sampling (DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS)
#
# synthesize() sends at most N of a cluster's members. Measured on the live
# store 2026-08-04 (VPS, qwen3:4b, 4 vCPU, native /api/chat, 45s budget), 526
# candidates / 20 clusters sized [23,16,10,10,10,9,8,8,7,6,6,5]: 4 members ->
# an uncapped 23-member cluster (~15,000 chars) never completed, which is what
# the real tick hit. Single probes per size suggested a clean size->latency
# ordering; repeating each capped prompt 3x dissolved it — cap 4 spanned
# 12.0-42.9s on IDENTICAL input, cap 5 gave 29.8-35.8s, cap 6 gave 25.1-35.8s.
# All of 4..6 fit; uncapped never does. Capping also improved output (4 -> 1
# insight, 6 -> 3 specific ones, uncapped -> 0). See config.py for the table.
# --------------------------------------------------------------------------


def _spread(n):
    """Members whose vectors fan out, so centrality actually discriminates."""
    return [
        Candidate(id=f"m{i:02d}", text=f"episode {i}", vector=[1.0, i * 0.05], payload={})
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_synthesize_caps_how_many_episodes_reach_the_prompt():
    """The measured defect: a 23-member cluster built a ~15,000-char prompt and
    blew the budget. Assert on the WIRE, because the prompt is the thing that
    cost the time — a unit test of sample_cluster alone would not prove the
    sample is what got sent."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": _raw()}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await syn.synthesize(
            _spread(23),
            settings=_S(DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS=5),
            max_chars=800, client=client,
        )

    user = next(m["content"] for m in seen["body"]["messages"] if m["role"] == "user")
    assert len(user.splitlines()) == 5
    assert "[5]" not in user, "indices must be 0..4 — they index the SAMPLE"


@pytest.mark.asyncio
async def test_a_cluster_under_the_cap_produces_a_byte_identical_prompt():
    """No behaviour change for a cluster that already fitted. Captured by
    running the same members with the cap on and with it effectively off and
    comparing the request bodies — the assertion the "small clusters are
    unaffected" requirement actually needs."""
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": _raw()}})

    members = _spread(4)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await syn.synthesize(
            members, settings=_S(DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS=5),
            max_chars=800, client=client,
        )
        await syn.synthesize(
            members, settings=_S(DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS=0),
            max_chars=800, client=client,
        )

    assert bodies[0] == bodies[1]


@pytest.mark.asyncio
async def test_source_indices_map_to_the_SAMPLED_members_not_the_whole_cluster():
    """The silent-corruption case this cap could have introduced.

    The model is shown 5 of 23 and cites index 4. If parse_insights were handed
    the CLUSTER instead of the SAMPLE, index 4 would resolve to the cluster's
    fifth member — a real id, in range, and the wrong memory. No exception, no
    log line, just a dream attributed to something it was never shown.

    The sample here is the 5 nearest the centroid of a fanned-out 23, so it is
    NOT the first five by id — which is what makes the two mappings disagree
    and lets this test fail if they are ever confused.
    """
    members = _spread(23)
    expected = [m.id for m in sel.sample_cluster(members, 5)]
    assert expected != [m.id for m in members[:5]], "sample must differ from the prefix"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": _raw(source_indices=[0, 4])}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await syn.synthesize(
            members, settings=_S(DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS=5),
            max_chars=800, client=client,
        )

    assert len(out) == 1
    assert out[0].source_ids == [expected[0], expected[4]]


@pytest.mark.asyncio
async def test_an_index_past_the_sample_is_rejected_even_though_the_cluster_is_longer():
    """Range validation must be against the SAMPLE. Index 7 is out of range for
    a 5-member prompt but perfectly in range for the 23-member cluster, so a
    validator pointed at the cluster would accept a citation the model could
    not have made."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": _raw(source_indices=[7])}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await syn.synthesize(
            _spread(23), settings=_S(DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS=5),
            max_chars=800, client=client,
        )
    assert out == []


@pytest.mark.asyncio
async def test_insight_records_how_many_episodes_it_was_shown():
    """`sample_size` is what lets the write path say "5 of 23" instead of
    implying the model read all 23. It is what the model SAW, deliberately not
    len(source_ids), which is only what it cited."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": _raw(source_indices=[0, 1])}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await syn.synthesize(
            _spread(23), settings=_S(DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS=5),
            max_chars=800, client=client,
        )
    assert out[0].sample_size == 5
    assert len(out[0].source_ids) == 2


def test_parse_insights_records_the_sample_size_it_validated_against():
    got = syn.parse_insights(_raw(), _members(4), max_chars=800)
    assert got[0].sample_size == 4


def test_hand_built_insight_defaults_to_unrecorded_sample_size():
    """Every pre-existing caller/test constructs an Insight with three fields;
    the new one must be optional, and its 0 must read as "not recorded" (store
    falls back to the cluster size) rather than "saw nothing"."""
    assert syn.Insight(content="c", memory_type="procedural", source_ids=["a"]).sample_size == 0


def test_default_cap_is_five_and_is_pinned_to_the_config_default():
    """synthesize() reads the cap as `getattr(settings, ..., 5)`; that literal
    is the duck-typed-stub fallback production never reaches, and this pins it
    to the real default so the two cannot drift silently — same shape as the
    DREAM_SYNTH_TIMEOUT_SECONDS drift guard above.

    5 is chosen for headroom under measured variance, not for a winning time:
    across 3 runs each, caps 4/5/6 all fit the 45s budget (worst run 35.8s at
    cap 5), while cap 4 alone spanned 12.0-42.9s on identical input. 5 clears
    the prompt's 2-episodes-per-insight floor and produced multiple insights in
    the quality probe. See config.py for the full table and why single probes
    are not sufficient to tune this.
    """
    from app.config import Settings

    assert Settings.model_fields["DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS"].default == 5


@pytest.mark.asyncio
async def test_settings_without_the_cap_field_still_caps_at_the_default():
    """A settings object predating the field (or a stub) must not silently
    revert to the unbounded prompt that made 19 of 20 clusters unrunnable."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": _raw()}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await syn.synthesize(_spread(23), settings=_S(), max_chars=800, client=client)

    user = next(m["content"] for m in seen["body"]["messages"] if m["role"] == "user")
    assert len(user.splitlines()) == 5


def test_compose_default_does_not_override_the_code_default():
    """A stale `${VAR:-N}` in compose silently wins over the code default:
    compose supplies the fallback when the operator's `.env` does not set the
    variable, so the container receives compose's number and Settings never
    sees its own. All three cortex services that run or schedule the dream tick
    must therefore carry the same 5 this file pins — a compose block left at,
    say, 23 would reinstate the unbounded prompt on the deployment while every
    test here passed. (`.env.example` carries no DREAM_* vars at all, so there
    is deliberately no sibling guard for it.)"""
    import re
    from pathlib import Path

    from app.config import Settings

    repo = Path(__file__).resolve().parents[2]
    text = (repo / "docker-compose.yml").read_text(encoding="utf-8")
    found = re.findall(
        r"DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS:\s*"
        r"\$\{DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS:-([0-9]+)\}",
        text,
    )
    assert len(found) == 3, (
        "docker-compose.yml must set DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS on "
        f"all three dream-carrying services; found {len(found)}"
    )
    assert {int(v) for v in found} == {
        Settings.model_fields["DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS"].default
    }
