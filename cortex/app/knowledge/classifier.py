"""Document classifier for the docs->skills ingestion pipeline.

Classifies a whole document as reference / procedural / mixed and, for
procedural content, extracts the titles of each distinct self-contained
procedure/runbook so they can be queued for per-procedure skill drafting
(see cortex/app/skills/synthesizer.py's synthesize_from_document, Task 4).

The LLM call goes through `app.llm.chat`, which selects ollama's native
`/api/chat` when the backend supports it (measured 4.00s) over
`/v1/chat/completions` (83.19s for the same document, because ollama ignores
`think:false` there and generates the full reasoning anyway). See app/llm.py.

Fail-loud posture (matches sleep_cycle, NOT memory_agent's silent
fallback): any failure anywhere in the call/parse/validate chain returns
the fixed fallback dict below. classify_document never raises.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app import llm

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
    if _is_read_timeout(exc):
        # httpx.ReadTimeout usually stringifies to "", so the generic
        # "(reason: ReadTimeout: )" says nothing actionable. Name the budget
        # that was exceeded instead — this is now a reachable state, since a
        # read timeout no longer masquerades as 'backend unavailable'.
        return (
            f"{_FAIL_LOUD_NOTE} (reason: the generation backend accepted the "
            "request but did not answer within KNOWLEDGE_CLASSIFY_TIMEOUT_SECONDS "
            "— it is reachable but too slow, not absent)"
        )
    reason = f"{type(exc).__name__}: {exc}"
    return f"{_FAIL_LOUD_NOTE} (reason: {reason[:200]})"


def _is_read_timeout(exc: Exception) -> bool:
    """A timeout waiting for a RESPONSE from a backend that accepted the
    connection — i.e. deployed, reachable, and merely slow. Deliberately
    excludes ConnectTimeout, which subclasses TimeoutException but means the
    backend never answered at all."""
    return isinstance(exc, httpx.TimeoutException) and not isinstance(exc, httpx.ConnectTimeout)


def _is_backend_unavailable(exc: Exception) -> bool:
    """True when classification failed because the GENERATION backend is
    absent/unreachable (an embed-only deploy has no chat model) rather than a
    genuine classify error. Lets the ingest degrade to a clean 'corpus_only'
    status instead of a scary 'failed', and auto-revert once generation returns.
    Covers: connection errors (backend down) and HTTP 404 / 'model not found'
    (ollama's response for a chat model it doesn't have).

    A READ TIMEOUT IS DELIBERATELY NOT IN THIS SET, and used to be. The tuple
    named bare `httpx.TimeoutException`, which `httpx.ReadTimeout` subclasses —
    so a classify that ran out its budget against a working, answering backend
    was recorded terminal `corpus_only` with a note claiming the generation
    backend was unavailable and that classification "will run automatically once
    a generation model is deployed". On the VPS the model IS deployed and
    answering; the status was simply false, and nothing ever re-enqueues a
    `corpus_only` source, so the document stayed silently corpus-only forever.
    That was not a rare corner: the pre-fix /v1 classify measured 288.9s against
    a 300.0s budget. `ConnectTimeout` must stay named explicitly — it subclasses
    TimeoutException too, but it means nothing answered, which IS unavailable.
    """
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
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
        result = await llm.chat(
            settings=settings,
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            json_mode=True,
            temperature=0.1,
            timeout=getattr(settings, "KNOWLEDGE_CLASSIFY_TIMEOUT_SECONDS", 300.0),
            native_timeout=getattr(settings, "KNOWLEDGE_CLASSIFY_NATIVE_TIMEOUT_SECONDS", None),
            purpose="knowledge classify",
        )

        # No reasoning-field fallback. It used to feed msg["reasoning"] to
        # json.loads when content was empty, which under JSON mode cannot help
        # BY CONSTRUCTION: if content is empty the grammar blocked it, so
        # `reasoning` is prose, not JSON (the measured /v1 call returned 4357
        # chars of it). Both paths end in JSONDecodeError, so dropping it
        # changes no terminal state — it only removes a line that read as a
        # recovery mechanism while never recovering anything.
        data = json.loads(result.content)

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
