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

THIS CALL IS SCHEMA-CONSTRAINED (`_PROFILE_SCHEMA`, 2026-08-05). An earlier
revision of this docstring claimed the opposite — "unlike `synthesize()` this
call is NOT json-mode: a profile is plain prose, and the JSON grammar would
fight the prompt" — and that claim was false in letter once the schema landed
and had always been false in effect. THE ABSENCE OF A GRAMMAR IS WHAT BROKE IT.
A profile is prose, so the reasoning went, therefore constrain nothing; what
that actually bought was free-form generation with nothing to terminate it and
no constraint on shape, handed to a small model along with a template.

MEASURED on the production VPS 2026-08-05 (qwen3:4b, 4 vCPU, native /api/chat,
`DREAM_SYNTH_TIMEOUT_SECONDS` already raised to 90s):

    prompt size sweep, 20/12/10 memories -> prompts of 23,486 / 8,546 / 5,627 /
    3,815 chars: ALL FOUR failed identically at the 240s client timeout.

The FLAT result is the finding. A size-driven effect shows a gradient; this
showed none, so the prompt was never the variable.

    max_tokens sweep at a fixed prompt: 4000 -> FAILED at 200s.

4000 tokens of prose at the ~6.5 tok/s this box sustains is ~10 minutes, so
there was no budget under which that cap could terminate anything. And at
SMALLER caps the model did not return a profile at all — it returned the SYSTEM
PROMPT ECHOED BACK, then narrated its own procedure:

    " - what they consistently ask for
      - recurring corrections they have given
      Steps:
      1. Read each memory to extract relevant facts without speculation.
      Let's go through the memories:
      Memory [0]: ..."

That is the THIRD recorded instance of one failure in this codebase, not a new
one: qwen3:4b echoed `_DOC_LLM_PROMPT`'s template placeholders in LLM-endpoint
phase 1, and mirrored the user message's own shape back in phase 3's decision
board. A small model handed a template under no structural constraint
reproduces the template. `synthesize()` never hit any of this, and not by
design — `format:"json"` happens to constrain shape enough to stop the echo AND
to terminate generation, so the sibling call was protected by an accident of
being JSON-shaped.

The fix is the phase-3 mechanism applied here: a minimal schema
(`{"profile": "<prose>"}`), the prose extracted back out of it, and the stored
payload still prose. Phase 3 measured adherence going 0/3 -> 3/3 at no latency
cost, because a constrained decode emits fewer wasted tokens. See
`_PROFILE_SCHEMA`, `_extract_profile_text` and `_MAX_PROFILE_TOKENS` for the
three decisions that carries.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app import llm
from app.dreams import store

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 800
_MEMORY_TRUNCATE_CHARS = 600

# How many of a member's memories are SENT. 40 produced no usable profile.
#
# Measured on the production VPS 2026-08-05 (qwen3:4b, 4 vCPU, native /api/chat,
# schema-constrained, DREAM_SYNTH_TIMEOUT_SECONDS=90), one member with 498
# candidate memories:
#
#     memories  prompt chars   elapsed        result
#        40        23,621      52.0s / 83.4s  None, or a bare list of topics
#        20        12,178      90.1s          None (hit the budget)
#        12         8,162      28.9-34.8s     a real profile, 3 runs of 3
#        10         6,950      39.7s          a real profile
#
# The 40-memory failure is the interesting one, because it is NOT primarily
# latency: one run finished in 52s, well inside the budget, and still yielded
# nothing usable — the model extracted a list of artifacts ("client 0.1.13,
# <sha>, kiro-cli steering files, ...") instead of characterising a person. Too
# much context did not make the abstraction better, it replaced abstraction with
# enumeration. At 12 the same model reliably answers the four things the system
# prompt actually asks for, and the SAME recurring corrections surface across
# independent runs — reproducible signal rather than one lucky sample.
#
# Same finding as the cluster cap (DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS): on
# this hardware, fewer well-chosen inputs beat more inputs on BOTH latency and
# quality. Kept a plain module constant rather than a Setting because, unlike
# the cluster cap, nothing has yet been measured that would want it tuned per
# deployment; promote it if that changes.
#
# NOT changed here, and worth knowing: selection is still `memories[:N]` in
# whatever order the caller supplied. For a profile, most-RECENT would likely
# beat arbitrary-but-stable, since a profile should describe how someone works
# now. That is a second variable and it has not been measured; changing it in
# the same breath as the count would make neither attributable.
_MAX_MEMORIES = 12

# The one key the schema below defines, named once so the schema, the extractor
# and the system prompt cannot drift apart.
_PROFILE_KEY = "profile"

# The MINIMAL schema that does the job: one required string. It is not here to
# describe a profile — a profile is prose and there is nothing in it to model —
# it is here to do the two things the unconstrained call could not do at all:
# pin the SHAPE so the model cannot answer with the system prompt it was handed
# (measured above), and TERMINATE generation, which for a string means the
# closing quote rather than a token cap nobody can size.
#
# OpenAI strict mode is what `llm.build_openai_body` requests (`strict: True`),
# and it requires `properties` and `required` to name exactly the same keys and
# `additionalProperties: false` — the same three conditions `decision/
# synthesize.py::_suggestion_schema` satisfies and records a conformance
# argument for. This satisfies them trivially, having one key.
#
# NO `minLength`/`maxLength`. `parse_profile` already rejects empty and
# over-`max_chars` output and does it AFTER extraction, where the number means
# what `DREAM_MAX_INSIGHT_CHARS` says it means; expressing the same bound in the
# grammar would either truncate a profile mid-word into a valid-but-mutilated
# string (a grammar cannot decline, only stop) or, at the low end, pressure the
# model into padding a group it had little to say about. Phase 3 rejected
# `minItems` on the same argument, measured: no adherence gain, latency and
# invention as the cost.
_PROFILE_SCHEMA: dict = {
    "type": "object",
    "properties": {_PROFILE_KEY: {"type": "string"}},
    "required": [_PROFILE_KEY],
    "additionalProperties": False,
}

# Completion budget for one profile call. 4000, the same number synthesize.py
# uses, and for the same measured reason — NOT because it is tidy to share.
#
# A derivation from the ANSWER size is the intuitive move and it is wrong. It
# was tried at 512 (a valid profile is at most DREAM_MAX_INSIGHT_CHARS = 800
# chars, ~200-330 tokens, plus envelope) and that reasoning silently assumes
# every generated token lands in the answer. On ollama's `/v1` endpoint it does
# not: `think:false` is IGNORED there, so the model generates its full reasoning
# block FIRST and the budget is spent before the answer starts. This repo has
# already measured exactly that, and the number is in synthesize.py's own
# comment: at max_tokens=700 on `/v1`, three probes of three returned HTTP 200,
# finish_reason='length', completion_tokens=700, CONTENT LENGTH ZERO and ~3200
# chars of reasoning; the identical call at 4000 returned correct output.
#
# 512 is below 700. So an answer-sized cap does not merely reduce headroom on a
# `/v1`-routed thinking-model deploy — it returns None on every call, forever,
# which is strictly worse than the state before the schema landed. That path is
# live: any non-ollama backend, and any ollama demoted by a pre-generation 4xx.
#
# THE SCHEMA IS THE TERMINATOR, WHICH IS WHY A LARGE CAP IS FREE. A JSON string
# ends at its closing quote, so on the native path generation stops on its own
# and the cap is never approached; the measured failure it replaced was an
# UNCONSTRAINED call, where nothing stopped at 4000 because nothing was stopping
# it at all. The cap now only bounds a backend that ignored or was denied the
# schema, and on that backend it must be large enough to reach the answer.
#
# The cost of being wrong is asymmetric and that decides it. Too large: a
# runaway on the fallback rung burns up to DREAM_SYNTH_TIMEOUT_SECONDS, one
# tick, loudly, and the next tick retries the unit. Too small: silent, total,
# permanent None on a whole class of deployment. Prefer the loud failure.
_MAX_PROFILE_TOKENS = 4000

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


def _strip_code_fence(text: str) -> str:
    """Unwrap a ```-fenced block, if the whole response is one. Never raises.

    The `{`-prefix guard below refuses an unparseable JSON envelope instead of
    storing it verbatim as a profile. A fence defeats that guard by prefixing
    the envelope: ```` ```json\\n{"profile": "..." ```` does not START with `{`,
    so a truncated fenced envelope would fall through the prose branch and be
    stored — with its fence and its JSON syntax — at the member's deterministic
    point id, then served through every briefing. That is the precise failure
    the guard exists for, reached by a different door.

    Newly likely rather than hypothetical: the system prompt now ASKS for JSON,
    and its previous instruction not to use markdown fencing was removed with
    it, so a model on the schema-dropped rung (constrained in syntax only, or
    not at all) has both a reason to emit JSON and no instruction against
    fencing it.

    Only a whole-response fence is unwrapped. A fence in the MIDDLE of prose is
    left alone: that is a profile that happens to quote a code block, and
    mangling it would be a worse bug than the one being fixed.
    """
    if not text.startswith("```"):
        return text
    body = text[3:]
    newline = body.find("\n")
    if newline == -1:            # ```json with no body at all
        return text
    body = body[newline + 1:]
    close = body.rfind("```")
    return (body[:close] if close != -1 else body).strip()


def _extract_profile_text(raw: str) -> str | None:
    """The schema's envelope -> the prose inside it. Never raises.

    TWO RUNGS ARRIVE HERE, AND BOTH MUST WORK. `llm.chat` answers a
    pre-generation 4xx by retrying ONCE with the schema dropped, which is what
    keeps a vLLM/LiteLLM/OpenAI deploy that has not implemented structured
    outputs working at all. Handling only the schema-shaped reply would trade a
    broken feature on ollama for a broken feature everywhere else.

    Rung 1 (schema honoured): `{"profile": "<prose>"}` -> the prose.
    Rung 2 (schema dropped): whatever the model felt like. Note the retry is
    still json-MODE — `llm.chat` coerces `json_mode` true whenever a schema was
    requested, precisely so the fallback is constrained JSON rather than
    nothing — but json mode constrains SYNTAX ONLY, so rung 2 can be a
    correctly-keyed object, a differently-keyed object, or (on a backend that
    honours neither) bare prose.

    The rules, and why each is the way round it is:

      - Text that does not parse as JSON is PROSE and is returned as-is. Prose
        is never valid JSON, so this branch cannot swallow a JSON reply.
      - Text that OPENS with `{` and does not parse is a TRUNCATED OR MALFORMED
        ENVELOPE, and is refused rather than returned. This is the branch that
        matters: without it, a completion cut off at `_MAX_PROFILE_TOKENS`
        would be handed back as prose and stored as the literal string
        `{"profile": "Works on cortex...`, at the member's deterministic point
        id, and served through every briefing.
      - A parsed object carrying `profile` as a string yields it.
      - Anything else that PARSED is valid JSON of the wrong shape — the
        mirrored-input failure this schema exists to prevent, arriving from a
        backend that ignored or was denied it. Refused, not stringified: a
        profile that reads `{"summary": ...}` is not a profile.

    A bare JSON string (`"Works on cortex..."`) is accepted and unquoted; it is
    the same prose with one layer of encoding on it, and refusing it would only
    punish a backend for being tidy.
    """
    if not isinstance(raw, str):
        return None
    text = _strip_code_fence(raw.strip())
    if not text:
        return None

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        if text.lstrip().startswith("{"):
            logger.warning(
                "Dream profile response opened as a JSON object but did not "
                "parse (truncated or malformed, %d chars) — refusing rather "
                "than storing the envelope as prose: %.120s",
                len(text), text,
            )
            return None
        return text

    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        value = data.get(_PROFILE_KEY)
        if isinstance(value, str):
            return value
        logger.warning(
            "Dream profile response was a JSON object without a string %r key "
            "(keys=%s) — the schema was ignored or dropped",
            _PROFILE_KEY, sorted(map(str, data.keys()))[:10],
        )
        return None

    logger.warning(
        "Dream profile response parsed as %s, not an object or a string",
        type(data).__name__,
    )
    return None


def _system_prompt(max_chars: int) -> str:
    """ONE paragraph changed when the schema landed: the output-format
    instruction, which used to end "No markdown fencing, no JSON, no preamble".
    That was not merely stale, it CONTRADICTED the grammar the call now sends —
    the model was being told not to emit the only thing it was permitted to
    emit. Everything above it is byte-identical on purpose: the schema is the
    variable under test, and changing the task description in the same breath
    would make the next measurement unattributable. The `decision/synthesize.py`
    precedent is the shape followed here — the prompt DESCRIBES the contract,
    the schema PINS it, and a model that can read only one of them still has a
    coherent instruction."""
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
        "## Output Format\n"
        'Return ONLY a JSON object of the form {"' + _PROFILE_KEY + '": "<the '
        'profile>"}. The value is the profile itself, as plain prose — no '
        "markdown fencing, no preamble like 'Here is the profile', no other keys, "
        "and do not restate these instructions."
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
    else is returned stripped.

    EXTRACTION RUNS FIRST, INSIDE THIS FUNCTION, and that placement is the
    point: every guard below then operates on the PROSE rather than on the JSON
    envelope carrying it. Both of them are wrong otherwise.

    `max_chars` is `DREAM_MAX_INSIGHT_CHARS`, a bound on the profile — measuring
    the envelope instead would silently spend ~14 of the human's characters on
    punctuation and reject a profile that is exactly at budget.

    `_looks_like_refusal` windows to the OPENING of the text
    (`_REFUSAL_WINDOW_CHARS`), so a guard applied to the raw response is defeated
    by any envelope whose prose starts past that window. `{"profile": "` is only
    13 characters, so the schema-honouring rung would in fact still be caught —
    the case that is NOT is the schema-DROPPED rung, where the model is
    constrained in syntax only and is free to emit keys of its own before the one
    we asked for. That is the rung a non-ollama deploy lives on permanently, and
    the incident this guard exists for (a refusal stored at the member's
    deterministic point id and served through the briefing) is destructive
    rather than cosmetic, because a profile is replaced in place."""
    text = _extract_profile_text(raw)
    if text is None:
        return None
    text = text.strip()
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

    RETRY SEMANTICS ARE DELIBERATELY UNCHANGED — STILL EXACTLY ONE CALL, STILL
    NO RETRY — AND THAT WAS RE-DECIDED, NOT INHERITED. The response is JSON now,
    which is `synthesize()`'s stated reason for retrying once ("only the model's
    own output is worth asking twice for"), so the question is live. Four
    arguments against, in descending weight:

      1. The asymmetry that justified no-retry is about the CALLER, not the
         response format, and the schema did not touch it: a failed profile
         leaves the PREVIOUS profile in place, so not writing is a real and safe
         outcome. A cluster has no equivalent — a failed synthesis leaves
         nothing at all — which is the whole of why `synthesize()` retries.
      2. A retry is a second FULL generation on a `--pool=solo` worker. At the
         measured ~6.5 tok/s and a 90s budget, one retry can take the tick from
         a 90s stall to a ~3 minute stall, during which nothing else on the box
         runs, including the 60s agent-gateway sweeper. `synthesize()` accepts
         that cost because the alternative is losing the cluster; here the
         alternative is keeping a profile that already exists.
      3. What a retry could rescue is now rare and, where it isn't, is not
         intermittent. The schema makes a malformed reply ungrammatical on the
         path that honours it. On the schema-dropped rung a retry re-sends an
         identical unconstrained request to the same model — and every recorded
         instance of this failure in this codebase (phase 1's placeholder echo,
         phase 3's mirrored input, the prompt-echo measured above) was
         REPRODUCIBLE ACROSS RUNS, not flaky. Paying a second generation for the
         same wrong answer is the likely outcome, not the unlucky one.
      4. The unit is retried anyway, just not inside this call: the caller marks
         the group done for THIS run and a later tick picks it up.

    `settings` replaces the old base_url/model/api_key/timeout quartet, all four
    of which are now `llm.chat`'s business — endpoint selection additionally
    needs `LLM_NATIVE_CHAT`/`LLM_NATIVE_BASE_URL`, which a four-argument
    signature had no way to carry. `max_chars` stays an argument: it is the
    prompt's length cue and `parse_profile`'s rejection threshold, not the LLM's.

    SCHEMA-CONSTRAINED, and `json_mode` is left False on purpose: `llm.chat`
    coerces it true whenever a schema is present, so passing both would only
    duplicate a decision the callee already makes, while the False here records
    that this module wants THE SCHEMA — not json mode with a schema attached.
    What comes back is `{"profile": "<prose>"}` and `parse_profile` extracts the
    prose from it, so the STORED payload is prose exactly as before; the grammar
    is on the wire, not in the store.

    NO `native_timeout`, for `synthesize()`'s reason: 90s already clears the
    measured native latency, and a lower native sibling is what strands a
    non-thinking-model ollama deploy (the probe confirms ollama, not a thinking
    model).
    """
    if not memories:
        return None
    try:
        messages = build_profile_messages(member_id, memories, max_chars=max_chars)
        result = await llm.chat(
            settings=settings,
            messages=messages,
            json_mode=False,
            json_schema=_PROFILE_SCHEMA,
            json_schema_name="dream_profile",
            temperature=0.2,
            max_tokens=_MAX_PROFILE_TOKENS,
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

        # NO `msg.get("reasoning")` FALLBACK, and the schema makes the argument
        # STRONGER rather than weakening it. Elsewhere (classifier.py,
        # decision/synthesize.py) the case against it is that under a grammar an
        # empty content means the grammar blocked the output, so `reasoning` is
        # prose and `json.loads` will reject it anyway — useless, not harmful.
        # Here it would be actively harmful, because the caller of this value
        # ACCEPTS prose: `_extract_profile_text`'s rung-2 branch returns any
        # non-JSON text as-is. Feeding it a model's raw chain-of-thought would
        # store that at the member's deterministic point id and serve it through
        # every briefing, replacing the real profile — the same defect class as
        # the refusal incident `_looks_like_refusal` exists for, through a
        # different door. An empty content is a failed profile; the caller's
        # correct response is to leave the previous one alone.
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
