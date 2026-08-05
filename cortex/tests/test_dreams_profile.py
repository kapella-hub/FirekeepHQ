import contextlib
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import llm
from app.dreams import profile, store
from app.dreams.synthesize import _MAX_COMPLETION_TOKENS


class _S:
    """Settings stub for the llm.chat path.

    `LLM_NATIVE_CHAT` defaults to "always" so `is_native` returns True WITHOUT
    probing — the probe builds its OWN httpx.AsyncClient, not the
    MockTransport-backed one these tests inject, so a test left on "auto" would
    attempt a real DNS lookup for `http://x/api/version` and then silently take
    the /v1 branch for whatever reason it failed. Tests that want /v1 ask for it
    with "never"; the tests that genuinely exercise "auto" patch the probe.
    (Same stub, same reasoning, as tests/test_dreams_synthesize.py.)
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


@contextlib.contextmanager
def _probe(*responses):
    """Patch the native-endpoint probe's own client. Yields the probed URLs."""
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


_MEMORIES = [{"text": "asked for measured evidence", "timestamp": None}]
_PROFILE_TEXT = "Works on cortex; asks for measured evidence rather than assertions."


class FakeVector:
    def __init__(self):
        self.points = {}

    async def upsert_point(self, point_id, text, payload):
        self.points[point_id] = {"text": text, "payload": payload}
        return point_id


def test_profile_payload_keys_on_member_id_not_agent_id():
    p = profile.build_profile_payload("who they are", member_id="mem1",
                                      workspace_id="ws1", run_id="r")
    assert p["member_id"] == "mem1"
    assert p["source"] == "dream_profile"
    assert p["agent_id"] == "dream"


def test_profile_is_excluded_from_future_candidate_selection():
    from datetime import datetime, timedelta, timezone
    from app.dreams.select import is_candidate

    p = profile.build_profile_payload("x", member_id="m", workspace_id="w", run_id="r")
    p["timestamp"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    assert not is_candidate(p, now=datetime.now(timezone.utc), min_age_days=2,
                            owm_floor=0.35, owm_prior_n=5)


def test_parse_profile_rejects_empty_and_overlong():
    assert profile.parse_profile("   ", max_chars=800) is None
    assert profile.parse_profile("x" * 900, max_chars=800) is None
    assert profile.parse_profile("  real  ", max_chars=800) == "real"


def test_parse_profile_rejects_the_refusal_a_live_run_actually_stored():
    """The exact text a live run stored at a member's deterministic point id
    and then served through the briefing. parse_profile only checked
    empty/overlong, so a refusal was a valid profile — and because a profile is
    replaced IN PLACE, it overwrote the real one."""
    refusal = ("No human is mentioned in the memories. The text describes "
               "system behavior, configuration changes, and automated "
               "processes rather than any individual person.")
    assert profile.parse_profile(refusal, max_chars=800) is None


@pytest.mark.parametrize("refusal", [
    "There is no person described in these memories.",
    "I'm sorry, but I cannot build a profile from the provided memories.",
    "The memories do not mention a specific human being.",
    "Insufficient information to describe how this person works.",
])
def test_parse_profile_rejects_other_refusal_shapes(refusal):
    assert profile.parse_profile(refusal, max_chars=800) is None


def test_parse_profile_accepts_a_real_profile_that_negates_mid_body():
    """The guard is windowed to the OPENING for exactly this case: a genuine
    profile may say "there is no evidence ..." about some trait once it is past
    the first sentence. Rejecting that would be worse than the defect — this is
    the false-positive direction the window exists to bound."""
    text = (
        "Works primarily on the cortex service and reviews changes by reading "
        "the diff before the tests. Consistently asks for measured evidence "
        "rather than assertions, and corrects claims that outrun their data. "
        "There is no evidence they work on the dashboard or the client kit."
    )
    assert profile.parse_profile(text, max_chars=800) == text
    assert text.lower().index("there is no ") >= profile._REFUSAL_WINDOW_CHARS, (
        "the negation must fall OUTSIDE the window or this test proves nothing"
    )


@pytest.mark.asyncio
async def test_repeated_profile_writes_leave_exactly_one_point():
    v = FakeVector()
    for text in ("v1", "v2", "v3"):
        await profile.write_profile(v, text, member_id="mem1", workspace_id="ws1", run_id="r")
    assert len(v.points) == 1
    only = next(iter(v.points.values()))
    assert only["text"] == "v3"


@pytest.mark.asyncio
async def test_two_members_get_two_points():
    v = FakeVector()
    await profile.write_profile(v, "a", member_id="m1", workspace_id="ws1", run_id="r")
    await profile.write_profile(v, "b", member_id="m2", workspace_id="ws1", run_id="r")
    assert len(v.points) == 2
    assert store.profile_point_id("m1", "ws1") in v.points


def test_build_profile_payload_defaults_match_pre_fix_behaviour():
    """Backward compatibility: a caller that doesn't pass namespace/project
    (as every pre-fix-round caller did) still gets the old hardcoded values."""
    p = profile.build_profile_payload("x", member_id="m", workspace_id="w", run_id="r")
    assert p["namespace"] == "default"
    assert p["project"] is None


def test_build_profile_payload_derives_namespace_and_project():
    """Fix-round review I2: namespace/project must be DERIVED from the
    member's memories, not hardcoded — project is a hard `must` filter in
    VectorClient.search, so a profile stamped project=None when its source
    memories actually carried a project was invisible to project-scoped
    recall."""
    p = profile.build_profile_payload(
        "x", member_id="m", workspace_id="w", run_id="r",
        namespace="acme", project="firekeep",
    )
    assert p["namespace"] == "acme"
    assert p["project"] == "firekeep"


@pytest.mark.asyncio
async def test_write_profile_passes_through_namespace_and_project():
    v = FakeVector()
    point_id = await profile.write_profile(
        v, "text", member_id="m1", workspace_id="ws1", run_id="r",
        namespace="acme", project="firekeep",
    )
    payload = v.points[point_id]["payload"]
    assert payload["namespace"] == "acme"
    assert payload["project"] == "firekeep"


# ---------------------------------------------------------------------------
# synthesize_profile — the llm.chat conversion
#
# This function had NO test coverage at all before the conversion: everything
# above tests the pure helpers and the write path, and test_dreams_task.py
# monkeypatches the whole call away. So these are not adapted assertions, they
# are the first ones — which is also why the pre-conversion defect (posting to
# /v1, where ollama silently ignores `think:false`, on a 45s budget the
# reasoning block cannot fit inside) survived review.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_profile_synthesis_goes_to_ollamas_native_endpoint():
    """The conversion, asserted where it pays: the URL.

    `think:false` was already being SENT before this change — to
    /v1/chat/completions, which throws it away and generates the full reasoning
    block anyway. The endpoint is what distinguishes working from inoperable,
    so the flag alone is not the assertion."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": _PROFILE_TEXT}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(), max_chars=800, client=client)

    assert seen["url"] == "http://x/api/chat"
    body = seen["body"]
    assert body["think"] is False
    # MEASURED (llm.py probe C): omit `stream` and ollama answers NDJSON,
    # resp.json() raises, and the call fails for a reason unrelated to dreams.
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0.2, "num_predict": _MAX_COMPLETION_TOKENS}
    assert out == _PROFILE_TEXT


@pytest.mark.asyncio
async def test_profile_synthesis_is_not_json_mode_on_either_endpoint():
    """THE deviation from synthesize(), and the one that would break profiles if
    it were "tidied" into consistency. The old code built the shared JSON body
    and then set `response_format = {"type": "text"}` — it deliberately turned
    the grammar OFF, because a profile is plain prose and the system prompt asks
    for exactly that. `json_mode=False` reproduces it: no `format` natively, no
    `response_format` on /v1 (OpenAI's default IS text)."""
    bodies = {}

    def handler(request: httpx.Request) -> httpx.Response:
        bodies[request.url.path] = json.loads(request.content)
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {"content": _PROFILE_TEXT}})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": _PROFILE_TEXT}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(), max_chars=800, client=client)
        await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(LLM_NATIVE_CHAT="never"), max_chars=800,
            client=client)

    assert "format" not in bodies["/api/chat"]
    assert "response_format" not in bodies["/v1/chat/completions"]


@pytest.mark.asyncio
async def test_profile_synthesis_falls_back_to_a_standard_openai_body():
    """A backend that does not confirm as ollama must still get a well-formed,
    STANDARD body — no `think`, no `chat_template_kwargs`, which real OpenAI
    400s on. The old hand-built body sent both unconditionally."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": _PROFILE_TEXT}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(LLM_NATIVE_CHAT="never"), max_chars=800,
            client=client)

    assert seen["url"] == "http://x/v1/chat/completions"
    body = seen["body"]
    assert body["max_tokens"] == _MAX_COMPLETION_TOKENS
    for absent in ("think", "chat_template_kwargs", "format", "options", "stream"):
        assert absent not in body
    assert out == _PROFILE_TEXT


@pytest.mark.asyncio
async def test_profile_synthesis_is_bounded_by_the_configured_dream_budget():
    """DREAM_SYNTH_TIMEOUT_SECONDS is read off `settings` now that the caller
    passes no timeout, and it bounds both endpoints — deliberately no native
    sibling, for synthesize()'s reason: a lower native budget is what strands a
    non-thinking-model ollama deploy."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"message": {"content": _PROFILE_TEXT}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(DREAM_SYNTH_TIMEOUT_SECONDS=45.0),
            max_chars=800, client=client)

    assert seen["timeout"]["read"] == 45.0


@pytest.mark.asyncio
async def test_empty_content_is_never_rescued_by_the_reasoning_field():
    """The deleted fallback, pinned as removed rather than merely untested.

    The old code read `msg.get("content") or msg.get("reasoning")`. Here that is
    worse than useless, not merely useless: this call is NOT json-mode, so the
    reasoning IS prose — the fallback would store a model's raw chain-of-thought
    at the member's deterministic point id and serve it through every briefing,
    replacing the real profile. Same defect class as the refusal incident,
    through a different door. The payload below is the one that could have
    exercised it: empty content, perfectly profile-shaped reasoning."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "", "reasoning": _PROFILE_TEXT}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(LLM_NATIVE_CHAT="never"), max_chars=800,
            client=client)

    assert out is None


@pytest.mark.asyncio
async def test_a_refusal_over_the_wire_is_still_rejected():
    """End-to-end proof that the conversion did not weaken `parse_profile`. The
    text is the one a live run actually stored and then served through the
    briefing. A profile is replaced IN PLACE, so returning None here is what
    leaves the previous, real profile intact."""
    refusal = ("No human is mentioned in the memories. The text describes "
               "system behavior, configuration changes, and automated processes.")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": refusal}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(), max_chars=800, client=client)

    assert out is None


@pytest.mark.asyncio
async def test_a_backend_error_returns_none_and_is_never_retried():
    """The retry contract — and it is NOT synthesize()'s. There is exactly one
    call and no retry on any failure, including a malformed or refusing
    response. That was true before the conversion and is unchanged; the caller
    absorbs it by marking the group done and leaving the previous profile in
    place, which a cluster has no equivalent of.

    Also re-verifies never-raises against a type new to this guard: llm.chat
    raises httpx.HTTPStatusError where this function used to call
    raise_for_status() itself. 500 is not in llm._DEMOTE_STATUS_CODES (those are
    pre-generation 4xx), so llm.chat performs no fallback either — exactly one
    request goes out."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(), max_chars=800, client=client)

    assert out is None
    assert len(calls) == 1, "a backend failure is not the model's fault — no retry"


@pytest.mark.asyncio
async def test_malformed_output_is_not_retried_either():
    """The other half of the same rule, stated separately because this is where
    synthesize() DIFFERS: it calls a second time on malformed JSON. Overlong
    output is this call's nearest equivalent to malformed, and it gets one call
    and None."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"message": {"content": "x" * 5000}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(), max_chars=800, client=client)

    assert out is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unusable_settings_return_none_rather_than_raising():
    """llm.chat reads settings.LLM_MODEL when it builds the body, so a settings
    object missing it raises AttributeError from INSIDE the call — a class that
    could not previously reach this function. The guard is Exception-shaped, not
    type-enumerated, so it lands in `return None` like everything else."""
    class _Broken:
        LLM_BASE_URL = "http://x/v1"
        LLM_NATIVE_CHAT = "never"

    assert await profile.synthesize_profile(
        "mem1", _MEMORIES, settings=_Broken(), max_chars=800) is None


@pytest.mark.asyncio
async def test_no_memories_short_circuits_before_any_request():
    """Pre-existing behaviour, now worth asserting because the call it guards
    is no longer local: an empty group must cost zero LLM round trips."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"message": {"content": _PROFILE_TEXT}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await profile.synthesize_profile(
            "mem1", [], settings=_S(), max_chars=800, client=client)

    assert out is None
    assert calls == []


@pytest.mark.asyncio
async def test_auto_mode_probes_then_demotes_to_v1_on_a_pre_generation_4xx():
    """The production default (`auto`) plus the escape hatch for an ollama old
    enough to reject `think`: probe confirms ollama, native is refused with a
    pre-generation 400, llm.chat demotes the cached verdict and retries once on
    /v1. A profile must come back, not None — synthesize_profile does not retry
    on its own, so if that fallback did not fire the member would silently keep
    a stale profile forever."""
    urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.path == "/api/chat":
            return httpx.Response(400, text="unknown field think")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": _PROFILE_TEXT}}]})

    # Client built OUTSIDE `_probe`: that patch replaces httpx.AsyncClient
    # itself, so a client constructed inside it is an AsyncMock, not a transport.
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with _probe(httpx.Response(200, json={"version": "0.11.0"})) as probed:
            out = await profile.synthesize_profile(
                "mem1", _MEMORIES, settings=_S(LLM_NATIVE_CHAT="auto"),
                max_chars=800, client=client)

    assert probed == ["http://x/api/version"]
    assert urls == ["http://x/api/chat", "http://x/v1/chat/completions"]
    assert out == _PROFILE_TEXT
    assert llm._probe_cache["http://x"][0] is False
