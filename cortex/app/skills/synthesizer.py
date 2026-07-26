"""LLM-based skill synthesis from session replay + shadow."""
from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

import httpx
import redis.asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct

from app.skills import internal_key_headers
from app.skills.scorer import SkillScore

logger = logging.getLogger(__name__)

# Deterministic-id namespace for document-sourced skill drafts. Fixed value
# Re-ingesting the same source_name::procedure_title always resolves to the same
# Qdrant point id — re-ingest becomes an upsert, not a race between a stale-delete
# and a fresh-insert. This namespace is DISTINCT from db.vector.FIREKEEP_UUID_NAMESPACE
# (used for regular-memory ids) so skill ids and memory ids occupy disjoint id
# spaces and can never collide (final-review hygiene). No production skills carry
# doc-source ids yet, so choosing a fresh namespace here is free.
SKILL_NS = uuid.UUID("5c0ffee5-5111-4b0b-9d0c-5c0ffee55111")

SKILL_CONTENT_TEMPLATE = """\
trigger: {trigger}
symptoms: {symptoms}
domain: {domain}
verified_on: {verified_on}
---
{body}"""

_LLM_PROMPT = """\
You are a technical knowledge distiller. Given a session's goal, shadow notes, and outcome, \
synthesize a reusable skill card in EXACTLY this format (no extra text before or after):

trigger: <one sentence — what situation activates this skill>
symptoms: <observable signals: error messages, failing patterns>
domain: <single word: e.g. neo4j, docker, qdrant, python, api-auth>
verified_on: <project/YYYY-MM>
---
## What's happening
<root cause in 1-3 sentences>

## Steps
1. <first action>
2. <second action>
...

## Gotchas
- <things that look like the fix but aren't>

## Example
<concrete command or snippet that worked>

SESSION GOAL: {goal}
SHADOW NOTES: {shadow_text}
OUTCOME: {outcome}"""

_DOC_LLM_PROMPT = """\
You are a technical knowledge distiller. From the following document, extract the \
procedure titled "{title}" and draft a reusable skill card in EXACTLY this format \
(no extra text before or after):

trigger: <one sentence — what situation activates this skill>
symptoms: <observable signals: error messages, failing patterns>
domain: <single word: e.g. neo4j, docker, qdrant, python, api-auth>
verified_on: <project/YYYY-MM>
---
## What's happening
<root cause in 1-3 sentences>

## Steps
1. <first action>
2. <second action>
...

## Gotchas
- <things that look like the fix but aren't>

## Example
<concrete command or snippet that worked>

SOURCE DOCUMENT: {source_name}
PROCEDURE TITLE: {title}
DOCUMENT CONTENT:
{doc_content}"""


def build_skill_content(
    trigger: str, symptoms: str, domain: str, verified_on: str, body: str
) -> str:
    return SKILL_CONTENT_TEMPLATE.format(
        trigger=trigger, symptoms=symptoms,
        domain=domain, verified_on=verified_on, body=body,
    )


def parse_skill_content(raw: str) -> dict[str, str]:
    """Parse hybrid skill content into header fields + body."""
    if "---" not in raw:
        # Fallback: treat entire text as body
        return {
            "trigger": "Synthesized skill",
            "symptoms": "",
            "domain": "",
            "verified_on": "",
            "body": raw.strip(),
        }
    header_raw, _, body = raw.partition("---")
    result: dict[str, str] = {"body": body.strip()}
    for line in header_raw.strip().splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    for field in ("trigger", "symptoms", "domain", "verified_on"):
        result.setdefault(field, "")
    if not result["trigger"]:
        result["trigger"] = "Synthesized skill"
    return result


class SkillSynthesizer:
    def __init__(self, settings: Any) -> None:
        self._settings = settings

    async def synthesize(
        self,
        session_id: str,
        score: SkillScore,
        project: str | None = None,
        agent_id: str = "unknown",
        namespace: str = "default",
    ) -> dict[str, Any]:
        """Synthesize a skill from session data and store in Qdrant.

        If the LLM call fails OR produces no usable skill (empty trigger and body),
        NO skill is stored — a failed synthesis must not pollute the draft queue
        with a 'Synthesis failed' placeholder (which is worse than nothing, since a
        human then has to find and delete it). The session is still marked evaluated
        so the periodic agent doesn't retry-loop on it."""
        try:
            shadow_text, goal, outcome = await self._fetch_session_data(session_id)
            content = await self._call_llm(goal, shadow_text, outcome)
            parsed = parse_skill_content(content)
        except Exception:
            logger.exception("Skill synthesis LLM call failed for session %s", session_id)
            await self._mark_evaluated(session_id)
            return {"status": "synthesis_failed", "session_id": session_id}

        trigger = (parsed.get("trigger") or "").strip()
        body = (parsed.get("body") or "").strip()
        if not trigger and not body:
            logger.warning("Skill synthesis produced empty content for session %s — not stored", session_id)
            await self._mark_evaluated(session_id)
            return {"status": "empty", "session_id": session_id}

        full_content = build_skill_content(
            trigger=parsed.get("trigger", ""),
            symptoms=parsed.get("symptoms", ""),
            domain=parsed.get("domain", ""),
            verified_on=parsed.get("verified_on", ""),
            body=parsed.get("body", ""),
        )

        payload = {
            "memory_type": "skill",
            "skill_status": "draft",
            "trigger": parsed.get("trigger", ""),
            "symptoms": parsed.get("symptoms", ""),
            "domain": parsed.get("domain", ""),
            "skill_score": score.total,
            "source_session_id": session_id,
            "project": project,
            "agent_id": agent_id,
            "namespace": namespace,
            "content": full_content,
        }
        skill_id = await self._store(full_content, payload)
        await self._mark_evaluated(session_id)
        return {"status": "ok", "skill_id": skill_id, "trigger": payload["trigger"]}

    async def synthesize_from_document(
        self,
        *,
        source_name: str,
        procedure_title: str,
        doc_content: str,
        project: str | None = None,
        namespace: str = "default",
    ) -> dict[str, Any]:
        """Draft a skill from a single procedure extracted out of an ingested document.

        The Qdrant point id is deterministic — uuid5(SKILL_NS, "source::title") —
        so re-ingesting the same document/procedure pair always targets the same
        point (idempotent upsert, no pre-flight delete, no race).

        Active-guard: if that id already holds a point promoted to
        skill_status="active" (a human approved it), the new draft content is
        NOT written over it. Instead the existing point is flagged
        needs_rereview=True so a human knows the source document changed.
        """
        try:
            raw = await self._call_llm_doc(source_name, procedure_title, doc_content)
            parsed = parse_skill_content(raw)
        except Exception:
            logger.exception(
                "Doc skill synthesis LLM call failed for %s :: %s", source_name, procedure_title
            )
            return {"status": "synthesis_failed", "source_doc": source_name,
                    "procedure_title": procedure_title}

        # A failed/empty synthesis must not become a 'Synthesis failed' placeholder
        # draft — that just pollutes the review queue.
        if not (parsed.get("trigger") or "").strip() and not (parsed.get("body") or "").strip():
            logger.warning("Doc skill synthesis produced empty content for %s :: %s — not stored",
                           source_name, procedure_title)
            return {"status": "empty", "source_doc": source_name,
                    "procedure_title": procedure_title}

        full_content = build_skill_content(
            trigger=parsed.get("trigger", ""),
            symptoms=parsed.get("symptoms", ""),
            domain=parsed.get("domain", ""),
            verified_on=parsed.get("verified_on", ""),
            body=parsed.get("body", ""),
        )

        payload = {
            "memory_type": "skill",
            "skill_status": "draft",
            "trigger": parsed.get("trigger", ""),
            "symptoms": parsed.get("symptoms", ""),
            "domain": parsed.get("domain", ""),
            "skill_score": 0.0,
            "source_session_id": None,
            "project": project,
            "agent_id": None,
            "namespace": namespace,
            "content": full_content,
            "source_type": "document",
            "content_class": "procedural",
            "source_doc": source_name,
            "procedure_title": procedure_title,
            "needs_rereview": False,
        }

        skill_id = str(uuid.uuid5(SKILL_NS, f"{source_name}::{procedure_title}"))

        s = self._settings
        aq = AsyncQdrantClient(host=s.QDRANT_HOST, port=s.QDRANT_PORT)
        try:
            existing = await aq.retrieve(
                collection_name=s.QDRANT_COLLECTION,
                ids=[skill_id],
                with_payload=True,
                with_vectors=False,
            )
            if existing and (existing[0].payload or {}).get("skill_status") == "active":
                await aq.set_payload(
                    collection_name=s.QDRANT_COLLECTION,
                    payload={
                        "needs_rereview": True,
                        "rereview_source_updated_at": datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
                    },
                    points=[skill_id],
                )
                return {"status": "rereview_flagged", "id": skill_id}
        finally:
            await aq.close()

        await self._store(full_content, payload, skill_id=skill_id)
        return {"status": "drafted", "id": skill_id}

    async def _fetch_session_data(self, session_id: str) -> tuple[str, str, str]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{self._settings.BRIDGE_URL}/sessions/{session_id}",
                headers=internal_key_headers(self._settings.FIREKEEP_INTERNAL_KEY),
            )
            resp.raise_for_status()
            data = resp.json()
        shadow = data.get("shadow") or {}
        texts: list[str] = []
        for v in shadow.get("scratch", {}).values():
            texts.append(str(v))
        for entry in shadow.get("decision", []):
            texts.append(str(entry.get("value", "")))
        for entry in shadow.get("progress", []):
            texts.append(str(entry.get("value", "")))
        shadow_text = " | ".join(texts)[:2000]
        goal = data.get("goal", "")
        outcome = data.get("outcome", "")
        return shadow_text, goal, outcome

    async def _call_llm(self, goal: str, shadow_text: str, outcome: str) -> str:
        s = self._settings
        prompt = _LLM_PROMPT.format(goal=goal, shadow_text=shadow_text, outcome=outcome)
        headers = {"Authorization": f"Bearer {s.LLM_API_KEY}"} if s.LLM_API_KEY else {}
        async with httpx.AsyncClient(timeout=s.SKILL_SYNTH_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{s.LLM_BASE_URL}/chat/completions",
                json={"model": s.LLM_MODEL, "messages": [{"role": "user", "content": prompt}]},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    async def _call_llm_doc(self, source_name: str, title: str, doc_content: str) -> str:
        """Doc-mode LLM call: extract one named procedure out of a document's
        full text and draft it as a skill card. Same call shape as `_call_llm`
        (no response_format — parse_skill_content handles the header/body text)."""
        s = self._settings
        prompt = _DOC_LLM_PROMPT.format(
            source_name=source_name, title=title, doc_content=doc_content
        )
        headers = {"Authorization": f"Bearer {s.LLM_API_KEY}"} if s.LLM_API_KEY else {}
        async with httpx.AsyncClient(timeout=s.SKILL_SYNTH_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{s.LLM_BASE_URL}/chat/completions",
                json={"model": s.LLM_MODEL, "messages": [{"role": "user", "content": prompt}]},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    async def _store(self, content: str, payload: dict, skill_id: str | None = None) -> str:
        s = self._settings
        embedding = await self._embed(content)
        if skill_id is None:
            skill_id = str(uuid.uuid4())
        payload["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        aq = AsyncQdrantClient(host=s.QDRANT_HOST, port=s.QDRANT_PORT)
        try:
            await aq.upsert(
                collection_name=s.QDRANT_COLLECTION,
                points=[PointStruct(id=skill_id, vector=embedding, payload=payload)],
            )
        finally:
            await aq.close()
        return skill_id

    async def _embed(self, text: str) -> list[float]:
        s = self._settings
        headers = {"Authorization": f"Bearer {s.LLM_API_KEY}"} if s.LLM_API_KEY else {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{s.LLM_BASE_URL}/embeddings",
                json={"model": s.EMBEDDING_MODEL, "input": text},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]

    async def _mark_evaluated(self, session_id: str) -> None:
        """Set nc:skill:evaluated:{session_id} in Redis — prevents Pass 9 double-processing."""
        try:
            r = redis.asyncio.from_url(self._settings.REDIS_URL, decode_responses=True)
            try:
                await r.set(f"nc:skill:evaluated:{session_id}", "1", ex=86400 * 7)
            finally:
                await r.aclose()
        except Exception:
            logger.warning("Failed to mark session %s as skill-evaluated", session_id)
