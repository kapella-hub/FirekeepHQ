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
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app.dreams import store
from app.dreams.synthesize import build_request_body

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
    base_url: str,
    model: str,
    api_key: str,
    timeout: float,
    max_chars: int,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Turn one member's memories into a single durable profile via one
    guarded LLM call. Returns None on ANY failure — no memories, unreachable
    backend, non-2xx response, or an empty/overlong parse. Never raises.

    Reuses synthesize.build_request_body so `think:false` (and
    chat_template_kwargs.enable_thinking) AND the completion budget
    (`synthesize._MAX_COMPLETION_TOKENS`) are inherited rather than re-derived
    — see synthesize.py's module docstring for why the flag is not optional
    (without it, qwen3 burns its whole budget thinking and returns empty
    content after 101s) and why the flag alone is not enough on ollama's `/v1`
    endpoint, which ignores it. A profile is far shorter than the budget
    allows; the budget exists to leave room for reasoning tokens that get
    generated whether or not this prompt wants them. The one field overridden
    afterward is `response_format`: insight extraction needs the JSON grammar,
    a profile is plain prose, and leaving json_object on would fight the
    prompt above.
    """
    if not memories:
        return None
    own_client = client is None
    http_client: httpx.AsyncClient | None = None
    try:
        http_client = client if client is not None else httpx.AsyncClient(timeout=timeout)
        messages = build_profile_messages(member_id, memories, max_chars=max_chars)
        body = build_request_body(model, messages)
        body["response_format"] = {"type": "text"}
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        url = f"{base_url}/chat/completions"

        resp = await http_client.post(url, json=body, headers=headers, timeout=timeout)
        resp.raise_for_status()

        msg = resp.json()["choices"][0]["message"]
        raw = msg.get("content") or msg.get("reasoning") or ""
        return parse_profile(raw, max_chars=max_chars)
    except Exception as exc:
        logger.warning("Profile synthesis failed: %s", exc)
        return None
    finally:
        if own_client and http_client is not None:
            await http_client.aclose()
