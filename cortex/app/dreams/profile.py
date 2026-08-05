"""Person profiles: one continuously-updated memory per human, keyed by
`member_id` — never `agent_id`. Ground truth, measured on the live store: one
human appeared under SEVEN distinct agent_id values — a hook-minted
`agent-<host>-<hash>`, a directory-style `<Surname, Forename>`, a bare
`<first-name>`, an OS `<username>`, plus `unknown`, `default` and
`legacy-pre-team-continuity` — while member_id was uniform across all 538
active memories. Keying on agent_id would build seven partial profiles of the
same person. (The real values are a named individual's identifiers and are
deliberately not reproduced in shipped source; the shape is what the design
turns on, not the strings.)

Written through store.profile_point_id + VectorClient.upsert_point — the same
dedicated write path dream insights use, for the same three reasons documented
in store.py's module docstring. That is what makes a profile "continuously
updated" rather than accumulating: re-profiling a member always resolves to
the same deterministic point id, so the write replaces it in place.

The payload sets memory_type="reference" — the one deliberate exception to the
"never reference" rule elsewhere in dreams (reference means no age decay at
all). A profile must not decay, because it is replaced wholesale on every run,
not accumulated; select.is_candidate's memory_type check (episodic-or-missing
only) is what keeps a profile out of its own future clustering input, so a
profile can never dream about itself.

THE LLM CALL GOES THROUGH `app.llm.chat`, which selects ollama's native
`/api/chat` when the backend confirms as ollama and falls back to
`/v1/chat/completions` otherwise. That is not tidiness; it is the difference
between this half of Dreaming working on a CPU deploy and not existing at all,
and it is the same fix `synthesize.py` took one commit earlier.

WHY, measured rather than reasoned. Ollama honours `think:false` on its native
`/api/chat` and silently IGNORES it — along with
`chat_template_kwargs.enable_thinking` — on `/v1/chat/completions`. This module
posted to `{LLM_BASE_URL}/chat/completions`, and `LLM_BASE_URL` ends in `/v1`,
so it sent both spellings of a flag the endpoint threw away and paid the full
reasoning cost on every call. On the production VPS (qwen3:4b, 4 vCPU) the
sibling cluster call measured >400s on `/v1` WITHOUT COMPLETING against the
same 45s `DREAM_SYNTH_TIMEOUT_SECONDS` this call is bounded by, versus 22.5s
native. A profile prompt is a different prompt, so that number is not this
call's latency — but the reasoning block it has to generate first is the same
block, produced by the same model on the same endpoint under the same budget,
and the budget is nine times too small for it. Profiles are the user-facing
half of Dreaming (`GET /briefing`'s profile section reads them), so this was
the half where the failure was most visible and least explicable.

Unlike `synthesize()` this call is NOT json-mode: a profile is plain prose, and
the JSON grammar would fight the prompt. See `synthesize_profile` for what that
changes and what it does not.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app import llm
from app.dreams import store

# One number for both dreams LLM calls. It used to reach this module inside
# `synthesize.build_request_body`, the shared `/v1` body builder; that function
# is gone (nothing built a body by hand any more once both calls went through
# `llm.chat`), so the constant is imported directly instead. Deliberately not
# copied: a second 4000 next door is a thing that drifts, and synthesize.py is
# where the measurement that justifies the number is written down. It is
# private-by-name and shared-by-intent within one package — see its comment
# there before changing it.
from app.dreams.synthesize import _MAX_COMPLETION_TOKENS

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 800
_MEMORY_TRUNCATE_CHARS = 600
_MAX_MEMORIES = 40

# A refusal is not a profile. Observed on a live run, stored at the member's
# deterministic point id and then SERVED through the briefing's profile
# section: "No human is mentioned in the memories. The text describes system
# behavior...". parse_profile rejected only empty and overlong text, so an LLM
# that declined the task produced a perfectly valid-looking profile point —
# and because a profile is REPLACED IN PLACE, that non-answer overwrote
# whatever real profile was there.
#
# This is a HEURISTIC and is meant to read as one. Two deliberate choices keep
# it from doing more harm than the bug:
#
#   - It matches only within the first _REFUSAL_WINDOW_CHARS. A real profile
#     may legitimately contain "there is no evidence that ..." mid-body;
#     rejecting that would be worse than the defect. A refusal lives in the
#     opening clause, so that is the only place worth looking.
#   - The patterns are specific verb phrases, not bare negations, for the same
#     reason.
#
# The asymmetry it trades on: a false REJECT costs one skipped refresh (the
# group is marked done for this run and picked up on a later one, with the
# previous profile left intact); a false ACCEPT overwrites a good profile with
# a non-answer that then gets injected into every briefing. Rejecting is the
# cheap direction.
#
# Explicitly NOT a guarantee — a differently-worded refusal still gets through.
# What was rejected as too fragile: requiring the profile to mention the member
# id. `member_id` is an opaque `member-<hex>` handle a profile has no reason to
# quote, so that check would reject almost every VALID profile.
_REFUSAL_WINDOW_CHARS = 200
_REFUSAL_PATTERNS = (
    "no human",
    "no person",
    "no individual",
    "no one is mentioned",
    "does not mention",
    "do not mention",
    "don't mention",
    "there is no ",
    "there are no ",
    "cannot build",
    "cannot create",
    "cannot produce",
    "cannot generate",
    "can't build",
    "can't create",
    "can't produce",
    "can't generate",
    "unable to build",
    "unable to create",
    "unable to produce",
    "unable to generate",
    "i'm sorry",
    "i am sorry",
    "insufficient information",
    "not enough information",
    "no information about",
)


def _looks_like_refusal(text: str) -> bool:
    """True when the OPENING of `text` reads as the model declining the task
    rather than answering it. See the _REFUSAL_PATTERNS comment above for why
    this is windowed, why it is a heuristic, and what was rejected as too
    fragile."""
    head = text[:_REFUSAL_WINDOW_CHARS].lower()
    return any(pattern in head for pattern in _REFUSAL_PATTERNS)


def _system_prompt(max_chars: int) -> str:
    return (
        "You are the Dreaming pass for a long-term agent memory store, building a "
        "PERSON PROFILE for one human from memories that mention them. Produce a "
        "compact, factual profile covering: how this person works, what they "
        "consistently ask for, recurring corrections they have given, and domains "
        "or projects they own.\n\n"
        "Rules:\n"
        "- Use ONLY what the memories below actually support. No speculation, no "
        "invented facts, no generic filler that isn't grounded in a specific memory.\n"
        "- If the memories don't support one of the categories above, omit it "
        "rather than guessing.\n"
        f"- Keep the whole profile under {max_chars} characters.\n\n"
        "Return ONLY the profile text as plain prose. No markdown fencing, no "
        "JSON, no preamble like 'Here is the profile'."
    )


def build_profile_messages(
    member_id: str, memories: list[dict], *, max_chars: int = _DEFAULT_MAX_CHARS
) -> list[dict]:
    lines = []
    for i, m in enumerate(memories[:_MAX_MEMORIES]):
        text = str((m or {}).get("text", ""))[:_MEMORY_TRUNCATE_CHARS]
        lines.append(f"[{i}] {text}")
    user_content = "\n".join(lines) if lines else "(no memories on record)"
    return [
        {"role": "system", "content": _system_prompt(max_chars)},
        {"role": "user", "content": f"Member: {member_id}\n\n{user_content}"},
    ]


def parse_profile(raw: str, *, max_chars: int) -> str | None:
    """Validate a raw LLM response as profile text. Never raises. Rejects
    empty/whitespace-only output, over-budget output, and output whose opening
    reads as a refusal rather than a profile (see _REFUSAL_PATTERNS); anything
    else is returned stripped."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if len(text) > max_chars:
        return None
    if _looks_like_refusal(text):
        logger.warning("Dream profile rejected as an LLM refusal: %.120s", text)
        return None
    return text


def build_profile_payload(
    text: str, *, member_id: str, workspace_id: str, run_id: str,
    namespace: str = "default", project: str | None = None,
) -> dict:
    """`namespace`/`project` default to the pre-fix-round hardcoded values
    (backward compatible for any existing caller that doesn't pass them), but
    task.py's real call site now derives both from the (post-C1-fix)
    homogeneous candidate group a profile was built from — see the module
    docstring's tenancy note. A profile stamped project=None when its source
    memories actually carried a project was INVISIBLE to project-scoped
    recall, since `project` is a hard `must` filter in VectorClient.search;
    with ~45% of live active memories carrying a project, this was a
    functional bug, not a future nicety (fix-round review I2)."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "text": text,
        "source": "dream_profile",
        "dream_run_id": run_id,
        # reference is deliberate here (see module docstring): a profile is
        # replaced in place, never accumulated, so it must not age-decay.
        "memory_type": "reference",
        "status": "active",
        "confirmed_count": 0,
        "contradicted_count": 0,
        "superseded_by": None,
        "timestamp": now,
        "created_at": now,
        "workspace_id": workspace_id,
        "namespace": namespace,
        "project": project,
        "member_id": member_id,
        "agent_id": "dream",
        "session_id": None,
        "domain": "general",
        "tags": ["dream", "profile"],
        # Recall reads memory_type from the projection, GC from top-level —
        # write both so they can never disagree about this point (same
        # precedent as store.build_dream_payload).
        "metadata": {"memory_type": "reference", "profile_member_id": member_id},
    }


async def write_profile(
    vector, text: str, *, member_id: str, workspace_id: str, run_id: str,
    namespace: str = "default", project: str | None = None,
) -> str:
    payload = build_profile_payload(
        text, member_id=member_id, workspace_id=workspace_id, run_id=run_id,
        namespace=namespace, project=project,
    )
    point_id = store.profile_point_id(member_id, workspace_id)
    await vector.upsert_point(point_id, text, payload)
    return point_id


async def synthesize_profile(
    member_id: str,
    memories: list[dict],
    *,
    settings: Any,
    max_chars: int,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Turn one member's memories into a single durable profile via one
    guarded LLM call. Returns None on ANY failure — no memories, unreachable
    backend, non-2xx response, or an empty/refusing/overlong parse.

    NEVER RAISES, and that guarantee had to be RE-VERIFIED rather than assumed
    when this moved onto `llm.chat`: `llm.chat` RAISES by contract, including
    `httpx.HTTPStatusError` (this function used to call `raise_for_status()`
    itself, so the type is not new to the process but is new to this guard) and
    `AttributeError` from a settings object with no `LLM_MODEL`. The guard is
    `Exception`-shaped rather than type-enumerated, so every one of them lands
    in the same `return None`.

    RETRY SEMANTICS ARE UNCHANGED, AND THEY ARE NOT `synthesize()`'S. There is
    exactly ONE call and NO retry — any failure, including a malformed or
    refusing response, returns None immediately. That is deliberate and was
    deliberate before this conversion: the failure is absorbed by the caller,
    which marks the group done and leaves the PREVIOUS profile in place (a
    profile is replaced in place, so not writing is a real, safe outcome — a
    cluster has no such fallback, which is why `synthesize()` retries once on
    malformed JSON and this does not).

    `settings` replaces the old base_url/model/api_key/timeout quartet, all four
    of which are now `llm.chat`'s business — endpoint selection additionally
    needs `LLM_NATIVE_CHAT`/`LLM_NATIVE_BASE_URL`, which a four-argument
    signature had no way to carry. `max_chars` stays an argument: it is the
    prompt's length cue and `parse_profile`'s rejection threshold, not the LLM's.

    NOT json-mode — the ONE place this deviates from `synthesize()`. The old
    code built the shared JSON body and then overrode `response_format` to
    `{"type": "text"}`, i.e. it deliberately turned the grammar OFF, because a
    profile is plain prose and JSON mode would fight the system prompt above.
    `json_mode=False` reproduces that exactly: `build_openai_body` emits no
    `response_format` at all (OpenAI's default is `{"type": "text"}`) and
    `build_native_body` emits no `format`. Passing `json_mode=True` here would
    not be a tidy-up, it would break every profile.

    NO `native_timeout` and NO `json_schema`, for `synthesize()`'s reasons: 45s
    already clears the measured native latency and a lower native sibling is
    what strands a non-thinking-model ollama deploy (the probe confirms ollama,
    not a thinking model); and there is no JSON here for a schema to constrain.
    """
    if not memories:
        return None
    try:
        messages = build_profile_messages(member_id, memories, max_chars=max_chars)
        result = await llm.chat(
            settings=settings,
            messages=messages,
            json_mode=False,
            temperature=0.2,
            max_tokens=_MAX_COMPLETION_TOKENS,
            # Read off `settings` now that the caller passes no timeout. The
            # literal is a stub-only fallback — production always passes a real
            # Settings — and it is pinned to config.py's default by the drift
            # guard in tests/test_dreams_synthesize.py, which asserts
            # `Settings.model_fields["DREAM_SYNTH_TIMEOUT_SECONDS"].default`
            # directly so a config change fails the suite rather than silently
            # diverging from this copy.
            timeout=float(getattr(settings, "DREAM_SYNTH_TIMEOUT_SECONDS", 90.0)),
            client=client,
            purpose="dream profile synthesis",
        )

        # NO `msg.get("reasoning")` FALLBACK. It used to read the reasoning
        # field when content came back empty, and here — unlike in JSON mode,
        # where the argument is that an empty content means the grammar blocked
        # it so the reasoning is prose by construction — the reasoning IS prose,
        # which makes the fallback worse rather than merely useless: it would
        # store a model's raw chain-of-thought at the member's deterministic
        # point id and serve it through every briefing, replacing the real
        # profile. That is the same defect class as the refusal incident
        # `_looks_like_refusal` exists for, arriving through a different door.
        # An empty content is a failed profile; the caller's correct response is
        # to leave the previous one alone.
        if not result.content.strip():
            logger.warning(
                "Dream profile synthesis returned empty content for member %s "
                "(endpoint=%s, reasoning=%d chars)",
                member_id, result.endpoint, len(result.reasoning),
            )
        return parse_profile(result.content, max_chars=max_chars)
    except Exception as exc:
        # Type AND message: the failure this conversion fixes is a bare
        # TimeoutError whose str() is empty, which logged as
        # "Profile synthesis failed: " and told an operator nothing.
        logger.warning("Profile synthesis failed: %s: %s", type(exc).__name__, exc)
        return None
