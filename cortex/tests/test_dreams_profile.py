import contextlib
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import llm
from app.dreams import profile, store
from app.dreams.profile import _MAX_PROFILE_TOKENS


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
        self.DREAM_SYNTH_TIMEOUT_SECONDS = kw.pop("DREAM_SYNTH_TIMEOUT_SECONDS", 90.0)
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
# What the schema-honouring rung actually puts on the wire.
_ENVELOPE = json.dumps({"profile": _PROFILE_TEXT})


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
        return httpx.Response(200, json={"message": {"content": _ENVELOPE}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(), max_chars=800, client=client)

    assert seen["url"] == "http://x/api/chat"
    body = seen["body"]
    assert body["think"] is False
    # MEASURED (llm.py probe C): omit `stream` and ollama answers NDJSON,
    # resp.json() raises, and the call fails for a reason unrelated to dreams.
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0.2, "num_predict": _MAX_PROFILE_TOKENS}
    assert out == _PROFILE_TEXT


@pytest.mark.asyncio
async def test_profile_synthesis_is_schema_constrained_on_both_endpoints():
    """The fix, asserted on the wire.

    This test previously asserted the OPPOSITE — that no grammar was sent, on
    the reasoning that a profile is prose so nothing should constrain it. What
    that actually bought was free-form generation with nothing to terminate it
    and no constraint on shape: measured on the VPS, a prompt-size sweep failed
    identically at the client timeout at every size (a size-driven effect shows
    a gradient; this showed none), and at smaller token caps the model returned
    the SYSTEM PROMPT ECHOED BACK. `format:"json"` alone is not enough either —
    phase 3 measured 0/3 adherence under it — so the assertion is the SCHEMA,
    in each endpoint's own envelope."""
    bodies = {}

    def handler(request: httpx.Request) -> httpx.Response:
        bodies[request.url.path] = json.loads(request.content)
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {"content": _ENVELOPE}})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": _ENVELOPE}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(), max_chars=800, client=client)
        await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(LLM_NATIVE_CHAT="never"), max_chars=800,
            client=client)

    # Ollama's structured-outputs surface IS `format` — the same field that
    # otherwise carries the string "json".
    assert bodies["/api/chat"]["format"] == profile._PROFILE_SCHEMA
    assert bodies["/v1/chat/completions"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "dream_profile",
            "strict": True,
            "schema": profile._PROFILE_SCHEMA,
        },
    }


def test_the_schema_conforms_to_openai_strict_mode():
    """`build_openai_body` sends `strict: True`, which requires `properties` and
    `required` to name exactly the same keys and `additionalProperties: false`.
    A schema that violates it is rejected pre-generation by OpenAI, which
    `llm.chat` answers by silently dropping the schema — so a non-conformant
    schema does not fail loudly, it degrades every OpenAI-backed deploy back to
    the unconstrained call this change exists to remove."""
    s = profile._PROFILE_SCHEMA
    assert s["type"] == "object"
    assert set(s["required"]) == set(s["properties"])
    assert s["additionalProperties"] is False
    assert s["properties"][profile._PROFILE_KEY] == {"type": "string"}
    # No length bounds: parse_profile owns those, AFTER extraction, where the
    # number means what DREAM_MAX_INSIGHT_CHARS says it means. A grammar cannot
    # decline, only stop — a maxLength truncates mid-word instead of rejecting.
    assert "maxLength" not in s["properties"][profile._PROFILE_KEY]
    assert "minLength" not in s["properties"][profile._PROFILE_KEY]


def test_the_completion_budget_absorbs_v1_reasoning_not_just_the_answer():
    """An ANSWER-SIZED cap is the intuitive derivation and it is wrong.

    It was tried: 800 chars of prose plus envelope is ~200-330 tokens, so 512
    looked like generous headroom. That reasoning assumes every generated token
    lands in the answer, which is false on ollama's `/v1` endpoint — `think:false`
    is ignored there, the reasoning block is generated FIRST, and the budget is
    spent before the answer starts. This repo measured it at max_tokens=700 on
    `/v1`: HTTP 200, finish_reason='length', completion_tokens=700, content length
    ZERO, ~3200 chars of reasoning; the same call at 4000 returned correct output.

    512 is below 700, so an answer-sized cap returns None on EVERY call on a
    `/v1`-routed thinking-model deploy — worse than before the schema landed, and
    that path is live (any non-ollama backend, any ollama demoted by a 4xx).

    A large cap is free because THE SCHEMA is the terminator: a JSON string ends
    at its closing quote, so the native path stops on its own and never
    approaches the cap. The failure this replaced was an UNCONSTRAINED call,
    where nothing stopped at 4000 because nothing was stopping it at all.

    Asymmetry decides the floor: too large costs one loud tick bounded by
    DREAM_SYNTH_TIMEOUT_SECONDS; too small is silent, total and permanent."""
    assert _MAX_PROFILE_TOKENS >= 4000, (
        "must exceed the 700 that measured empty-content on /v1; 4000 is the "
        "empirically verified working value"
    )


def test_the_budget_still_clears_the_largest_acceptable_profile():
    """The other end: whatever the cap is, it must not be able to truncate a
    profile the module would ACCEPT. parse_profile discards anything longer than
    max_chars, so the largest acceptable answer is DREAM_MAX_INSIGHT_CHARS of
    prose plus the JSON envelope. At a pessimistic 2.5 chars/token that is
    ~330 tokens for the shipped 800.

    This is the drift guard the review asked for: DREAM_MAX_INSIGHT_CHARS is
    env-tunable and _MAX_PROFILE_TOKENS is a frozen literal, so a deployment
    that raises the char budget far enough would silently start truncating every
    profile into an unparseable envelope and storing None. Reads the real
    Settings default rather than restating it."""
    from app.config import Settings
    max_chars = Settings.model_fields["DREAM_MAX_INSIGHT_CHARS"].default
    worst_case_tokens = max_chars / 2.5 + 10   # pessimistic tokenizer + envelope
    assert _MAX_PROFILE_TOKENS > worst_case_tokens, (
        f"DREAM_MAX_INSIGHT_CHARS={max_chars} needs ~{worst_case_tokens:.0f} "
        f"tokens worst-case but the cap is {_MAX_PROFILE_TOKENS}"
    )


# ---------------------------------------------------------------------------
# synthesize_profile over the wire — the non-native body, the budget, the two
# rungs of the schema ladder, and the no-retry contract.
#
# The schema landed after these were first written, and it changed what several
# of them see on the wire: `llm.chat` now runs a THREE-rung ladder natively
# (native+schema -> native -> /v1) and a TWO-rung one on /v1 (schema -> no
# schema), so "how many requests went out" is no longer the same number for the
# same failure. Each test below states what it now expects and why, rather than
# being left asserting the pre-schema shape.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_profile_synthesis_falls_back_to_a_standard_openai_body():
    """A backend that does not confirm as ollama must still get a well-formed,
    STANDARD body — no `think`, no `chat_template_kwargs`, which real OpenAI
    400s on. The old hand-built body sent both unconditionally.

    `response_format` is deliberately NOT in the absent list: it is a standard
    OpenAI field and it is now the schema's carrier on this endpoint (asserted
    in full by test_profile_synthesis_is_schema_constrained_on_both_endpoints).
    The vendor-only fields are the ones that must never appear here."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": _ENVELOPE}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(LLM_NATIVE_CHAT="never"), max_chars=800,
            client=client)

    assert seen["url"] == "http://x/v1/chat/completions"
    body = seen["body"]
    assert body["max_tokens"] == _MAX_PROFILE_TOKENS
    for absent in ("think", "chat_template_kwargs", "format", "options", "stream"):
        assert absent not in body
    assert out == _PROFILE_TEXT


@pytest.mark.asyncio
async def test_profile_synthesis_is_bounded_by_the_configured_dream_budget():
    """DREAM_SYNTH_TIMEOUT_SECONDS is read off `settings` now that the caller
    passes no timeout, and it bounds both endpoints — deliberately no native
    sibling, for synthesize()'s reason: a lower native budget is what strands a
    non-thinking-model ollama deploy (the probe confirms ollama, not a thinking
    model)."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"message": {"content": _ENVELOPE}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(DREAM_SYNTH_TIMEOUT_SECONDS=90.0),
            max_chars=800, client=client)

    assert seen["timeout"]["read"] == 90.0


@pytest.mark.asyncio
async def test_empty_content_is_never_rescued_by_the_reasoning_field():
    """The deleted fallback, pinned as removed rather than merely untested.

    The old code read `msg.get("content") or msg.get("reasoning")`. THE SCHEMA
    STRENGTHENS THE CASE AGAINST IT RATHER THAN WEAKENING IT. Elsewhere
    (classifier.py, decision/synthesize.py) the argument is that under a grammar
    an empty content means the grammar blocked the output, so `reasoning` is
    prose and `json.loads` rejects it anyway — useless, not harmful. Here it is
    actively harmful, because the consumer of this value ACCEPTS prose:
    `_extract_profile_text`'s rung-2 branch returns any non-JSON text as-is. So
    the fallback would store a model's raw chain-of-thought at the member's
    deterministic point id and serve it through every briefing, replacing the
    real profile — the same defect class as the refusal incident, through a
    different door. The payload below is the one that could have exercised it:
    empty content, perfectly profile-shaped reasoning."""
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
    """End-to-end proof that neither the llm.chat conversion nor the schema
    weakened `parse_profile`. The text is the one a live run actually stored and
    then served through the briefing. A profile is replaced IN PLACE, so
    returning None here is what leaves the previous, real profile intact.

    BARE PROSE on purpose: that is the schema-DROPPED rung (rung 2), the one a
    non-ollama backend lives on permanently, and the rung where the extractor
    hands text straight through with no envelope to inspect. The envelope shape
    is the sibling test below."""
    refusal = ("No human is mentioned in the memories. The text describes "
               "system behavior, configuration changes, and automated processes.")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": refusal}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(), max_chars=800, client=client)

    assert out is None


@pytest.mark.asyncio
async def test_a_refusal_inside_the_json_envelope_is_still_rejected():
    """The schema-HONOURING rung of the same guard, and it is not implied by the
    test above.

    A grammar constrains shape, not content: a model that declines the task can
    decline it perfectly validly inside `{"profile": "..."}`, and that reply is
    indistinguishable at the transport layer from a good one. The refusal check
    therefore has to survive extraction — which is exactly why `parse_profile`
    extracts FIRST and guards second. Without that ordering the schema would
    have quietly reintroduced the incident it was added to prevent, since a
    refusal wrapped in an envelope is still stored in place over a real
    profile."""
    refusal = ("There is no person described in these memories; they describe "
               "system behavior only.")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"message": {"content": json.dumps({"profile": refusal})}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(), max_chars=800, client=client)

    assert out is None


@pytest.mark.asyncio
async def test_a_backend_error_returns_none_and_is_never_retried():
    """The retry contract — and it is NOT synthesize()'s. There is exactly one
    call and no retry on any failure, including a malformed or refusing
    response. The caller absorbs it by marking the group done and leaving the
    previous profile in place, which a cluster has no equivalent of.

    Also re-verifies never-raises against a type new to this guard: llm.chat
    raises httpx.HTTPStatusError where this function used to call
    raise_for_status() itself.

    THE SCHEMA DOES NOT LOOSEN THE ONE-CALL COUNT, and that is worth pinning
    rather than assuming: `llm.chat` now plans a three-rung ladder for a
    schema-carrying native call, but every rung below the first is reachable
    only through `_DEMOTE_STATUS_CODES`, which is pre-generation 4xx ONLY. A 500
    may arrive mid-generation, so it propagates on the first attempt and no rung
    after it is ever built."""
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
@pytest.mark.parametrize("content", [
    '{"profile": "Works on cortex; asks for measured evi',
    "x" * 5000,
], ids=["truncated-envelope", "over-budget-prose"])
async def test_malformed_output_is_not_retried_either(content):
    """The other half of the same rule, stated separately because this is where
    synthesize() DIFFERS: it calls a second time on malformed JSON. This one
    does not, and the reason is about the CALLER rather than the response
    format — a failed profile leaves the PREVIOUS profile in place, so not
    writing is a real and safe outcome, while a failed cluster synthesis leaves
    nothing at all.

    Two shapes, because the schema created a second one. The truncated envelope
    is what `_MAX_PROFILE_TOKENS` (or any backend cutoff) actually produces and
    is malformed in the literal sense; the over-budget prose is the pre-schema
    equivalent and still arrives on the schema-dropped rung. Both must cost
    exactly one request."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"message": {"content": content}})

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
    type-enumerated, so it lands in `return None` like everything else.

    Note the body is built before any client is touched, so this also proves the
    failure costs zero round trips despite no transport being injected."""
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
        return httpx.Response(200, json={"message": {"content": _ENVELOPE}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await profile.synthesize_profile(
            "mem1", [], settings=_S(), max_chars=800, client=client)

    assert out is None
    assert calls == []


@pytest.mark.asyncio
async def test_a_backend_that_rejects_the_schema_still_produces_a_profile():
    """The schema is a REQUEST, not a prerequisite — asserted on the rung that
    keeps every non-ollama deploy working.

    There is no capability endpoint to feature-detect structured outputs
    against, so a `/v1` backend that has not implemented
    `response_format.type = "json_schema"` refuses the request pre-generation.
    422 is the shape that matters here specifically: vLLM's OpenAI-compatible
    server is FastAPI, whose request-validation rejection is a 422 rather than a
    400. If that fallback did not fire, adding the schema would have traded a
    broken profile pass on ollama for a broken one everywhere else — and
    silently, since synthesize_profile answers every failure with None and the
    member simply keeps a stale profile forever.

    The retry stays JSON-MODE (`llm.chat` coerces `json_mode` true whenever a
    schema was requested), which is why the second body still carries
    `json_object`: falling back to NO output constraint at all would turn a
    backend's polite refusal into free-form prose."""
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(422, text="unknown response_format type")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": _PROFILE_TEXT}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await profile.synthesize_profile(
            "mem1", _MEMORIES, settings=_S(LLM_NATIVE_CHAT="never"), max_chars=800,
            client=client)

    assert len(bodies) == 2, "one rejected schema attempt, one schema-dropped retry"
    assert bodies[1]["response_format"] == {"type": "json_object"}
    # Rung 2: the reply is unconstrained prose and must still become a profile.
    assert out == _PROFILE_TEXT


@pytest.mark.asyncio
async def test_auto_mode_probes_then_demotes_to_v1_on_a_pre_generation_4xx():
    """The production default (`auto`) plus the escape hatch for an ollama old
    enough to reject `think`: probe confirms ollama, native is refused with a
    pre-generation 400, llm.chat demotes the cached verdict and retries on /v1.
    A profile must come back, not None — synthesize_profile does not retry on
    its own, so if that fallback did not fire the member would silently keep a
    stale profile forever.

    THE REQUEST COUNT CHANGED WITH THE SCHEMA and the new number is the point.
    The ladder drops exactly ONE capability per rung, so a native backend that
    refuses everything is tried natively TWICE — once with the schema, once
    without — before the endpoint itself is given up. That ordering is
    deliberate: a native call that failed merely because of the schema must not
    cost the process its native verdict, since the endpoint was never the
    problem. Only the native->/v1 transition demotes, which is why the cache
    still ends up False here."""
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
    assert urls == [
        "http://x/api/chat",             # rung 1: native + schema
        "http://x/api/chat",             # rung 2: native, schema dropped
        "http://x/v1/chat/completions",  # rung 3: endpoint given up
    ]
    assert out == _PROFILE_TEXT
    assert llm._probe_cache["http://x"][0] is False


def test_the_refusal_guard_runs_on_the_extracted_prose_not_the_envelope():
    """WHERE the guard runs, proved by a case where the two answers differ.

    The test above does not settle this on its own: `{"profile": "` is 13
    characters, so a refusal in a single-key envelope still falls inside
    `_REFUSAL_WINDOW_CHARS` and would be caught either way. The window is what
    makes the placement load-bearing — any envelope whose prose starts past 200
    characters defeats a guard applied to the raw response. That shape is
    reachable on the schema-dropped rung, where the model is constrained in
    syntax only and may add keys of its own before the one we asked for."""
    refusal = "No human is mentioned in the memories; they describe system behavior."
    envelope = json.dumps({"note": "x" * 400, "profile": refusal})

    assert not profile._looks_like_refusal(envelope), (
        "the raw envelope must NOT match, or this proves nothing about placement"
    )
    assert profile._looks_like_refusal(profile._extract_profile_text(envelope))
    assert profile.parse_profile(envelope, max_chars=800) is None


# ---------------------------------------------------------------------------
# _extract_profile_text — the two rungs, and the shapes that must NOT pass
# ---------------------------------------------------------------------------

def test_extraction_unwraps_the_schema_envelope():
    assert profile._extract_profile_text(_ENVELOPE) == _PROFILE_TEXT


def test_extraction_accepts_bare_prose_from_the_schema_dropped_rung():
    """Rung 2. Prose is never valid JSON, so this branch cannot swallow a JSON
    reply — which is what lets it be a plain fallthrough rather than a guess."""
    assert profile._extract_profile_text(_PROFILE_TEXT) == _PROFILE_TEXT


def test_extraction_unquotes_a_bare_json_string():
    """A backend that returns the prose as a JSON scalar has still returned the
    prose, with one layer of encoding on it. Refusing it would punish tidiness."""
    assert profile._extract_profile_text(json.dumps(_PROFILE_TEXT)) == _PROFILE_TEXT


def test_extraction_refuses_a_truncated_envelope_instead_of_calling_it_prose():
    """The branch that earns `_MAX_PROFILE_TOKENS` the right to be a cap at all.

    A completion cut off at the token budget is an UNPARSEABLE JSON envelope,
    and the prose fallthrough would hand it back verbatim — so the member's
    deterministic point id would hold the literal string `{"profile": "Works
    on...`, and every briefing would serve it. Refusing costs one skipped
    refresh with the previous profile intact."""
    truncated = '{"profile": "Works on cortex; asks for measured evi'
    assert profile._extract_profile_text(truncated) is None
    assert profile.parse_profile(truncated, max_chars=800) is None


@pytest.mark.parametrize("payload", [
    {"summary": "Works on cortex."},                       # right idea, wrong key
    {"context": "...", "questions": []},                   # phase 3's mirrored input
    {"profile": {"nested": "object"}},                      # right key, wrong type
    {"profile": ["a", "b"]},
    ["Works on cortex."],                                   # an array, not an object
])
def test_extraction_refuses_valid_json_of_the_wrong_shape(payload):
    """A backend that ignored or was denied the schema can return well-formed
    JSON that is not a profile. Stringifying it would store `{"summary": ...}`
    as somebody's profile; there is no honest prose in any of these."""
    assert profile._extract_profile_text(json.dumps(payload)) is None


def test_extraction_of_an_empty_or_non_string_response_is_none():
    assert profile._extract_profile_text("") is None
    assert profile._extract_profile_text("   ") is None
    assert profile._extract_profile_text(None) is None


def test_the_char_budget_is_measured_on_the_prose_not_the_envelope():
    """`max_chars` is DREAM_MAX_INSIGHT_CHARS — a bound on the profile. Checking
    the envelope would spend ~14 of the human's characters on punctuation, and
    would reject a profile that is exactly at budget."""
    at_budget = "x" * 800
    assert profile.parse_profile(json.dumps({"profile": at_budget}),
                                 max_chars=800) == at_budget
    assert profile.parse_profile(json.dumps({"profile": "x" * 801}),
                                 max_chars=800) is None


def test_the_system_prompt_no_longer_contradicts_the_grammar():
    """It used to end "No markdown fencing, no JSON, no preamble" — an
    instruction not to emit the only thing the schema permits. A model handed
    two contradictory constraints is exactly the setup that produced the
    prompt-echo this change fixes."""
    prompt = profile._system_prompt(800)
    assert "no JSON" not in prompt
    assert profile._PROFILE_KEY in prompt
