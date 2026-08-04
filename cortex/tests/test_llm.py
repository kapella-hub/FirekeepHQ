"""Tests for app/llm.py — chat endpoint selection, body shape, normalisation.

The body-shape assertions are not style preferences. Each pins a fact measured
on the wire against the live VPS backend (ollama 0.32.4, qwen3:4b) on
2026-08-04 and recorded in app/llm.py's module docstring:

  * omitting `stream` makes ollama stream NDJSON, so `resp.json()` raises and
    the ingest goes terminal `failed` — the single most likely way to break
    this change;
  * omitting `think` costs 111.20s instead of 4.00s on the same document;
  * with `think:false` the response's `message` has NO `thinking` key at all,
    so parsing must use `.get()`.
"""
from __future__ import annotations

import contextlib
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import llm


class _S:
    """Minimal settings stub."""

    def __init__(self, **kw):
        self.LLM_BASE_URL = kw.pop("LLM_BASE_URL", "http://ollama:11434/v1")
        self.LLM_MODEL = kw.pop("LLM_MODEL", "qwen3:4b")
        self.LLM_API_KEY = kw.pop("LLM_API_KEY", "")
        self.LLM_NATIVE_CHAT = kw.pop("LLM_NATIVE_CHAT", "auto")
        self.LLM_NATIVE_PROBE_TTL_SECONDS = kw.pop("LLM_NATIVE_PROBE_TTL_SECONDS", 600.0)
        self.LLM_NATIVE_BASE_URL = kw.pop("LLM_NATIVE_BASE_URL", "")
        for k, v in kw.items():
            setattr(self, k, v)


@contextlib.contextmanager
def _probe(*responses):
    """Patch the probe's client. Yields the list of probed URLs.

    Each element of `responses` is an httpx.Response or an Exception; the last
    one repeats if more probes happen than were supplied.
    """
    urls: list[str] = []

    def _factory(*_a, **_k):
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        async def _get(url, **_kw):
            urls.append(url)
            item = responses[min(len(urls) - 1, len(responses) - 1)]
            if isinstance(item, Exception):
                raise item
            return item

        client.get = _get
        return client

    with patch("app.llm.httpx.AsyncClient", side_effect=_factory):
        yield urls


def _ok_probe() -> httpx.Response:
    return httpx.Response(200, json={"version": "0.32.4"})


# ---------------------------------------------------------------------------
# native_root
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "base_url,expected",
    [
        ("http://ollama:11434/v1", "http://ollama:11434"),
        ("http://ollama:11434/v1/", "http://ollama:11434"),
        ("https://gw.example.com/openai/v1", "https://gw.example.com/openai"),
        ("http://localhost:11434/v1", "http://localhost:11434"),
        # No /v1 suffix: nothing defensible to derive, so never native.
        ("http://ollama:11434", None),
        ("https://api.openai.com/v2", None),
        ("", None),
        ("/v1", None),
        (None, None),
        (1234, None),
    ],
)
def test_native_root_derivation(base_url, expected):
    assert llm.native_root(base_url) == expected


def test_resolve_root_prefers_explicit_override():
    s = _S(LLM_BASE_URL="http://no-suffix:9999", LLM_NATIVE_BASE_URL="http://ollama:11434/")
    assert llm._resolve_root(s) == "http://ollama:11434"


def test_resolve_root_type_guards_non_string_settings():
    """A settings object handing back a non-str (a MagicMock in a test, a
    mis-typed env) must fall through to 'no native root' rather than be
    string-formatted into a nonsense probe URL."""
    s = _S(LLM_BASE_URL=object(), LLM_NATIVE_BASE_URL=object())
    assert llm._resolve_root(s) is None


# ---------------------------------------------------------------------------
# Body builders
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("json_mode", [True, False])
@pytest.mark.parametrize("temperature", [None, 0.1])
@pytest.mark.parametrize("max_tokens", [None, 800])
def test_every_native_body_sets_stream_false(json_mode, temperature, max_tokens):
    """MEASURED (probe C): omit `stream` and the response is 35 lines of NDJSON,
    `json.loads` raises JSONDecodeError('Extra data: line 2 column 1'), which is
    not an httpx type and carries no 'model' — so _is_backend_unavailable says
    False and every ingest goes terminal `failed`. No argument combination may
    drop it."""
    body = llm.build_native_body(
        model="m", messages=[], json_mode=json_mode,
        temperature=temperature, max_tokens=max_tokens,
    )
    assert body["stream"] is False
    assert body["think"] is False


def test_native_body_places_format_top_level_and_tuning_in_options():
    """MEASURED (probe A): `format` and `think` are top-level siblings of
    `stream`; `temperature`/`num_predict` live inside `options`."""
    body = llm.build_native_body(
        model="qwen3:4b",
        messages=[{"role": "user", "content": "hi"}],
        json_mode=True,
        temperature=0.1,
        max_tokens=800,
    )
    assert body["format"] == "json"
    assert body["options"] == {"temperature": 0.1, "num_predict": 800}
    assert "temperature" not in body
    assert "num_predict" not in body
    assert "max_tokens" not in body
    assert "response_format" not in body


def test_native_body_omits_format_and_options_when_not_requested():
    body = llm.build_native_body(model="m", messages=[])
    assert "format" not in body
    assert "options" not in body


def test_openai_body_sends_only_standard_openai_fields():
    """dreams/synthesize.py sends `think` and `chat_template_kwargs` on /v1
    unconditionally; ollama ignores them and real OpenAI 400s on unrecognised
    request arguments. A helper used by default-on paths must not inherit that."""
    body = llm.build_openai_body(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}],
        json_mode=True, temperature=0.1, max_tokens=800,
    )
    assert body == {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 800,
    }
    for forbidden in ("think", "chat_template_kwargs", "format", "options", "stream"):
        assert forbidden not in body


def test_openai_body_omits_optional_fields_when_unset():
    assert llm.build_openai_body(model="m", messages=[]) == {"model": "m", "messages": []}


# ---------------------------------------------------------------------------
# Response normalisation
# ---------------------------------------------------------------------------

def test_parse_native_response_tolerates_absent_thinking_key():
    """MEASURED (probe A/B): with think:false the message dict is exactly
    ['content', 'role'] — `thinking` is ABSENT, not present-and-empty. A
    subscript here would raise on every successful native call."""
    content, thinking = llm.parse_native_response(
        {"message": {"role": "assistant", "content": '{"ok": true}'}, "done": True}
    )
    assert content == '{"ok": true}'
    assert thinking == ""


def test_parse_native_response_returns_thinking_when_present():
    content, thinking = llm.parse_native_response(
        {"message": {"role": "assistant", "content": "", "thinking": "hmm" * 10}}
    )
    assert content == ""
    assert thinking == "hmm" * 10


@pytest.mark.parametrize("data", [{}, {"message": None}, None])
def test_parse_native_response_degrades_on_odd_shapes(data):
    assert llm.parse_native_response(data) == ("", "")


def test_parse_openai_response_reads_content_and_reasoning():
    content, reasoning = llm.parse_openai_response(
        {"choices": [{"message": {"content": "hi", "reasoning": "because"}}]}
    )
    assert (content, reasoning) == ("hi", "because")


def test_parse_openai_response_raises_on_missing_choices():
    """Stays as loud as the code it replaced: a malformed backend reply must not
    become a silent empty string."""
    with pytest.raises((KeyError, IndexError, TypeError)):
        llm.parse_openai_response({"choices": []})


# ---------------------------------------------------------------------------
# Probe + caching
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_accepts_only_2xx_json_object_with_version():
    """A 200 from a catch-all proxy must not be mistaken for ollama — a
    non-ollama backend has no /api/chat to fall back to."""
    cases = [
        (_ok_probe(), True),
        (httpx.Response(200, json={"status": "ok"}), False),      # no version key
        (httpx.Response(200, json=["version"]), False),           # not an object
        (httpx.Response(200, text="<html>hello</html>"), False),  # not JSON
        (httpx.Response(404, json={"version": "1"}), False),      # not 2xx
        (httpx.Response(500, json={"version": "1"}), False),
        (httpx.ConnectError("refused"), False),
    ]
    for response, expected in cases:
        llm.reset_probe_cache()
        with _probe(response):
            assert await llm.is_native(_S()) is expected


@pytest.mark.asyncio
async def test_probe_url_is_api_version_on_the_derived_root():
    with _probe(_ok_probe()) as urls:
        assert await llm.is_native(_S()) is True
    assert urls == ["http://ollama:11434/api/version"]


@pytest.mark.asyncio
async def test_verdict_is_cached_so_n_calls_cost_one_probe():
    with _probe(_ok_probe()) as urls:
        for _ in range(5):
            assert await llm.is_native(_S()) is True
    assert len(urls) == 1


@pytest.mark.asyncio
async def test_negative_verdict_expires_sooner_than_a_positive_one():
    """Asymmetric TTL: a probe that fails transiently while ollama restarts
    would otherwise pin every call onto the slow /v1 path for the full positive
    TTL — and with a tightened native budget that window produces a burst of
    hard failures rather than slow successes."""
    s = _S(LLM_NATIVE_PROBE_TTL_SECONDS=600.0)

    with _probe(httpx.ConnectError("down")):
        assert await llm.is_native(s) is False
    _, negative_expiry = llm._probe_cache["http://ollama:11434"]

    llm.reset_probe_cache()
    with _probe(_ok_probe()):
        assert await llm.is_native(s) is True
    _, positive_expiry = llm._probe_cache["http://ollama:11434"]

    assert positive_expiry - negative_expiry > 500  # 600s vs the 60s ceiling


@pytest.mark.asyncio
async def test_positive_ttl_is_capped_by_the_negative_ceiling_when_shorter():
    """A configured TTL below the 60s ceiling must not LENGTHEN the negative
    cache — min(), not a fixed 60."""
    s = _S(LLM_NATIVE_PROBE_TTL_SECONDS=5.0)
    with _probe(httpx.ConnectError("down")):
        assert await llm.is_native(s) is False
    _, expiry = llm._probe_cache["http://ollama:11434"]
    import time
    assert expiry - time.monotonic() <= 5.0


@pytest.mark.asyncio
async def test_cache_is_keyed_per_root():
    a = _S(LLM_BASE_URL="http://ollama-a:11434/v1")
    b = _S(LLM_BASE_URL="http://ollama-b:11434/v1")
    with _probe(_ok_probe()) as urls:
        await llm.is_native(a)
        await llm.is_native(b)
        await llm.is_native(a)
    assert urls == [
        "http://ollama-a:11434/api/version",
        "http://ollama-b:11434/api/version",
    ]


@pytest.mark.asyncio
async def test_no_v1_suffix_means_never_native_and_never_probed():
    with _probe(_ok_probe()) as urls:
        assert await llm.is_native(_S(LLM_BASE_URL="https://api.openai.com")) is False
    assert urls == []


@pytest.mark.asyncio
async def test_mode_never_skips_the_probe_entirely():
    with _probe(_ok_probe()) as urls:
        assert await llm.is_native(_S(LLM_NATIVE_CHAT="never")) is False
    assert urls == []


@pytest.mark.asyncio
async def test_mode_always_skips_the_probe_and_forces_native():
    with _probe(httpx.ConnectError("would have failed")) as urls:
        assert await llm.is_native(_S(LLM_NATIVE_CHAT="always")) is True
    assert urls == []


@pytest.mark.asyncio
async def test_mode_always_without_a_derivable_root_falls_back_not_crashes():
    s = _S(LLM_NATIVE_CHAT="always", LLM_BASE_URL="https://api.openai.com")
    with _probe(_ok_probe()):
        assert await llm.is_native(s) is False


# ---------------------------------------------------------------------------
# chat() — endpoint selection end to end
# ---------------------------------------------------------------------------

def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_chat_posts_the_native_shape_to_api_chat():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": '{"primary_type":"reference"}'},
            "done": True,
        })

    async with _client(handler) as client:
        with _probe(_ok_probe()):
            result = await llm.chat(
                settings=_S(), messages=[{"role": "user", "content": "doc"}],
                timeout=300.0, json_mode=True, temperature=0.1, client=client,
            )

    assert seen["url"] == "http://ollama:11434/api/chat"
    assert seen["body"]["stream"] is False
    assert seen["body"]["think"] is False
    assert seen["body"]["format"] == "json"
    assert result.endpoint == "native"
    assert result.content == '{"primary_type":"reference"}'
    assert result.reasoning == ""


@pytest.mark.asyncio
async def test_chat_posts_the_openai_shape_when_not_native():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hi", "reasoning": "long think"}}]
        })

    async with _client(handler) as client:
        result = await llm.chat(
            settings=_S(LLM_NATIVE_CHAT="never"),
            messages=[{"role": "user", "content": "doc"}],
            timeout=300.0, json_mode=True, temperature=0.1, client=client,
        )

    assert seen["url"] == "http://ollama:11434/v1/chat/completions"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert "think" not in seen["body"]
    assert result.endpoint == "openai"
    assert result.content == "hi"
    # Kept, not discarded: when a model returns nothing usable this is the only
    # thing left to look at.
    assert result.reasoning == "long think"


@pytest.mark.asyncio
async def test_chat_sends_bearer_token_only_when_an_api_key_is_set():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    async with _client(handler) as client:
        await llm.chat(settings=_S(LLM_NATIVE_CHAT="never"), messages=[],
                       timeout=1.0, client=client)
        await llm.chat(settings=_S(LLM_NATIVE_CHAT="never", LLM_API_KEY="sk-1"),
                       messages=[], timeout=1.0, client=client)

    assert seen == [None, "Bearer sk-1"]


# ---------------------------------------------------------------------------
# The 4xx safety net
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 405, 501])
async def test_pre_generation_4xx_demotes_and_retries_once_on_v1(status):
    """An older ollama daemon may reject the `think` flag outright. Without this
    net, upgrading ollama becomes a prerequisite of the change. These codes come
    back BEFORE generation starts, so the retry is ~free."""
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.path == "/api/chat":
            return httpx.Response(status, json={"error": "unknown field think"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    s = _S()
    async with _client(handler) as client:
        with _probe(_ok_probe()):
            result = await llm.chat(settings=s, messages=[], timeout=1.0, client=client)

    assert urls == [
        "http://ollama:11434/api/chat",
        "http://ollama:11434/v1/chat/completions",
    ]
    assert result.endpoint == "openai"
    assert result.content == "ok"

    # The verdict is demoted process-wide, so the NEXT call goes straight to
    # /v1 without re-probing — otherwise every call would pay the failed native
    # attempt until the TTL expired.
    with _probe(_ok_probe()) as probe_urls:
        assert await llm.is_native(s) is False
    assert probe_urls == []


@pytest.mark.asyncio
async def test_5xx_on_the_native_path_propagates_without_retry():
    """A 5xx may arrive mid-generation, where a retry costs a second full
    generation — the opposite of free."""
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(503, json={"error": "overloaded"})

    async with _client(handler) as client:
        with _probe(_ok_probe()):
            with pytest.raises(httpx.HTTPStatusError):
                await llm.chat(settings=_S(), messages=[], timeout=1.0, client=client)

    assert urls == ["http://ollama:11434/api/chat"]  # no retry


@pytest.mark.asyncio
async def test_timeout_propagates_and_is_not_retried():
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        raise httpx.ReadTimeout("too slow", request=request)

    async with _client(handler) as client:
        with _probe(_ok_probe()):
            with pytest.raises(httpx.ReadTimeout):
                await llm.chat(settings=_S(), messages=[], timeout=1.0, client=client)

    assert len(urls) == 1


@pytest.mark.asyncio
async def test_4xx_on_the_openai_path_is_not_retried():
    """The demote-and-retry is native-only; there is nowhere to fall back to."""
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(400, json={"error": "bad request"})

    async with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await llm.chat(settings=_S(LLM_NATIVE_CHAT="never"), messages=[],
                           timeout=1.0, client=client)

    assert len(urls) == 1


# ---------------------------------------------------------------------------
# Timeout selection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_native_timeout_applies_only_on_the_native_endpoint():
    """The two endpoints have different latency regimes (83.19s vs 4.00s on the
    same document), so one number cannot be both a safe ceiling for /v1 and a
    useful bound on native. A deployment whose probe says NOT-native must keep
    the full /v1 budget, or the reduction converts today's slow successes into
    guaranteed timeouts."""
    for mode, expected in (("always", 55.0), ("never", 300.0)):
        constructed = []

        def _factory(*_a, **kw):
            constructed.append(kw.get("timeout"))
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.post = AsyncMock(return_value=httpx.Response(
                200,
                json={"message": {"content": "x"}, "choices": [{"message": {"content": "x"}}]},
                request=httpx.Request("POST", "http://stub/"),
            ))
            return client

        with patch("app.llm.httpx.AsyncClient", side_effect=_factory):
            await llm.chat(
                settings=_S(LLM_NATIVE_CHAT=mode), messages=[],
                timeout=300.0, native_timeout=55.0,
            )

        assert constructed == [expected], f"mode={mode}"


@pytest.mark.asyncio
async def test_omitting_native_timeout_uses_the_single_budget_for_both():
    constructed = []

    def _factory(*_a, **kw):
        constructed.append(kw.get("timeout"))
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=httpx.Response(
            200, json={"message": {"content": "x"}},
            request=httpx.Request("POST", "http://stub/"),
        ))
        return client

    with patch("app.llm.httpx.AsyncClient", side_effect=_factory):
        await llm.chat(settings=_S(LLM_NATIVE_CHAT="always"), messages=[], timeout=42.0)

    assert constructed == [42.0]


@pytest.mark.asyncio
async def test_reset_probe_cache_clears_the_verdict():
    s = _S()
    with _probe(_ok_probe()) as urls:
        await llm.is_native(s)
        llm.reset_probe_cache()
        await llm.is_native(s)
    assert len(urls) == 2
