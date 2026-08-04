"""Decision-board homework: global recall → deterministic evidence + knowledge_found,
then a bounded best-effort LLM pass for suggested answers/actions (degrades on timeout).

The suggestion call goes through `app.llm.chat`, which selects ollama's native
`/api/chat` when the backend supports it. That is not a micro-optimisation on
this path — it is the difference between a feature that runs and one that
cannot. Ollama IGNORES `think:false` on `/v1/chat/completions`, so on a thinking
model this pass generated its entire reasoning block before emitting a single
character of JSON: 83.19s on a comparable call (app/llm.py, probe E) against a
budget that was 20s. See DECISION_SYNTH_TIMEOUT_SECONDS in config.py for the
budget's own reasoning, and app/llm.py for all five wire measurements.

Retrieval is deliberately sequenced BEFORE the LLM pass and is never inside its
try/except: evidence and knowledge_found are produced, and returned, whatever
the suggestion call does.

THE SUGGESTION CALL IS SCHEMA-CONSTRAINED (`_suggestion_schema`, 2026-08-04).
Fixing the endpoint and the budget was necessary and not sufficient: measured on
the deployed VPS afterwards, the board completed in 15.07s, reported
`degraded: False`, and returned `answers=0 actions=0` on every question, because
`format:"json"` constrains SYNTAX ONLY and qwen3:4b answered by mirroring the
user message's own shape back. Two things changed. The schema makes that answer
ungrammatical rather than merely discouraged; and `synthesize_board` now checks
that the payload grounded something, because a board that produced nothing must
never report itself healthy — the same shape as the `_llm_suggest` swallow, one
level down.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from app import llm
from app.models import ContextQuery

logger = logging.getLogger(__name__)

_SUGGEST_SYSTEM_PROMPT = (
    "You are the suggestion engine for a decision board. Given a context "
    "description and a list of open questions (each with any evidence "
    "snippets already found for it), propose concrete suggested answers and "
    "concrete suggested actions for each question.\n\n"
    "## Rules\n"
    "- Ground suggestions in the provided context and evidence snippets when available.\n"
    "- If a question has no evidence snippets, still propose reasonable "
    "suggestions from the context alone, but keep them general and clearly "
    "provisional.\n"
    "- Do NOT invent, restate, or cite evidence/sources — evidence is handled "
    "separately by the caller. Only return answers and actions.\n\n"
    "## Output Format\n"
    "Return ONLY a valid JSON object mapping each question id to an object "
    'with "suggested_answers" and "suggested_actions" (each a list of short '
    "strings). No markdown fencing, no explanation, no extra keys.\n\n"
    "Example:\n"
    "{\n"
    '  "q0": {"suggested_answers": ["Restart the ingest worker"], '
    '"suggested_actions": ["Run scripts/restart_worker.sh"]}\n'
    "}"
)


def _suggestion_schema(question_ids: list[str]) -> dict:
    """A JSON Schema pinning the contract the system prompt above only DESCRIBES.

    Measured 2026-08-04 on the VPS (ollama 0.32.4, qwen3:4b) with this exact
    prompt and three questions: under `format:"json"` the model answered 0/3 on
    both runs and instead echoed the USER message's own shape back — top-level
    keys `['context', 'questions']`, one run corrupting a key to
    `"evidence_sn:"`. Under this schema it answered 3/3 on both runs with
    top-level keys exactly `['q0','q1','q2']`. `json_mode` constrains SYNTAX;
    only a schema constrains SHAPE, and a small model handed a JSON input under
    a syntax-only constraint reproduces the input.

    Naming every question id in `properties` AND `required` is the load-bearing
    part — it is what makes the mirrored-input answer ungrammatical rather than
    merely discouraged. `additionalProperties: false` closes the same door from
    the other side and is also what OpenAI's `strict: true` requires.

    NO `minItems`. It was measured (24.51s vs 14.81–16.55s for the same 3/3
    result) and rejected: adherence was already total without it, so it buys
    latency plus pressure to invent a suggestion where the model has none.
    """
    def _string_list() -> dict:
        return {"type": "array", "items": {"type": "string"}}

    def _per_question() -> dict:
        # Freshly built per question rather than shared: these dicts are handed
        # to json serialisation, and an aliased sub-object is a trap waiting for
        # the first caller who mutates one.
        return {
            "type": "object",
            "properties": {
                "suggested_answers": _string_list(),
                "suggested_actions": _string_list(),
            },
            "required": ["suggested_answers", "suggested_actions"],
            "additionalProperties": False,
        }

    return {
        "type": "object",
        "properties": {qid: _per_question() for qid in question_ids},
        "required": list(question_ids),
        "additionalProperties": False,
    }


def _string_list(value) -> list[str]:
    """A suggestion field → a list of strings, or nothing.

    `[str(a) for a in (value or [])]` iterates whatever it is handed, and the
    two things a model most plausibly sends instead of a list are exactly the
    two that iterate into garbage: a BARE STRING yields one entry PER CHARACTER
    (`"Restart the worker"` → `['R','e','s','t',...]`, rendered to the human as
    eighteen suggestions), and a dict yields its keys. Neither raises, so the
    board reported `degraded=False` and showed the mess.

    This is the sibling of the `isinstance` guard on the question value one
    level up, and it matters most on the schema-DROPPED fallback rung, which is
    precisely where an unconstrained model is free to emit a bare string.

    A non-list is DISCARDED, not coerced. Wrapping a bare string into a
    one-element list would look kinder, but it invents structure the model did
    not produce, and there is no honest answer for the dict case. Discarding
    leaves the question ungrounded, which is a state this module already knows
    how to report: if every question ends up that way, the board degrades and
    says so.
    """
    if not isinstance(value, list):
        return []
    return [str(a) for a in value]


async def _recall_evidence(rag_engine, text: str) -> tuple[bool, list[dict]]:
    """Global recall (project=None, raw) → (knowledge_found, evidence[])."""
    resp = await rag_engine.recall(ContextQuery(task=text, project=None, format="raw"))
    evidence, has_vector = [], False
    for s in resp.sources:
        if s.store in ("vector", "both"):
            has_vector = True
        evidence.append({"source": s.store, "snippet": s.content, "ref": s.metadata})
    return has_vector, evidence


async def _llm_suggest(context: str, questions: list[dict], *, settings) -> dict:
    """One JSON-mode LLM call → {question_id: {suggested_answers, suggested_actions}}.
    Confined to suggestions; never emits evidence.

    RAISES on any failure — transport, HTTP status, unparseable JSON, or a
    non-object top level. It used to catch `Exception` and return `{}`, which
    made every non-timeout failure indistinguishable from a model that had
    nothing to suggest: `synthesize_board` saw an ordinary return and reported
    `degraded=False, note=""` on a board that had produced nothing. That was not
    a corner case. A connect error against a generation-less deploy (the office
    embed-only ollama image is a real, shipped configuration) took that path on
    every single call, as did any 4xx/5xx and any malformed completion. Only the
    `asyncio.wait_for` timeout escaped, and only by accident: it cancels, and
    `CancelledError` is a BaseException, so `except Exception` did not swallow
    it. Failure now propagates to the caller's handler, which is what sets
    `degraded=True`.
    """
    payload_questions = [
        {
            "id": q["id"],
            "text": q["text"],
            "evidence_snippets": [e["snippet"] for e in q.get("evidence", [])][:5],
        }
        for q in questions
    ]
    user_content = json.dumps({"context": context, "questions": payload_questions})

    # Built from the ids actually on this board, so the schema names `q0..qN`
    # for THIS call. Omitted entirely for a board with no questions: the schema
    # would then constrain output to the literal `{}`, and an empty
    # `properties`/`required` pair is also the one shape OpenAI's strict mode
    # has no use for. Plain json mode is the honest request for "nothing to ask".
    question_ids = [q["id"] for q in questions]
    schema = _suggestion_schema(question_ids) if question_ids else None

    result = await llm.chat(
        settings=settings,
        messages=[
            {"role": "system", "content": _SUGGEST_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        json_mode=True,
        json_schema=schema,
        json_schema_name="decision_suggestions",
        temperature=0.2,
        # ONE budget for both endpoints — no `native_timeout`, unlike
        # knowledge/classifier.py. A native sibling could only ever be LOWER
        # than this, and phase 1 measured that lowering the native budget
        # strands non-thinking-model deploys: the probe confirms ollama, not a
        # thinking model, so such a backend is routed down the native path and
        # gains nothing from `think:false` while losing headroom.
        timeout=float(getattr(settings, "DECISION_SYNTH_TIMEOUT_SECONDS", 30.0)),
        # NO `max_tokens`, deliberately — the one place this conversion differs
        # from skills/synthesizer.py, which gained one in phase 1. This call is
        # JSON-mode, so the grammar terminates generation by itself (a native
        # classify returns complete valid JSON in 4.00s). A cap therefore cannot
        # make a successful call shorter; it can only truncate a long one into
        # invalid JSON, converting a slow success into a guaranteed
        # JSONDecodeError. Skill drafting is free-form text with nothing to
        # close it — which is why phase 1 measured `done_reason=length` at every
        # cap there, and why a bound is right THERE and wrong here. Wall clock
        # is already bounded by DECISION_SYNTH_TIMEOUT_SECONDS, and on `/v1` a
        # cap is spent on reasoning tokens before the answer even starts, the
        # exact failure dreams/synthesize.py records from its 700 -> 4000 raise.
        purpose="decision suggestions",
    )

    # No reasoning-field fallback; it was deleted here for the same reason
    # knowledge/classifier.py's was. Under JSON mode, empty content means the
    # grammar blocked the output, so `reasoning` is prose ON EVERY BACKEND
    # MEASURED — phase-1 probe E returned 1978 chars of it — and json.loads
    # rejects prose. Stated that way deliberately: it is not proven universally.
    # A backend that mirrored the JSON into `reasoning` would have been rescued
    # by the old fallback, so this does trade an unobserved rescue for a visible
    # failure. That is the right way round, because the rescue path also
    # returned `degraded=False` — it could turn a broken call into a board that
    # claimed to be healthy, which is the defect this change exists to close.
    data = json.loads(result.content)
    if not isinstance(data, dict):
        raise ValueError(
            f"decision suggestions: expected a JSON object, got {type(data).__name__}"
        )
    return data


async def synthesize_board(context: str, draft_questions: list[str], *, rag_engine, settings) -> dict:
    try:
        ctx_found, ctx_ev = await _recall_evidence(rag_engine, context)
        cap = int(getattr(settings, "DECISION_MAX_QUESTIONS", 8))
        questions = []
        for i, qt in enumerate(draft_questions[:cap]):
            found, ev = await _recall_evidence(rag_engine, qt)
            questions.append({"id": f"q{i}", "text": qt, "knowledge_found": found,
                              "evidence": ev, "suggested_answers": [], "suggested_actions": []})
        # excess questions kept, un-recalled
        for j, qt in enumerate(draft_questions[cap:], start=cap):
            questions.append({"id": f"q{j}", "text": qt, "knowledge_found": ctx_found,
                              "evidence": [], "suggested_answers": [], "suggested_actions": []})
    except Exception as exc:
        logger.warning("decision retrieval failed, returning degraded board with no evidence: %s", exc)
        questions = [{"id": f"q{i}", "text": qt, "knowledge_found": False, "evidence": [],
                      "suggested_answers": [], "suggested_actions": []}
                     for i, qt in enumerate(draft_questions)]
        return {"questions": questions, "generated_at": datetime.now(timezone.utc).isoformat(),
                "degraded": True, "note": "retrieval-unavailable"}

    degraded = False
    note = ""
    try:
        # `wait_for` stays even though `llm.chat` is given the same budget:
        # httpx applies its timeout PER OPERATION (connect/write/read), so a
        # pathological backend can exceed it in total, and this endpoint has to
        # answer inside the client's fixed 45s ceiling. The two deadlines race
        # by roughly the connect time, but that race is no longer semantically
        # load-bearing — since _llm_suggest stopped swallowing, both arms land
        # here and set degraded=True.
        suggestions = await asyncio.wait_for(
            _llm_suggest(context, questions, settings=settings),
            timeout=float(getattr(settings, "DECISION_SYNTH_TIMEOUT_SECONDS", 30.0)))
        # grounding: keep only what maps to a real question id; suggestions never carry evidence
        #
        # Three counts, not one, because they are three different facts and the
        # log line has to be able to tell them apart:
        #   present  — the id is a key in the payload, whatever its value
        #   usable   — that value is a dict we can read fields off
        #   grounded — reading it produced at least one suggestion
        # Collapsing present into usable is what made an earlier version of the
        # warning below report "0/1 question ids present" while printing
        # `top-level keys=['q0']` in the same line.
        present = 0
        usable = 0
        grounded = 0
        for q in questions:
            if q["id"] in suggestions:
                present += 1
            s = suggestions.get(q["id"])
            # isinstance rather than `or {}`: a non-dict value (a list, a bare
            # string) would raise on `.get` HALFWAY through the loop, leaving
            # some questions assigned and the rest not. One malformed entry is
            # not a reason to abandon the others.
            if isinstance(s, dict):
                usable += 1
            else:
                s = {}
            q["suggested_answers"] = _string_list(s.get("suggested_answers"))
            q["suggested_actions"] = _string_list(s.get("suggested_actions"))
            if q["suggested_answers"] or q["suggested_actions"]:
                grounded += 1

        # A successful call that grounded NOTHING is not a healthy board.
        #
        # This is the same defect one level down from the one the `_llm_suggest`
        # rewrite closed: there, any exception reported `degraded=False`; here,
        # a 200 carrying a structurally unusable payload did. It is not
        # hypothetical — it is what the VPS served in production. `format:"json"`
        # made qwen3:4b mirror the user message back, so `suggestions` was a
        # well-formed dict keyed `['context','questions']`, `.get("q0")` missed
        # on every question, nothing raised, and the endpoint reported a healthy
        # board with `answers=0 actions=0` on all of them. The schema above is
        # the fix; this is the detector that would have named it in an hour
        # instead of three phases, and still catches any backend that ignores
        # or is denied the schema.
        #
        # The two notes are distinguished because they need different responses:
        # `unusable` means the model answered a different question than the one
        # asked (a prompt/schema/backend problem), `empty` means it answered
        # this one with nothing (a retrieval or model-capability problem).
        # The split keys off `usable`, not `present`: an id whose value is a
        # bare list or string IS present, but the model still did not answer the
        # question in the required shape, which is `unusable`.
        if questions and not grounded:
            degraded = True
            note = "suggestions-empty" if usable else "suggestions-unusable"
            logger.warning(
                "decision suggestion pass returned a payload that grounded "
                "nothing (%s): of %d question(s), %d id(s) present and %d "
                "usable; top-level keys=%s",
                note, len(questions), present, usable,
                sorted(suggestions.keys())[:10])
    except Exception as exc:
        # The type name matters: a bare wait_for TimeoutError stringifies to "",
        # so "%s" alone would log a reason of nothing at all.
        logger.warning(
            "decision suggestion LLM pass failed — retrieval-only board: %s: %s",
            type(exc).__name__, exc)
        degraded = True
        note = "retrieval-only"

    return {"questions": questions, "generated_at": datetime.now(timezone.utc).isoformat(),
            "degraded": degraded, "note": note}
