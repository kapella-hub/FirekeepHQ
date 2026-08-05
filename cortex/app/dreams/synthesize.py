"""One guarded LLM call: a cluster of episodes -> 1-3 durable insights.

The call goes through `app.llm.chat`, which picks ollama's NATIVE `/api/chat`
when the backend confirms as ollama and falls back to `/v1/chat/completions`
otherwise. That indirection is not tidiness — it is the difference between this
feature working on a CPU deploy and not existing at all.

WHY, measured rather than reasoned. `think:false` is what makes a thinking
model answer instead of reasoning until the budget runs out: on the VPS
(qwen3:4b, 4 vCPU) a synthesis with it takes 22.5s and returns correct JSON,
and without it the model burns its entire completion budget on thinking that
the JSON grammar blocks, returning EMPTY content after ~101s. **Ollama honours
that flag on `/api/chat` and silently IGNORES it — along with
`chat_template_kwargs.enable_thinking` — on `/v1/chat/completions`.** This
module used to post straight to `{LLM_BASE_URL}/chat/completions`, and
`LLM_BASE_URL` ends in `/v1`, so it sent both spellings of a flag the endpoint
threw away and paid the full reasoning cost on every call.

What that cost, measured on the production VPS 2026-08-04 (qwen3:4b, 4 vCPU,
minimum cluster of 4 members, 2,595-char prompt — i.e. the SMALLEST unit of
work this pass ever attempts):

    via /v1, the code this replaced   -> >400s, did not complete
                                         (hit a 400s client timeout)
    DREAM_SYNTH_TIMEOUT_SECONDS       -> 45.0
    an actual dream tick              -> "Dream synthesis failed:" (a bare
                                         TimeoutError with an EMPTY str),
                                         47.3s, 0 insights
    native /api/chat, think:false     -> 22.5s

The budget was under a NINTH of what the slow endpoint needed for the easiest
possible cluster — and 400s is a floor, not a completion time, because the
probe gave up first. So dreaming was INOPERABLE on a CPU deploy, not degraded,
and the one log line it left behind named neither the endpoint nor the
exception type. The budget alone could not have fixed it (see `_MAX_COMPLETION_TOKENS`,
which was already raised 700 -> 4000 for a related symptom): on `/v1` the
reasoning runs no matter how the request is phrased, so only the endpoint
choice removes it. Conversion was deliberately deferred out of LLM-endpoint
phases 1-3 and is paid off here.

STILL ON THE OLD PATH: `profile.py`, which posts to `/v1` itself using
`build_request_body` below. It is a separate call with its own failure profile
and is out of scope for this change; the function stays for it and says so.

`llm.chat` RAISES on transport or HTTP failure by contract — callers own their
degradation. `synthesize` therefore keeps its own guard and its own retry rule,
both unchanged: malformed model JSON gets exactly one more call, anything else
returns [] at once.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app import llm
from app.dreams.select import Candidate

logger = logging.getLogger(__name__)

_MAX_INSIGHTS = 3
_EPISODE_TRUNCATE_CHARS = 600
_DEFAULT_MAX_CHARS = 800

# Completion budget for one synthesis call — sized to ABSORB a thinking model's
# blocked reasoning, not just the JSON answer (which is ~200-400 tokens).
#
# Passed as `llm.chat(max_tokens=...)`, which lands as `options.num_predict` on
# the native endpoint and `max_tokens` on `/v1`. It is kept at the /v1-sized
# value even though the native path generates no reasoning to absorb: the probe
# confirms OLLAMA, not a THINKING MODEL, and the /v1 path is still reachable
# (any non-ollama backend, or an ollama that rejects `think` pre-generation and
# gets demoted). A budget that is generous for the fast path costs nothing —
# the grammar terminates a JSON completion on its own — while one sized for the
# fast path re-creates the starvation below on every slow-path call.
#
# It was 700, and 700 starved the feature on its own documented reference
# configuration. Measured live against ollama 0.17.5 with this exact request
# body, 3 probes out of 3: HTTP 200, `finish_reason='length'`,
# `completion_tokens=700` (i.e. exactly the cap), **content length 0**,
# reasoning length ~3200 chars. The identical call with `max_tokens=4000`
# returned correct JSON. So it is budget starvation, not a broken model and not
# a broken flag: `think:false` / `chat_template_kwargs.enable_thinking` are
# ignored on `/v1/chat/completions` (see the module docstring), the reasoning
# runs regardless, the JSON grammar blocks it from being emitted as content, and
# the cap is hit before a single content token exists. Field impact: 2 of 3
# clusters produced ZERO insights, reproducibly, across two full runs — and a
# zero-insight cluster is never added to `dreams:consolidated`, so those
# clusters were re-selected and re-attempted on every subsequent run forever.
#
# Deliberately NOT derived from `DREAM_MAX_INSIGHT_CHARS` and deliberately not a
# config field. That setting caps the CONTENT of each insight (800 chars,
# enforced by `parse_insights`); the tokens this number has to cover are
# overwhelmingly reasoning the content cap knows nothing about, so tying them
# would make raising the content cap silently shrink the reasoning headroom and
# vice versa. It is also read by `build_request_body`, which is a pure function
# used by `profile.py` and has no `Settings` in scope.
#
# KNOWN INTERACTION, stated rather than hidden: on a `/v1` deployment this
# raises worst-case wall time, because the reasoning tokens now actually get
# generated instead of being truncated at 700. `DREAM_SYNTH_TIMEOUT_SECONDS`
# (45.0) is the only control that binds under `--pool=solo`, so on slow CPU
# inference the call can time out where it previously returned fast-and-empty.
# Both outcomes yield zero insights; the difference is that the timeout is loud
# (a WARNING from `synthesize`) and `GET /dreams` reports `degraded` rather than
# `ok`. That was the state this module shipped in and the reason it was
# converted: the endpoint, not the budget, is what removes the reasoning. The
# note remains live for `profile.py`, which still posts to `/v1` directly, and
# for any synthesis routed down the `/v1` fallback.
_MAX_COMPLETION_TOKENS = 4000


def _system_prompt(max_chars: int) -> str:
    """The char budget is interpolated in so the model has a concrete length
    cue — without one it has no idea what "concise" means and can burn a full
    ~22s CPU call producing insights parse_insights then silently discards for
    being over max_chars."""
    return (
        "You are the Dreaming pass for a long-term agent memory store. You are given "
        "a cluster of episodic memories that were judged similar to each other. Find "
        "1-3 DURABLE, GENERAL insights that are each supported by MULTIPLE episodes "
        "in the cluster — not a restatement of any single episode.\n\n"
        "Rules:\n"
        "- Each insight must be a general lesson, not a specific event.\n"
        "- Each insight must be supported by at least 2 episodes; cite them by index.\n"
        "- Do not invent facts not present in the episodes.\n"
        f"- Keep each insight under {max_chars} characters; longer insights are discarded.\n\n"
        "## Output Format\n"
        "Return ONLY a valid JSON object. No markdown fencing, no explanation.\n\n"
        "{\n"
        '  "insights": [\n'
        '    {"content": "<the durable lesson>", "memory_type": "procedural", '
        '"source_indices": [0, 2]}\n'
        "  ]\n"
        "}"
    )


@dataclass
class Insight:
    content: str
    memory_type: str
    source_ids: list[str] = field(default_factory=list)


def build_messages(members: list[Candidate], *, max_chars: int = _DEFAULT_MAX_CHARS) -> list[dict]:
    lines = []
    for i, member in enumerate(members):
        text = member.text[:_EPISODE_TRUNCATE_CHARS]
        lines.append(f"[{i}] {text}")
    user_content = "\n".join(lines)
    return [
        {"role": "system", "content": _system_prompt(max_chars)},
        {"role": "user", "content": user_content},
    ]


def build_request_body(model: str, messages: list[dict]) -> dict:
    """`/v1/chat/completions` body — used ONLY by `profile.py` now.

    `synthesize()` below no longer calls this: it goes through `app.llm.chat`,
    which builds the body for whichever endpoint it selects. Kept rather than
    deleted because `profile.py` still imports it (`from app.dreams.synthesize
    import build_request_body`) and is not part of that conversion, and it is
    named here rather than left to a grep because a body builder that describes
    nobody's request is exactly how this module's docstring came to lie.

    So the caveat below is a live description of the profile call, not a
    historical note: `think` / `chat_template_kwargs.enable_thinking` are
    correct and honoured on ollama's native `/api/chat`, and IGNORED on
    `/v1/chat/completions`, which is where profile.py posts. That is why the
    budget has to cover reasoning tokens there.
    """
    return {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "think": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": _MAX_COMPLETION_TOKENS,
    }


def parse_insights(raw: str, members: list[Candidate], *, max_chars: int) -> list[Insight]:
    """Validate and convert raw LLM JSON into Insights. Never raises — any
    malformed input (bad JSON, wrong shape, out-of-range indices, empty/overlong
    content) is rejected item-by-item or as a whole, returning [] at worst."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []

    if not isinstance(data, dict):
        return []

    raw_insights = data.get("insights")
    if not isinstance(raw_insights, list):
        return []

    member_ids = [m.id for m in members]
    out: list[Insight] = []
    for item in raw_insights:
        if len(out) >= _MAX_INSIGHTS:
            break
        if not isinstance(item, dict):
            continue

        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if len(content) > max_chars:
            continue

        source_indices = item.get("source_indices")
        if not isinstance(source_indices, list) or not source_indices:
            continue
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in source_indices):
            continue
        if not all(0 <= i < len(member_ids) for i in source_indices):
            continue

        source_ids = [member_ids[i] for i in source_indices]

        # memory_type is FORCED to procedural regardless of what the model
        # returned — "reference" means no age decay at all (permanent rank
        # immunity), which an unreviewed auto-approved memory must never get.
        out.append(Insight(content=content, memory_type="procedural", source_ids=source_ids))

    return out[:_MAX_INSIGHTS]


async def synthesize(
    members: list[Candidate],
    *,
    settings: Any,
    max_chars: int,
    client: httpx.AsyncClient | None = None,
) -> list[Insight]:
    """Turn a cluster of episodes into 0-3 durable insights via one guarded LLM
    call. Returns [] on ANY failure — unreachable backend, non-2xx response, an
    unusable cluster (e.g. a corrupt Qdrant payload with non-string text), or an
    empty/invalid parse. Never raises.

    `settings` replaces the old base_url/model/api_key/timeout quartet: every
    one of those is now `app.llm.chat`'s business, and it reads them off the
    same object (endpoint selection needs `LLM_NATIVE_CHAT` and
    `LLM_NATIVE_BASE_URL` too, which a four-argument signature had no way to
    carry). `max_chars` stays an argument because it is not the LLM's — it is
    `build_messages`' length cue and `parse_insights`' rejection threshold.

    The whole body is wrapped in a single outer guard (mirrors
    app/knowledge/classifier.py's fail-loud-but-never-raise posture): request
    construction (build_messages) runs INSIDE the try, not before it, because a
    background orchestrator loops many clusters and one bad candidate must not
    kill the run. That guard now also has to hold for a wider set of exception
    types — `llm.chat` RAISES `httpx.HTTPStatusError` where this function used
    to call `raise_for_status()` itself, and can raise `AttributeError` on a
    settings object missing `LLM_MODEL` — but the arms below are `Exception`-
    shaped, not type-enumerated, so every one of them lands in a `return []`.

    Malformed model JSON gets exactly one retry (a second `llm.chat` call); any
    other failure (connection error, non-2xx, unexpected response shape)
    returns [] immediately without retrying. That distinction is deliberate:
    only the model's own output is worth asking twice for.

    NO `native_timeout`. `DREAM_SYNTH_TIMEOUT_SECONDS` (45.0) already
    accommodates the measured 22.5s native latency with room to spare, and a
    native sibling could only ever be LOWER — which is exactly what strands a
    non-thinking-model ollama deploy, since the probe confirms ollama, not a
    thinking model, and such a backend takes the native path while gaining
    nothing from `think:false` (the `decision/synthesize.py` reasoning, not the
    `knowledge/classifier.py` one).
    """
    try:
        messages = build_messages(members, max_chars=max_chars)
        timeout = float(getattr(settings, "DREAM_SYNTH_TIMEOUT_SECONDS", 45.0))

        text: str | None = None
        for attempt in range(2):
            try:
                result = await llm.chat(
                    settings=settings,
                    messages=messages,
                    json_mode=True,
                    # No `json_schema`. json_mode constrains syntax only, and a
                    # schema is a separate, measurable decision — the insight
                    # payload has a nested array shape that phase 3 never
                    # exercised against this prompt, and adding one unmeasured
                    # would be guessing in the one place this module has a
                    # history of guessing.
                    temperature=0.2,
                    max_tokens=_MAX_COMPLETION_TOKENS,
                    timeout=timeout,
                    client=client,
                    purpose="dream synthesis",
                )
                candidate = result.content

                # No reasoning-field fallback. It used to feed the `reasoning`
                # field to json.loads when content came back empty, which under
                # JSON mode cannot help BY CONSTRUCTION: empty content means the
                # grammar blocked the output, so `reasoning` is prose. Both
                # paths end in JSONDecodeError, so removing it changes no
                # outcome — it only deletes a line that read as a recovery
                # mechanism while never recovering anything. (Same deletion,
                # same argument, as knowledge/classifier.py in phase 1.)
                if not candidate.strip():
                    logger.warning(
                        "Dream synthesis returned empty content "
                        "(endpoint=%s, reasoning=%d chars)",
                        result.endpoint,
                        len(result.reasoning),
                    )

                # Validate the model actually returned parseable JSON before
                # committing to it — a malformed body gets one retry; a real
                # backend/transport failure does not.
                json.loads(candidate)
                text = candidate
                break
            except (json.JSONDecodeError, TypeError) as exc:
                if attempt == 0:
                    continue
                logger.warning("Dream synthesis returned malformed JSON after retry: %s", exc)
                return []
            except Exception as exc:
                # Type AND message. The live failure this conversion fixes
                # logged "Dream synthesis failed:" and nothing else — a bare
                # TimeoutError whose str() is empty — so the one line an
                # operator had said neither what went wrong nor where.
                logger.warning("Dream synthesis failed: %s: %s", type(exc).__name__, exc)
                return []

        if text is None:
            return []
        return parse_insights(text, members, max_chars=max_chars)
    except Exception as exc:
        # Catches anything the loop above doesn't — e.g. a corrupt candidate
        # (non-string .text) blowing up build_messages. Nothing may escape
        # synthesize().
        logger.warning("Dream synthesis failed: %s: %s", type(exc).__name__, exc)
        return []
