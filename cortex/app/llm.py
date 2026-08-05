"""Chat-endpoint selection, body construction and response normalisation.

ONE entry point — `chat()` — owns the decision of whether a chat call goes to
ollama's NATIVE `/api/chat` or to the OpenAI-compatible `/v1/chat/completions`.
Callers pass messages and a timeout; they do not build bodies or parse responses.

WHY THIS EXISTS (measured, 2026-08-04, live against the production VPS —
ollama 0.32.4, qwen3:4b, 4 vCPU, inside the cortex container; identical
two-procedure runbook document in every probe):

    GET http://ollama:11434/api/version
        -> 200 {"version":"0.32.4"}

    A  /api/chat  {stream:False, think:False, format:"json",
                   options:{temperature:0.1, num_predict:800}}
        -> 200 in 16.18s COLD (includes model load), valid JSON content
    B  same, WARM
        -> 200 in 4.00s  (load_duration 0.25s)
    C  same but "stream" OMITTED
        -> 200, but the body is NDJSON: 35 newline-delimited objects.
           json.loads() raises JSONDecodeError("Extra data: line 2 column 1").
    D  same but "think" OMITTED
        -> 200 in 111.20s, message.thinking present with 3552 chars of reasoning
    E  /v1/chat/completions with EXACTLY what classifier.py sent before this
       module existed ({temperature, response_format:{type:json_object}})
        -> 200 in 83.19s, message.reasoning 1978 chars, 26 content tokens

So: 83.19s -> 4.00s, a 20.8x saving on that document (the audit measured 288.9s
-> 3.3s, 87x, on a larger one). The saving is ENDPOINT-LEVEL and therefore
model-independent for any thinking model: ollama honours `think:false` on
`/api/chat` and silently IGNORES it — along with `chat_template_kwargs.
enable_thinking` — on `/v1/chat/completions`. Probe D proves the flag, not the
endpoint, is the lever; probe E proves `/v1` will not apply it.

THREE FACTS TAKEN FROM THE WIRE, NOT FROM A DESIGN DOCUMENT:

1. `format` and `think` are TOP-LEVEL siblings of `stream`, not `options` keys.
   `num_predict` and `temperature` DO live inside `options`.
2. `"stream": False` is MANDATORY (probe C). Omit it and ollama streams NDJSON,
   `resp.json()` raises, and — because JSONDecodeError is not an httpx type and
   carries no 'model' in its message — `classifier._is_backend_unavailable`
   returns False and the ingest goes terminal `failed`. That is the single most
   likely way to break this change, so `build_native_body` always emits it and
   `tests/test_llm.py` asserts it on every native body.
3. With `think:false` the response's `message` dict has keys `['content',
   'role']` — the `thinking` key is ABSENT ENTIRELY, not present-and-empty
   (contrast probe D, where it appears with 3552 chars). `parse_native_response`
   therefore uses `.get()`; a subscript would raise on every successful call.

Native response shape (probe A/B): `{model, created_at, message: {role, content
[, thinking]}, done, done_reason, eval_count, eval_duration, load_duration,
prompt_eval_count, prompt_eval_duration, total_duration}`.

STRUCTURED OUTPUTS (`json_schema=`, added 2026-08-04 after the measurements
below). `json_mode=True` constrains output to SYNTACTICALLY VALID JSON and
NOTHING MORE — it enforces no schema, on either endpoint. Measured on the same
VPS/model with the decision board's own suggestion prompt (three questions,
system prompt documenting a `{question_id: {suggested_answers,
suggested_actions}}` contract):

    F  format:"json"      -> 20.62s / 16.31s, 0/3 questions answered BOTH runs.
       The model MIRRORED THE USER MESSAGE back — top-level keys `['context',
       'questions']`, and not even cleanly (one run emitted the corrupted key
       `"evidence_sn:"`). Handed a JSON input under a "be valid JSON"
       constraint, a small model reproduces the input's shape.
    G  format:<json schema> -> 16.55s / 14.81s, 3/3 answered BOTH runs, top-level
       keys exactly `['q0','q1','q2']`.
    H  as G plus `minItems:1` -> 24.51s, 3/3 — no adherence gain over G, so the
       extra constraint buys only latency and pressure to invent. Not used.
    I  G, latency vs question count: n=1 8.16s, n=3 22.95s, n=8 52.75s
       (43/126/328 output tokens) — constrained decode is bounded by tokens
       emitted, as unconstrained decode is. A schema does not make a big board
       fit a small budget.

So the schema is not a tuning knob, it is the difference between a feature that
answers and one that echoes. Passing `json_schema` sets ollama's native `format`
to the schema object and `/v1`'s `response_format` to the OpenAI
`{"type":"json_schema", ...}` shape. `json_mode` is IMPLIED by a schema (see
`chat`) so the schema-dropped fallback below is still JSON rather than prose.

NON-OLLAMA SAFETY. Structured outputs are not universal: a `/v1` backend that
does not implement `response_format.type = "json_schema"` rejects the request.
There is no capability endpoint to feature-detect against, so `chat` uses the
mechanism already in this file — a pre-generation 4xx triggers ONE retry with
the schema dropped, on the same endpoint, degrading to plain json mode rather
than failing. `422` joins `_DEMOTE_STATUS_CODES` for this: vLLM's server is
FastAPI, whose request-validation rejection is a 422, and it is pre-generation
exactly like the other four. A schema-dropped call returns unconstrained output,
which is what the caller's own adherence check is for — `decision/synthesize.py`
reports `degraded` when the payload grounds nothing.

THE OPENAI BRANCH SENDS ONLY STANDARD OPENAI FIELDS. `dreams/profile.py`
sends `think` and `chat_template_kwargs` on the `/v1` path unconditionally
(`dreams/synthesize.py` did too until it was converted to this module); nobody
has been burned because `DREAM_ENABLED=false`. Real OpenAI rejects
unrecognised request arguments with a 400, so a helper used by default-on paths
cannot inherit that. The non-native path here is strictly more standards-
compliant than the code it replaces.

THIS MODULE RAISES. It never swallows a transport or HTTP failure, because
`knowledge/classifier.py` inspects the caught exception to choose between the
`corpus_only` and `failed` terminal states, and a helper that returned an empty
result on failure would collapse that distinction. Every caller already wraps
its call in a try/except and keeps its own degradation semantics.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# The probe: cheapest possible yes/no. `/api/tags` would also answer it but
# returns the whole model inventory, and conflating liveness detection with
# model-availability checking puts a second failure mode into the endpoint
# decision (dreams/task.py already owns the model-availability question).
_PROBE_PATH = "/api/version"
_PROBE_TIMEOUT_SECONDS = 2.0

# A negative verdict expires far sooner than a positive one. A probe that fails
# transiently while ollama restarts would otherwise pin every call onto the
# ~83-289s `/v1` path for the full positive TTL.
_NEGATIVE_TTL_CEILING_SECONDS = 60.0

# Returned BEFORE generation starts, so retrying is ~free. An older ollama
# daemon may reject the `think` flag outright, and a `/v1` backend that has not
# implemented structured outputs rejects `response_format.type=json_schema`;
# without this net, either would become a hard prerequisite. Deliberately
# excludes 5xx and timeouts — those may arrive mid-generation, where a retry
# costs a second full generation.
#
# 422 is here for the schema fallback specifically: vLLM's OpenAI-compatible
# server is FastAPI, whose request-validation rejection is a 422 rather than a
# 400. It satisfies this set's one criterion — pre-generation — exactly like the
# other four, so it is safe on the endpoint-demotion path too.
_DEMOTE_STATUS_CODES = frozenset({400, 404, 405, 422, 501})

_NATIVE = "native"
_OPENAI = "openai"

# {native_root: (verdict, expires_at_monotonic)}
#
# THERE IS DELIBERATELY NO asyncio.Lock AROUND THIS. Do not "fix" it. The Celery
# worker runs `--pool=solo` and drives async code through a FRESH EVENT LOOP per
# task; a module-level asyncio.Lock binds to the loop that first acquires it and
# raises in every subsequent one. The benign race this leaves costs at most a
# few duplicate 2s probes on cold start. A cross-loop lock costs a crash in the
# worker.
_probe_cache: dict[str, tuple[bool, float]] = {}

# One INFO line per (root, verdict) per process, not one per call.
_logged_verdicts: set[tuple[str, bool]] = set()


@dataclass(frozen=True)
class ChatResult:
    """Normalised chat response.

    `reasoning` is kept rather than discarded even though `think:false` makes it
    empty by design: when a model returns nothing usable, it is the only thing
    left to look at. Callers log it bounded at DEBUG; nobody parses it.
    """

    content: str
    reasoning: str
    endpoint: str  # _NATIVE | _OPENAI
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


# --------------------------------------------------------------------------
# Pure helpers — no I/O, testable without a network
# --------------------------------------------------------------------------

def native_root(base_url: str) -> str | None:
    """Derive ollama's native root from an OpenAI-compatible base URL.

    `http://ollama:11434/v1` -> `http://ollama:11434`. A trailing slash is
    tolerated. Anything NOT ending in `/v1` yields None: without that suffix
    there is no defensible way to construct a native URL, so such a deployment
    never goes native (`LLM_NATIVE_BASE_URL` is the operator's escape hatch).

    Path-prefixed proxies derive their own prefix — `https://gw/openai/v1` ->
    `https://gw/openai` — which the probe then either confirms or rejects.
    """
    if not base_url or not isinstance(base_url, str):
        return None
    trimmed = base_url.strip().rstrip("/")
    if not trimmed.endswith("/v1"):
        return None
    root = trimmed[: -len("/v1")].rstrip("/")
    return root or None


def _resolve_root(settings: Any) -> str | None:
    """Operator override first, then derivation from LLM_BASE_URL.

    Both are type-guarded to `str`. A settings object that hands back something
    else (a MagicMock in a test, a mis-typed env override) must fall through to
    "no native root" — i.e. to `/v1` — rather than be string-formatted into a
    nonsense probe URL.
    """
    override = getattr(settings, "LLM_NATIVE_BASE_URL", "")
    if isinstance(override, str) and override.strip():
        return override.strip().rstrip("/") or None
    base = getattr(settings, "LLM_BASE_URL", "")
    return native_root(base) if isinstance(base, str) else None


def build_native_body(
    *,
    model: str,
    messages: list[dict],
    json_mode: bool = False,
    json_schema: dict | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    """Body for ollama's native `/api/chat`.

    `stream` and `think` are unconditional: `stream:False` because streaming
    NDJSON breaks `resp.json()` (probe C), `think:False` because it is the
    entire point of using this endpoint (probe D: 4.00s vs 111.20s).
    `temperature`/`num_predict` go inside `options`; `format` stays top level.

    `format` carries the SCHEMA OBJECT when one is supplied — that is ollama's
    structured-outputs surface, the same field that otherwise carries the string
    `"json"`. A schema therefore supersedes `json_mode` rather than combining
    with it: probe F/G measured that the string alone constrains syntax only and
    let qwen3:4b mirror the input back on every run.
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
    }
    if json_schema is not None:
        body["format"] = json_schema
    elif json_mode:
        body["format"] = "json"

    options: dict[str, Any] = {}
    if temperature is not None:
        options["temperature"] = temperature
    if max_tokens is not None:
        options["num_predict"] = max_tokens
    if options:
        body["options"] = options
    return body


def build_openai_body(
    *,
    model: str,
    messages: list[dict],
    json_mode: bool = False,
    json_schema: dict | None = None,
    json_schema_name: str = "response",
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    """Body for `/v1/chat/completions` — standard OpenAI fields ONLY.

    No `think`, no `chat_template_kwargs`, no `format`: ollama ignores them here
    and real OpenAI 400s on unrecognised arguments. `response_format` IS
    standard, in both its `json_object` and its `json_schema` form, so the
    schema goes in the OpenAI envelope rather than a vendor field.

    `strict: True` is what makes the schema binding rather than advisory on
    OpenAI; a backend that has not implemented `json_schema` at all rejects the
    request pre-generation, which `chat` answers by retrying once without it.
    `json_schema_name` must match OpenAI's `^[a-zA-Z0-9_-]{1,64}$` — callers own
    it, it is never derived from user input.
    """
    body: dict[str, Any] = {"model": model, "messages": messages}
    if json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": json_schema_name,
                "strict": True,
                "schema": json_schema,
            },
        }
    elif json_mode:
        body["response_format"] = {"type": "json_object"}
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    return body


def parse_native_response(data: Any) -> tuple[str, str]:
    """`{"message": {"content": ..., "thinking": ...}}` -> (content, thinking).

    `.get()` on both, because with `think:false` the `thinking` key is absent
    entirely (probe A/B) — a subscript would raise on every successful call.
    """
    message = (data or {}).get("message") or {}
    return (message.get("content") or "", message.get("thinking") or "")


def parse_openai_response(data: Any) -> tuple[str, str]:
    """`{"choices":[{"message":{...}}]}` -> (content, reasoning).

    The container is indexed strictly — a response with no `choices` raises, as
    it did before this module existed, so a malformed backend reply stays a
    loud failure rather than becoming a silent empty string.
    """
    message = data["choices"][0]["message"]
    return (message.get("content") or "", message.get("reasoning") or "")


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def reset_probe_cache() -> None:
    """Drop every cached verdict. For tests; also a safe no-op in production."""
    _probe_cache.clear()
    _logged_verdicts.clear()


def _cached(root: str) -> bool | None:
    entry = _probe_cache.get(root)
    if entry is None:
        return None
    verdict, expires_at = entry
    if time.monotonic() >= expires_at:
        _probe_cache.pop(root, None)
        return None
    return verdict


def _remember(root: str, verdict: bool, *, positive_ttl: float) -> None:
    ttl = positive_ttl if verdict else min(_NEGATIVE_TTL_CEILING_SECONDS, positive_ttl)
    _probe_cache[root] = (verdict, time.monotonic() + max(0.0, ttl))
    key = (root, verdict)
    if key not in _logged_verdicts:
        _logged_verdicts.add(key)
        logger.info(
            "LLM chat endpoint for %s: %s (probe %s)",
            root,
            "native /api/chat" if verdict else "/v1/chat/completions",
            "succeeded" if verdict else "failed or backend is not ollama",
        )


async def _probe(root: str) -> bool:
    """GET {root}/api/version. Ollama ONLY if 2xx AND a JSON object with a
    `version` key — a catch-all proxy answering 200 must not be mistaken for
    ollama, and a non-ollama backend has no `/api/chat` to fall back to.

    Fails toward `/v1` on anything unexpected: `/v1` is the contract, native is
    the optimisation (same idiom as `write_text_if_changed` failing toward
    writing and `resolver.is_bypassed` failing toward not-bypassed).
    """
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            resp = await client.get(f"{root}{_PROBE_PATH}")
        if not (200 <= resp.status_code < 300):
            return False
        data = resp.json()
        return isinstance(data, dict) and "version" in data
    except Exception as exc:  # noqa: BLE001 — any failure means "not native"
        logger.debug("Native-endpoint probe of %s failed: %s", root, exc)
        return False


async def is_native(settings: Any) -> bool:
    """Resolve `LLM_NATIVE_CHAT` (auto|always|never) into an endpoint choice."""
    mode = str(getattr(settings, "LLM_NATIVE_CHAT", "auto") or "auto").strip().lower()
    root = _resolve_root(settings)

    if mode == "never":
        return False

    if mode == "always":
        if root:
            # Returns BEFORE consulting the cache, which means `always` also
            # ignores a verdict demoted by `chat`'s 4xx safety net. That is the
            # intended reading of "always" — an explicit operator override is
            # not something a probe result may quietly overrule — but it has a
            # cost worth stating: against an older ollama that 400s on `think`,
            # every single call pays a wasted native round trip before falling
            # back, forever, because the demotion can never be read. `auto` is
            # the mode that learns; `always` is the mode that obeys.
            return True
        logger.warning(
            "LLM_NATIVE_CHAT=always but no native root is derivable from "
            "LLM_BASE_URL=%r (it must end in /v1) and LLM_NATIVE_BASE_URL is "
            "unset — falling back to /v1/chat/completions",
            getattr(settings, "LLM_BASE_URL", ""),
        )
        return False

    if not root:
        return False

    cached = _cached(root)
    if cached is not None:
        return cached

    verdict = await _probe(root)
    _remember(
        root,
        verdict,
        positive_ttl=float(getattr(settings, "LLM_NATIVE_PROBE_TTL_SECONDS", 600.0)),
    )
    return verdict


def _demote(settings: Any, root: str) -> None:
    """Force the cached verdict negative after a pre-generation 4xx."""
    _remember(
        root,
        False,
        positive_ttl=float(getattr(settings, "LLM_NATIVE_PROBE_TTL_SECONDS", 600.0)),
    )


# --------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------

async def chat(
    *,
    settings: Any,
    messages: list[dict],
    timeout: float,
    json_mode: bool = False,
    json_schema: dict | None = None,
    json_schema_name: str = "response",
    temperature: float | None = None,
    max_tokens: int | None = None,
    native_timeout: float | None = None,
    client: httpx.AsyncClient | None = None,
    purpose: str = "",
) -> ChatResult:
    """Post one chat completion to whichever endpoint this backend supports.

    `timeout` bounds the `/v1` call. `native_timeout`, when supplied, bounds the
    native call instead — the two endpoints have genuinely different latency
    regimes (83.19s vs 4.00s on the same document), and a single number cannot
    be simultaneously a safe ceiling for the slow one and a useful one for the
    fast one. Callers that do not care may omit it and get `timeout` for both.

    `json_schema`, when supplied, constrains generation to that shape — see the
    module docstring for why `json_mode` alone does not. It is a request, not a
    guarantee: a backend that rejects it pre-generation gets ONE retry with the
    schema dropped, so a deploy against vLLM/LiteLLM/OpenAI that has not
    implemented structured outputs keeps working at the pre-schema quality
    rather than failing.

    RAISES on transport or HTTP failure. Callers own their own degradation.
    """
    # A schema implies JSON output. Coerced once, here, so the schema-dropped
    # fallback attempt is still json-mode: a caller that passed only a schema
    # would otherwise fall back to a body carrying NO output constraint at all,
    # turning a backend's polite 400 into free-form prose and a JSONDecodeError.
    json_mode = json_mode or json_schema is not None

    native = await is_native(settings)
    root = _resolve_root(settings) if native else None

    def _plan(use_native: bool, use_schema: bool) -> tuple[str, dict, float]:
        schema = json_schema if use_schema else None
        if use_native:
            return (
                f"{root}/api/chat",
                build_native_body(
                    model=settings.LLM_MODEL,
                    messages=messages,
                    json_mode=json_mode,
                    json_schema=schema,
                    temperature=temperature,
                    max_tokens=max_tokens,
                ),
                timeout if native_timeout is None else native_timeout,
            )
        return (
            f"{settings.LLM_BASE_URL}/chat/completions",
            build_openai_body(
                model=settings.LLM_MODEL,
                messages=messages,
                json_mode=json_mode,
                json_schema=schema,
                json_schema_name=json_schema_name,
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            timeout,
        )

    api_key = getattr(settings, "LLM_API_KEY", "") or ""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def _post(use_native: bool, use_schema: bool) -> httpx.Response:
        url, body, budget = _plan(use_native, use_schema)
        if client is not None:
            resp = await client.post(url, json=body, headers=headers, timeout=budget)
        else:
            async with httpx.AsyncClient(timeout=budget) as own:
                resp = await own.post(url, json=body, headers=headers)
        resp.raise_for_status()
        return resp

    # The ladder of attempts, most-capable first. Each rung drops exactly ONE
    # capability, so a pre-generation rejection is answered by giving up the
    # least it can. Without a schema this is bit-identical to the two-rung
    # native->/v1 ladder that shipped before structured outputs.
    #
    # Deliberately no (/v1, schema) rung below a failed (native, schema): on
    # ollama both endpoints are the same engine, so a schema the native handler
    # rejects will not be honoured by its own /v1 either — that rung would only
    # buy a wasted round trip.
    attempts: list[tuple[bool, bool]] = []
    if json_schema is not None:
        attempts.append((native, True))
    attempts.append((native, False))
    if native:
        attempts.append((False, False))

    resp = None
    # The endpoint whose schema-carrying attempt was rejected, if any. Recorded
    # rather than acted on immediately: at rejection time we know only THAT the
    # request was refused, never WHY. An older ollama rejecting `think` refuses
    # the schema-carrying body too, so a diagnosis logged here would announce
    # "this backend does not implement structured outputs" about a backend whose
    # actual complaint was a different field — and would do it one line before
    # the demote message that gives the real reason.
    schema_rejected_on: bool | None = None

    for index, (use_native, use_schema) in enumerate(attempts):
        try:
            resp = await _post(use_native, use_schema)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else None
            last = index == len(attempts) - 1
            if last or status not in _DEMOTE_STATUS_CODES:
                raise
            next_native, next_schema = attempts[index + 1]
            if use_schema and not next_schema:
                schema_rejected_on = use_native
                # Strictly what happened. No cause claimed.
                logger.warning(
                    "%s returned %s for %s with a JSON schema — retrying once "
                    "without it",
                    "Native /api/chat" if use_native else "/v1/chat/completions",
                    status,
                    purpose or "chat",
                )
            if use_native and not next_native:
                # Pre-generation rejection (most likely an older ollama that
                # does not know `think`). Demote the cached verdict and retry
                # once on the contract endpoint.
                #
                # The demotion is NOT permanent: it is stored with the negative
                # TTL (60s), so `auto` re-probes after it lapses. That is
                # deliberate and better than latching — an ollama upgraded to a
                # version that accepts `think` is picked up within a minute with
                # no restart — and the cost of being wrong is bounded to one
                # wasted round trip per minute rather than one per call.
                #
                # It fires ONLY on this transition. A native call that failed
                # merely because of the schema is retried natively without it,
                # and must not cost the process its native verdict: the endpoint
                # was never the problem.
                logger.warning(
                    "Native /api/chat returned %s for %s — demoting to "
                    "/v1/chat/completions and retrying once",
                    status,
                    purpose or "chat",
                )
                if root:
                    _demote(settings, root)
            continue

        # The diagnosis, emitted only where it is earned: the SAME endpoint that
        # refused the schema accepted the identical request without it. That is
        # the only evidence available that the schema was the thing it objected
        # to. A success on the OTHER endpoint proves nothing about the schema —
        # the demote line above already explains that case — so it stays silent.
        if (schema_rejected_on is not None
                and not use_schema
                and use_native == schema_rejected_on):
            logger.warning(
                "%s accepted %s without the JSON schema it had just rejected — "
                "this backend appears not to implement structured outputs, so "
                "output adherence is not enforced for this call",
                "Native /api/chat" if use_native else "/v1/chat/completions",
                purpose or "chat",
            )
        native = use_native
        break

    data = resp.json()
    if native:
        content, reasoning = parse_native_response(data)
    else:
        content, reasoning = parse_openai_response(data)

    if reasoning:
        logger.debug(
            "%s: model returned %d chars of reasoning (endpoint=%s)",
            purpose or "chat",
            len(reasoning),
            _NATIVE if native else _OPENAI,
        )

    return ChatResult(
        content=content,
        reasoning=reasoning,
        endpoint=_NATIVE if native else _OPENAI,
        raw=data if isinstance(data, dict) else {},
    )
