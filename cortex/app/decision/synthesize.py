"""Decision-board homework: global recall → deterministic evidence + knowledge_found,
then a bounded best-effort LLM pass for suggested answers/actions (degrades on timeout)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

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
    Confined to suggestions; never emits evidence. Mirrors knowledge/classifier.py's
    httpx JSON-mode call pattern (headers, content-or-reasoning fallback).

    Returns {} on ANY failure so the outer asyncio.wait_for/except in
    synthesize_board degrades cleanly to a retrieval-only board.
    """
    try:
        api_key = getattr(settings, "LLM_API_KEY", "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        timeout = getattr(settings, "DECISION_SYNTH_TIMEOUT_SECONDS", 20.0)

        payload_questions = [
            {
                "id": q["id"],
                "text": q["text"],
                "evidence_snippets": [e["snippet"] for e in q.get("evidence", [])][:5],
            }
            for q in questions
        ]
        user_content = json.dumps({"context": context, "questions": payload_questions})

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{settings.LLM_BASE_URL}/chat/completions",
                json={
                    "model": settings.LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": _SUGGEST_SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                },
                headers=headers,
            )
            resp.raise_for_status()

        msg = resp.json()["choices"][0]["message"]
        text = msg.get("content") or ""
        # Fallback: some models (e.g. qwen3) put output in a reasoning field
        # (verbatim from classifier.py / sleep_cycle.py's .strip()-gated fallback).
        if not text.strip():
            text = msg.get("reasoning") or ""
        data = json.loads(text)

        if not isinstance(data, dict):
            return {}
        return data
    except Exception as exc:
        logger.warning("decision suggestion LLM call failed, returning empty: %s", exc)
        return {}


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
        suggestions = await asyncio.wait_for(
            _llm_suggest(context, questions, settings=settings),
            timeout=float(getattr(settings, "DECISION_SYNTH_TIMEOUT_SECONDS", 20.0)))
        # grounding: keep only what maps to a real question id; suggestions never carry evidence
        for q in questions:
            s = suggestions.get(q["id"]) or {}
            q["suggested_answers"] = [str(a) for a in (s.get("suggested_answers") or [])]
            q["suggested_actions"] = [str(a) for a in (s.get("suggested_actions") or [])]
    except Exception:
        logger.warning("decision suggestion LLM pass degraded (timeout/error) — retrieval-only board")
        degraded = True

    return {"questions": questions, "generated_at": datetime.now(timezone.utc).isoformat(),
            "degraded": degraded, "note": "retrieval-only" if degraded else ""}
