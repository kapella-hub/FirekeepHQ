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

    result = await llm.chat(
        settings=settings,
        messages=[
            {"role": "system", "content": _SUGGEST_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        json_mode=True,
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
    # grammar blocked the output, so `reasoning` is prose by construction and
    # can never be the JSON. Both paths ended in JSONDecodeError — the fallback
    # only read like a rescue.
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
        for q in questions:
            s = suggestions.get(q["id"]) or {}
            q["suggested_answers"] = [str(a) for a in (s.get("suggested_answers") or [])]
            q["suggested_actions"] = [str(a) for a in (s.get("suggested_actions") or [])]
    except Exception as exc:
        # The type name matters: a bare wait_for TimeoutError stringifies to "",
        # so "%s" alone would log a reason of nothing at all.
        logger.warning(
            "decision suggestion LLM pass failed — retrieval-only board: %s: %s",
            type(exc).__name__, exc)
        degraded = True

    return {"questions": questions, "generated_at": datetime.now(timezone.utc).isoformat(),
            "degraded": degraded, "note": "retrieval-only" if degraded else ""}
