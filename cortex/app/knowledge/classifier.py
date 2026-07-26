"""Document classifier for the docs->skills ingestion pipeline.

Classifies a whole document as reference / procedural / mixed and, for
procedural content, extracts the titles of each distinct self-contained
procedure/runbook so they can be queued for per-procedure skill drafting
(see cortex/app/skills/synthesizer.py's synthesize_from_document, Task 4).

JSON-mode call pattern mirrors app/workers/sleep_cycle.py's LLM extraction
call (~line 362) verbatim: same headers/timeout/content-or-reasoning
fallback logic, ported to httpx.AsyncClient since this runs in the async
request path (POST /knowledge/ingest, Task 6) rather than a Celery task.

Fail-loud posture (matches sleep_cycle, NOT memory_agent's silent
fallback): any failure anywhere in the call/parse/validate chain returns
the fixed fallback dict below. classify_document never raises.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_VALID_PRIMARY_TYPES = {"reference", "procedural", "mixed"}

_FAIL_LOUD_NOTE = (
    "classification failed — the document is in the corpus and searchable via "
    "memory_recall; no skills were drafted"
)


_UNAVAILABLE_NOTE = (
    "generation backend unavailable — the document is in the corpus and "
    "searchable via memory_recall; classification/skill-drafting will run "
    "automatically once a generation model is deployed"
)


def _fail_note(exc: Exception) -> str:
    """Fail-loud note with the concrete reason (bounded) so the dashboard says
    WHY — e.g. a generation-less deploy (embed-only ollama image) surfaces as a
    ConnectError here rather than a bare 'classification failed'. Exception
    reprs on this path (ConnectError/HTTPStatusError/JSONDecodeError) do not
    carry the API key (headers are never in the message)."""
    reason = f"{type(exc).__name__}: {exc}"
    return f"{_FAIL_LOUD_NOTE} (reason: {reason[:200]})"


def _is_backend_unavailable(exc: Exception) -> bool:
    """True when classification failed because the GENERATION backend is
    absent/unreachable (an embed-only deploy has no chat model) rather than a
    genuine classify error. Lets the ingest degrade to a clean 'corpus_only'
    status instead of a scary 'failed', and auto-revert once generation returns.
    Covers: connection/timeout errors (backend down) and HTTP 404 / 'model not
    found' (ollama's response for a chat model it doesn't have)."""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException)):
        return True
    if (isinstance(exc, httpx.HTTPStatusError) and exc.response is not None
            and exc.response.status_code == 404):
        return True
    msg = str(exc).lower()
    return "model" in msg and ("not found" in msg or "does not exist" in msg
                               or "not exist" in msg)

CLASSIFY_SYSTEM_PROMPT = (
    "You are the document classifier for a knowledge ingestion pipeline. "
    "Given the full text of a document, classify it and extract procedure titles.\n\n"
    "## Classification\n"
    "Classify the WHOLE document as exactly one of:\n"
    '- "reference": general knowledge, background, or facts with no actionable steps.\n'
    '- "procedural": the document is one or more step-by-step procedures or runbooks.\n'
    '- "mixed": the document contains both reference material and procedures.\n\n'
    "## Procedure titles\n"
    "List the title of each DISTINCT, self-contained procedure or runbook in the "
    "document — a procedure is a sequence of steps that accomplishes one task "
    "end-to-end. If the document is pure reference material with no procedures, "
    "return an empty list.\n\n"
    "## Output Format\n"
    "Return ONLY a valid JSON object. No markdown fencing, no explanation.\n\n"
    "Example:\n"
    "{\n"
    '  "primary_type": "procedural",\n'
    '  "procedure_titles": ["Restart the ingest worker", "Rotate the API key"]\n'
    "}"
)


async def classify_document(content: str, *, settings: Any) -> dict:
    """Classify a document's primary type and extract procedure titles.

    Returns:
        {"primary_type": "reference"|"procedural"|"mixed",
         "procedure_titles": [str, ...], "ok": bool, "note": str}

    Never raises — any failure (LLM unreachable, non-2xx response, malformed
    JSON, unexpected shape) is caught and converted to the fail-loud
    fallback so the caller can proceed with corpus-only ingestion.
    """
    try:
        headers = {"Authorization": f"Bearer {settings.LLM_API_KEY}"} if settings.LLM_API_KEY else {}
        timeout = getattr(settings, "KNOWLEDGE_CLASSIFY_TIMEOUT_SECONDS", 300.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{settings.LLM_BASE_URL}/chat/completions",
                json={
                    "model": settings.LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                        {"role": "user", "content": content},
                    ],
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                },
                headers=headers,
            )
            resp.raise_for_status()

        msg = resp.json()["choices"][0]["message"]
        text = msg.get("content") or ""
        # Fallback: some models (e.g. qwen3) put output in a reasoning field
        # (verbatim from sleep_cycle.py's .strip()-gated fallback).
        if not text.strip():
            text = msg.get("reasoning") or ""
        data = json.loads(text)

        primary_type = data.get("primary_type")
        if primary_type not in _VALID_PRIMARY_TYPES:
            primary_type = "mixed"

        raw_titles = data.get("procedure_titles", [])
        if not isinstance(raw_titles, list):
            raw_titles = []

        titles: list[str] = []
        dropped = 0
        for title in raw_titles:
            if isinstance(title, str) and title.strip():
                titles.append(title.strip())
            else:
                dropped += 1

        max_procedures = getattr(settings, "KNOWLEDGE_MAX_PROCEDURES", 10)
        capped = len(titles) > max_procedures
        if capped:
            titles = titles[:max_procedures]

        note_parts = []
        if dropped:
            note_parts.append(f"dropped {dropped} invalid procedure title(s)")
        if capped:
            note_parts.append(f"capped at {max_procedures} procedures")

        return {
            "primary_type": primary_type,
            "procedure_titles": titles,
            "ok": True,
            "note": "; ".join(note_parts),
        }
    except Exception as exc:
        unavailable = _is_backend_unavailable(exc)
        if unavailable:
            logger.info("Document classification skipped — generation backend unavailable: %s", exc)
        else:
            logger.error("Document classification failed: %s", exc)
        return {
            "primary_type": "reference",
            "procedure_titles": [],
            "ok": False,
            "unavailable": unavailable,
            "note": _UNAVAILABLE_NOTE if unavailable else _fail_note(exc),
        }
