"""LLM-based skill synthesis from session replay + shadow."""
from __future__ import annotations

import datetime
import json
import logging
import uuid
from typing import Any

import httpx
import redis.asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct

from app import llm
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

# ---------------------------------------------------------------------------
# Structured outputs — why a skill card is drafted as JSON and rendered here
# ---------------------------------------------------------------------------
#
# The two prompts ABOVE ask for a Markdown card in free-form text and are kept
# only as the last rung of the fallback ladder (see `_chat`). They are not what
# is sent to a backend that accepts a schema, because on the reference
# deployment they never produced a card at all.
#
# MEASURED 2026-08-06, live on the VPS (ollama 0.32.4, qwen3:4b, native
# `/api/chat`, `think:false`, the real `_DOC_LLM_PROMPT` over the real
# "Runbook: Restart stuck Celery worker" corpus document):
#
#   free-form, num_predict=800  -> done_reason="length", eval_count=800,
#                                  3512 chars, 143.82s, NO `---` header,
#                                  NO `## Steps`, tail mid-sentence
#                                  ("...But the problem doesn't specify.")
#   free-form + `/no_think`     -> done_reason="length", eval_count=800,
#                                  3393 chars, 116.63s, still no card, head
#                                  "We are given a specific procedure title:"
#   SCHEMA, num_predict=800     -> done_reason="stop", 263-317 output tokens,
#                                  parsed, all 8 fields, 33.69-54.54s
#                                  (5 runs: small doc x3, larger doc x2)
#
# `think:false` does NOT stop qwen3:4b deliberating on this ollama build — it
# moves the deliberation OUT of the `thinking` key and INTO `content` (one probe
# returned a literal `</think>` inside `content`). So the token budget was being
# spent on reasoning before the card began, and the card never began. That is
# the whole of the "Docs->Skills produces zero drafts" failure, and it is also
# where the live review queue's `trigger: "Synthesized skill"` + raw-deliberation
# body came from: the deliberation IS the completion.
#
# Raising the cap cannot fix it on this deployment. Generation measured ~5.6
# tok/s, so `SKILL_SYNTH_TIMEOUT_SECONDS=300` buys ~1680 tokens; a run long
# enough to deliberate AND write a card would hit the clock instead of the cap
# and fail just as completely, only slower. A grammar removes the deliberation
# rather than budgeting for it — the same result LLM-endpoint phase 3 measured
# for the decision board (adherence 0 -> 100%, latency no worse, because a
# constrained decode stops emitting wasted tokens).
#
# `SKILL_SYNTH_MAX_TOKENS=800` is therefore LEFT AT 800: under the schema the
# worst measured run used 317 of it. It is now a real safety bound instead of
# the thing that broke the feature.
_CARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "trigger": {"type": "string"},
        "symptoms": {"type": "string"},
        "domain": {"type": "string"},
        "verified_on": {"type": "string"},
        "whats_happening": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}},
        "gotchas": {"type": "array", "items": {"type": "string"}},
        "example": {"type": "string"},
    },
    "required": [
        "trigger", "symptoms", "domain", "verified_on",
        "whats_happening", "steps", "gotchas", "example",
    ],
}

_FIELD_GUIDE = """\
Field meanings:
- trigger: one sentence — what situation activates this skill
- symptoms: observable signals (error messages, failing patterns)
- domain: a single lowercase word, e.g. neo4j, docker, qdrant, python, api-auth
- verified_on: project/YYYY-MM
- whats_happening: the root cause in 1-3 sentences
- steps: ordered actions, one per array element, no numbering
- gotchas: things that look like the fix but aren't, one per array element
- example: a concrete command or snippet that worked"""

_DOC_JSON_PROMPT = """\
You are a technical knowledge distiller. From the following document, extract the \
procedure titled "{title}" and return a skill card as JSON.

""" + _FIELD_GUIDE + """

SOURCE DOCUMENT: {source_name}
PROCEDURE TITLE: {title}
DOCUMENT CONTENT:
{doc_content}"""

_SESSION_JSON_PROMPT = """\
You are a technical knowledge distiller. Given a session's goal, shadow notes, and \
outcome, return a reusable skill card as JSON.

""" + _FIELD_GUIDE + """

SESSION GOAL: {goal}
SHADOW NOTES: {shadow_text}
OUTCOME: {outcome}"""


def _render_card(raw: str) -> str:
    """Turn a schema-shaped JSON completion into skill-card text.

    Anything that is not a JSON object carrying at least one card field is
    returned VERBATIM, so the free-form path (`parse_skill_content`) still sees
    exactly what it saw before. That is the fallback for a backend whose
    `json_schema` rung `llm.chat` had to drop, and it is why adding the schema
    cannot regress a deployment where the old prompt happened to work.
    """
    text = raw.strip()
    if not text.startswith("{"):
        return raw
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return raw
    if not isinstance(payload, dict):
        return raw
    if not any(k in payload for k in _CARD_SCHEMA["properties"]):
        return raw
    card = card_from_payload(payload)
    return build_skill_content(
        trigger=card["trigger"], symptoms=card["symptoms"],
        domain=card["domain"], verified_on=card["verified_on"],
        body=card["body"],
    )


def _bullets(values: Any, marker: str) -> str:
    """Render a schema array as Markdown lines; tolerate a bare string."""
    if isinstance(values, str):
        items = [v.strip() for v in values.splitlines() if v.strip()]
    elif isinstance(values, (list, tuple)):
        items = [str(v).strip() for v in values if str(v).strip()]
    else:
        items = []
    if marker == "1.":
        return "\n".join(f"{i}. {v}" for i, v in enumerate(items, 1))
    return "\n".join(f"- {v}" for v in items)


def card_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    """Render a schema-shaped card payload into the parsed-card dict.

    The STORED artifact is unchanged — `build_skill_content` still writes the
    same `trigger:/symptoms:/domain:/verified_on:` header plus a Markdown body,
    and `GET /skills` still serves exactly that. The grammar lives on the wire,
    not in the store (the `dreams/profile.py` precedent).

    Section headings are emitted verbatim because downstream code reads them:
    `card_defect` requires `## Steps`, and a human reviewing the draft queue is
    reading the same shape a hand-authored skill has.
    """
    body_parts: list[str] = []
    whats = str(payload.get("whats_happening") or "").strip()
    if whats:
        body_parts.append(f"## What's happening\n{whats}")
    steps = _bullets(payload.get("steps"), "1.")
    if steps:
        body_parts.append(f"## Steps\n{steps}")
    gotchas = _bullets(payload.get("gotchas"), "-")
    if gotchas:
        body_parts.append(f"## Gotchas\n{gotchas}")
    example = str(payload.get("example") or "").strip()
    if example:
        body_parts.append(f"## Example\n{example}")
    return {
        "trigger": str(payload.get("trigger") or "").strip(),
        "symptoms": str(payload.get("symptoms") or "").strip(),
        "domain": str(payload.get("domain") or "").strip(),
        "verified_on": str(payload.get("verified_on") or "").strip(),
        "body": "\n\n".join(body_parts),
    }


def build_skill_content(
    trigger: str, symptoms: str, domain: str, verified_on: str, body: str
) -> str:
    return SKILL_CONTENT_TEMPLATE.format(
        trigger=trigger, symptoms=symptoms,
        domain=domain, verified_on=verified_on, body=body,
    )


FALLBACK_TRIGGER = "Synthesized skill"


def card_defect(parsed: dict[str, str]) -> str | None:
    """Name what is wrong with a parsed skill card, or None if it is usable.

    WHY THIS EXISTS. The empty-guard the callers already had requires BOTH
    trigger and body to be blank — and neither of the two ways a synthesis
    actually fails produces a blank trigger:

      * `parse_skill_content` substitutes the literal ``"Synthesized skill"``
        whenever the model returned prose with no ``---`` header. On the live
        deployment that stored the model's raw deliberation as a skill —
        trigger ``"Synthesized skill"``, symptoms ``""``, domain ``""``, body
        "We are given a specific procedure title... But the problem says
        \\"single word\\"... I think the domain should be" — truncated
        mid-sentence.
      * A small model handed a template often ECHOES it. The sibling draft from
        the same document stored ``trigger: "<one sentence — what situation
        activates this skill>"`` and the matching placeholders for symptoms and
        domain, verbatim, as real field values.

    Both are truthy, so both sailed past the guard, and every status surface
    reported success: the worker logged ``{status: drafted}``,
    ``/knowledge/sources`` showed ``classified/procedural, skills_queued=2``,
    and both sat in the human review queue looking legitimate. The code's own
    stated intent — "A failed/empty synthesis must not become a placeholder
    draft — that just pollutes the review queue" — is what this enforces.

    A skill card is a CARD: a trigger a human wrote, and steps. Anything that
    is neither is not a draft worth reviewing.
    """
    trigger = (parsed.get("trigger") or "").strip()
    body = (parsed.get("body") or "").strip()

    if not trigger and not body:
        return "empty"
    if trigger == FALLBACK_TRIGGER:
        # No `---` header was parsed at all: the model returned prose, not a
        # card, and the trigger is the parser's own placeholder.
        return "no-card-header"
    for field_name in ("trigger", "symptoms", "domain"):
        value = (parsed.get(field_name) or "").strip()
        if value.startswith("<") and value.endswith(">"):
            return f"template-placeholder:{field_name}"
    if "## Steps" not in body:
        # A skill without steps is not a playbook. The prompt asks for the
        # section by name, so its absence means the model did not follow the
        # format rather than that this procedure happens to have no steps.
        return "no-steps"
    return None


def parse_skill_content(raw: str) -> dict[str, str]:
    """Parse hybrid skill content into header fields + body."""
    if "---" not in raw:
        # Fallback: treat entire text as body
        return {
            "trigger": FALLBACK_TRIGGER,
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
        result["trigger"] = FALLBACK_TRIGGER
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

        defect = card_defect(parsed)
        if defect:
            logger.warning(
                "Skill synthesis produced an unusable card for session %s (%s) — not stored",
                session_id, defect,
            )
            await self._mark_evaluated(session_id)
            return {"status": "empty", "session_id": session_id, "defect": defect}

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
        workspace_id: str | None = None,
        member_id: str | None = None,
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
        # draft — that just pollutes the review queue. `card_defect` is what makes
        # that true for the two failures that actually happen (a headerless prose
        # dump, and an echoed prompt template); the old blank-both test caught
        # neither, because both produce a truthy trigger.
        defect = card_defect(parsed)
        if defect:
            logger.warning(
                "Doc skill synthesis produced an unusable card for %s :: %s (%s) — not stored",
                source_name, procedure_title, defect,
            )
            return {"status": "empty", "source_doc": source_name,
                    "procedure_title": procedure_title, "defect": defect}

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
        # Tenancy. A skill point written with workspace_id=null is filtered out
        # of every recall path (VectorClient.search applies workspace_id as a
        # hard `must`), so it is not "awaiting review", it is unfindable —
        # measured live: a probe skill scored 0.877 at rank 1 with no filter
        # and vanished under the caller's real workspace. Emitted only when
        # known so a re-draft cannot overwrite a migration backfill with null.
        if workspace_id:
            payload["workspace_id"] = workspace_id
        if member_id:
            payload["member_id"] = member_id

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
        shadow = data.get("shadow")
        # GET /sessions/{id} returns `shadow` as the assembled MARKDOWN STRING
        # (bridge/app/mcp_server.py: assemble_shadow(data)), same defect as
        # app/skills/scorer.py::_score_resolution_language. Dict is still
        # accepted so a future shape change degrades instead of regressing.
        if isinstance(shadow, dict):
            # Real key names if this ever becomes a dict again: `decisions`/
            # `progress` (plural) with entries shaped {timestamp, content},
            # not `decision` with {value}.
            texts = [str(v) for v in (shadow.get("scratch") or {}).values()]
            for section in ("decisions", "progress"):
                texts += [str(e.get("content", "")) for e in (shadow.get(section) or [])]
        else:
            texts = [str(shadow or "")]
        shadow_text = " | ".join(texts)[:2000]
        goal = data.get("goal", "")
        outcome = data.get("outcome", "")
        return shadow_text, goal, outcome

    async def _chat(self, prompt: str, *, purpose: str) -> str:
        """One skill-card generation, via the shared endpoint helper.

        SCHEMA-CONSTRAINED. A skill card is still STORED as header-plus-Markdown
        and `parse_skill_content` still handles the text — but it is REQUESTED as
        JSON against `_CARD_SCHEMA` and rendered back to card text here, because
        free-form prompting produced no card at all on the reference deployment.
        The measurement, and why raising the token cap could not have fixed it,
        is with `_CARD_SCHEMA` above.

        A grammar is not a substitute for the guards below, it is what stops them
        firing. `llm.chat`'s own ladder drops the schema and retries once against
        a backend that rejects it (a schema implies `json_mode`, so the retry is
        still JSON); if even that yields prose, `_render_card` passes the text
        through untouched and the free-form parse path takes over exactly as
        before. Every rung degrades to a previously-shipped behaviour.

        `max_tokens` bounds output. Both synthesis calls previously sent no bound
        at all, so on a thinking model the reasoning and the card had to fit
        inside one timeout with nothing capping the former.

        Blank content RAISES rather than being returned. `parse_skill_content("")`
        yields the fallback dict whose trigger is the literal string
        "Synthesized skill" — which is truthy, so the callers' empty-guard
        (`if not trigger and not body`) does NOT fire and a contentless
        placeholder gets stored. Both callers document that a failed synthesis
        must never pollute the draft queue; raising is what makes that true. An
        empty completion is a real, observed failure mode on a thinking model
        (see dreams/synthesize.py), so this is a live guard, not a theoretical
        one.
        """
        s = self._settings
        result = await llm.chat(
            settings=s,
            messages=[{"role": "user", "content": prompt}],
            # No `native_timeout`: one budget for both endpoints here. Drafting
            # is generation-bound, so the native path is barely faster — see
            # SKILL_SYNTH_TIMEOUT_SECONDS in config.py for the measurement.
            timeout=s.SKILL_SYNTH_TIMEOUT_SECONDS,
            max_tokens=getattr(s, "SKILL_SYNTH_MAX_TOKENS", 800),
            json_schema=_CARD_SCHEMA,
            json_schema_name="skill_card",
            purpose=purpose,
        )
        if not result.content.strip():
            raise ValueError(
                f"{purpose}: model returned empty content "
                f"(endpoint={result.endpoint}, reasoning={len(result.reasoning)} chars)"
            )
        if result.truncated:
            # Stopped at the token cap, not at the end of the answer. The card
            # is half-written by construction — a live draft ended
            # '...I think the domain should be'. Storing it puts a
            # mid-sentence fragment in the human review queue wearing the same
            # badge as a real skill; raising sends it down the callers'
            # already-correct not-stored path.
            #
            # This guard is CORRECT and stays. It is also, since the schema
            # landed, expected never to fire on the reference deployment: the
            # worst of five live runs used 317 of the 800-token cap and stopped
            # on `done_reason="stop"`. Before the schema it fired on 100% of
            # drafts, which turned a right refusal into a disabled feature —
            # refusing bad output is only defensible when good output is
            # reachable.
            raise ValueError(
                f"{purpose}: model hit the {getattr(s, 'SKILL_SYNTH_MAX_TOKENS', 800)}-token "
                f"cap and the card is truncated (endpoint={result.endpoint}, "
                f"{len(result.content)} chars)"
            )
        return _render_card(result.content)

    async def _call_llm(self, goal: str, shadow_text: str, outcome: str) -> str:
        return await self._chat(
            _SESSION_JSON_PROMPT.format(
                goal=goal, shadow_text=shadow_text, outcome=outcome
            ),
            purpose="skill synthesis (session)",
        )

    async def _call_llm_doc(self, source_name: str, title: str, doc_content: str) -> str:
        """Doc-mode LLM call: extract one named procedure out of a document's
        full text and draft it as a skill card."""
        return await self._chat(
            _DOC_JSON_PROMPT.format(
                source_name=source_name, title=title, doc_content=doc_content
            ),
            purpose="skill synthesis (document)",
        )

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
