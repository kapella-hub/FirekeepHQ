"""The exception inbox's section gatherers — one function per queue.

Each function reads exactly one store and returns one section. They do NOT
guard themselves: `api.py` wraps every call so a single broken store degrades
that section to an error marker instead of 500-ing the whole inbox (the
`run_doctor` philosophy — a diagnostic surface that dies on its first bad
dependency is worthless precisely when it is needed). Keeping the try/except
in the caller rather than in each gatherer means the guard cannot be
accidentally omitted for a section added later: the caller's helper is the only
way a section reaches the response.

COUNTING IS SCROLL-AND-LEN. There is no `client.count` call anywhere in this
codebase and this is not the file to introduce one — the canonical idiom is
`app/knowledge/api.py`'s draft-skill count (scroll with `with_payload=False`,
take `len`). The cost is bounded by the scroll `limit`, and where that limit
can bite we say so in the response rather than reporting a capped number as if
it were the truth.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchValue, Range

logger = logging.getLogger(__name__)

# How many points a section may scroll. A section whose real population exceeds
# this reports `approximate: true` rather than a confidently wrong count — the
# same honesty rule the dreaming scan states about its own cap.
SECTION_SCAN_LIMIT = 1000

# How many rows a section hands the dashboard. The inbox is a triage surface:
# the count answers "how much is waiting", the items answer "what should I look
# at first". Shipping all 1000 would make the panel the thing that needs
# triaging.
SECTION_ITEM_LIMIT = 20

# Ceiling on the eval-DLQ key scan. These keys carry a 7-day TTL, so the
# population is self-limiting; the cap exists so a pathological failure storm
# cannot make the inbox itself the slow thing.
DLQ_SCAN_CAP = 1000

# low_efficacy_skills (D4) — visibility only, see the section docstring below.
# MIN_N is a small evidence floor: skill_efficacy is shrunk toward neutral 0.5
# by a Beta prior of OWM_PRIOR_N (default 5) pseudo-observations (app/owm.py),
# so below that many real observations the score is still mostly the prior,
# not a measurement. Matching OWM_PRIOR_N's default here is deliberate — it is
# the point past which the shrinkage stops dominating.
MIN_N = 5
# THRESHOLD is a below-neutral cutoff: skill_efficacy is a success rate in
# [0, 1] centered on 0.5, so anything at or above 0.4 is doing roughly as well
# as (or better than) a coin flip and is not a triage candidate.
THRESHOLD = 0.4

_EVAL_DLQ_PREFIX = "rp:eval_dlq:"


def _preview(text: object, limit: int = 120) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[: limit - 1] + "…"


async def _scroll(vector, settings, conditions: list) -> tuple[list, bool]:
    """Scroll one filtered slice. Returns (points, capped).

    `capped` is True when the page came back full, which is the only signal
    available that there may be more: Qdrant does not tell us the total, and
    inferring one from a full page would be inventing a number.
    """
    points, _ = await vector._client.scroll(
        collection_name=settings.QDRANT_COLLECTION,
        scroll_filter=Filter(must=conditions),
        limit=SECTION_SCAN_LIMIT,
        with_payload=True,
        with_vectors=False,
    )
    return list(points), len(points) >= SECTION_SCAN_LIMIT


def _skill_row(point) -> dict[str, Any]:
    """One skill, as a triage row.

    `title` prefers the procedure title a document-derived skill carries and
    falls back to the trigger, because a client-authored skill (`skill_create`)
    has no `procedure_title` at all and would otherwise render as a blank row
    with a UUID next to it.
    """
    payload = point.payload or {}
    return {
        "id": str(point.id),
        "title": payload.get("procedure_title") or payload.get("trigger") or "",
        "trigger": payload.get("trigger") or "",
        "source_doc": payload.get("source_doc") or "",
        "created": payload.get("timestamp") or "",
    }


async def draft_skills(vector, settings) -> dict[str, Any]:
    """Skills awaiting human approval — `status="draft"` from docs->skills."""
    points, capped = await _scroll(vector, settings, [
        FieldCondition(key="memory_type", match=MatchValue(value="skill")),
        FieldCondition(key="skill_status", match=MatchValue(value="draft")),
    ])
    return {
        "count": len(points),
        "approximate": capped,
        "items": [_skill_row(p) for p in points[:SECTION_ITEM_LIMIT]],
    }


async def stale_skills(vector, settings) -> dict[str, Any]:
    """Active skills the staleness sweep flagged as un-recalled and un-reviewed.

    `stale` is a payload BOOL that the sweep sets both ways (True on flag,
    False on unflag — `app/skills/staleness.py`), so matching on `value=True`
    is exact rather than a "field present" approximation.
    """
    points, capped = await _scroll(vector, settings, [
        FieldCondition(key="memory_type", match=MatchValue(value="skill")),
        FieldCondition(key="stale", match=MatchValue(value=True)),
    ])
    return {
        "count": len(points),
        "approximate": capped,
        "items": [_skill_row(p) for p in points[:SECTION_ITEM_LIMIT]],
    }


async def rereview_skills(vector, settings) -> dict[str, Any]:
    """Active skills whose SOURCE DOCUMENT changed under them.

    Not gated on `skill_status` here even though it could be: the only writer
    of `needs_rereview=True` (`skills/synthesizer.py`) sets it exclusively on
    skills that are already active, so adding the status condition would
    narrow nothing while implying a distinction that does not exist. If a
    future writer ever flags a draft, this section should surface it rather
    than silently drop it — a review queue that filters out work it was not
    expecting is how work disappears.
    """
    points, capped = await _scroll(vector, settings, [
        FieldCondition(key="memory_type", match=MatchValue(value="skill")),
        FieldCondition(key="needs_rereview", match=MatchValue(value=True)),
    ])
    return {
        "count": len(points),
        "approximate": capped,
        "items": [_skill_row(p) for p in points[:SECTION_ITEM_LIMIT]],
    }


async def low_efficacy_skills(vector, settings) -> dict[str, Any]:
    """Active skills the nightly OWM pass scored below neutral, with enough
    evidence to trust the number (outcome truth PR3, D4).

    VISIBILITY ONLY — this section does not change recall ranking, does not
    mutate `skill_status`, and does not write anything. A flagged skill is a
    human's cue to go read it; the ranking-side response to a low score is
    explicitly deferred (spec D4).

    Filtered server-side on BOTH `skill_efficacy_n >= MIN_N` and
    `skill_efficacy < THRESHOLD` (the `_qm.Range` idiom `app/owm.py`'s
    stale-reset sweep uses for the same field) — a skill missing either
    condition has either too little evidence or nothing to triage. `n` is
    carried on every row alongside the score for the same reason MIN_N
    exists: a reader who sees only `skill_efficacy` cannot tell a genuinely
    poor performer from a skill still near the neutral prior.
    """
    points, capped = await _scroll(vector, settings, [
        FieldCondition(key="memory_type", match=MatchValue(value="skill")),
        FieldCondition(key="skill_status", match=MatchValue(value="active")),
        FieldCondition(key="skill_efficacy_n", range=Range(gte=MIN_N)),
        FieldCondition(key="skill_efficacy", range=Range(lt=THRESHOLD)),
    ])
    return {
        "count": len(points),
        "approximate": capped,
        "items": [_low_efficacy_row(p) for p in points[:SECTION_ITEM_LIMIT]],
    }


def _low_efficacy_row(point) -> dict[str, Any]:
    payload = point.payload or {}
    return {
        "id": str(point.id),
        "trigger": payload.get("trigger") or "",
        "skill_efficacy": payload.get("skill_efficacy"),
        "skill_efficacy_n": payload.get("skill_efficacy_n"),
    }


async def contested_memories(vector, settings) -> dict[str, Any]:
    """Active memories the deep contradiction pass marked as CONTESTED.

    Contested is the state between "agrees" and "superseded": two active
    memories that disagree, where similarity alone is not enough to decide
    which one loses, so BOTH stay active and recall annotates them rather than
    one silently winning. The pass that flags them writes `contested`,
    `contested_with` and `contested_at` (`workers/memory_agent.py`), and the
    verdict is a human one — `POST /memory/contested/resolve`.

    Which is exactly why this belongs in an inbox: a contested pair is the one
    lifecycle state the system deliberately refuses to decide on its own, so
    it accumulates forever unless somebody is told it exists. Both sides of a
    pair are flagged, so a single dispute appears here as two rows.
    """
    points, capped = await _scroll(vector, settings, [
        FieldCondition(key="status", match=MatchValue(value="active")),
        FieldCondition(key="contested", match=MatchValue(value=True)),
    ])
    pairs = []
    for p in points[:SECTION_ITEM_LIMIT]:
        payload = p.payload or {}
        pairs.append({
            "id": str(p.id),
            "contested_with": payload.get("contested_with") or "",
            "contested_at": payload.get("contested_at") or "",
            "text_preview": _preview(payload.get("text")),
        })
    return {"count": len(points), "approximate": capped, "pairs": pairs}


async def procedure_proposals(redis_client, settings) -> dict[str, Any]:
    """Living Procedures' nightly proposals, across every skill.

    Gated on PROCEDURE_ENABLED and reported as `enabled: false` with an empty
    list rather than omitted: a disabled subsystem and a subsystem with nothing
    to propose look identical from an empty array, and only one of them is
    something an operator can act on.
    """
    if not getattr(settings, "PROCEDURE_ENABLED", False):
        return {"enabled": False, "count": 0, "items": []}

    from app.procedures import store as proc_store

    proposals = await proc_store.list_proposals(redis_client, skill_id=None)
    return {
        "enabled": True,
        "count": len(proposals),
        "items": proposals[:SECTION_ITEM_LIMIT],
    }


async def runbook_deviations(redis_client, settings) -> dict[str, Any]:
    """Enforced Runbooks' deviation ledger — blocks fired, challenges
    acknowledged, matched commands that failed.

    Same PROCEDURE_ENABLED gate and `enabled: false` shape as the proposals
    section, for the same reason: a disabled subsystem and a quiet one are
    different states with different remedies. The ledger LTRIMs itself to
    `MAX_DEVIATIONS` (a disclosed cap), so reading that many is reading all of
    it — and a full read means older deviations were already trimmed away,
    which is what `approximate` exists to say. Rows deliberately drop
    member/agent/command_hash: triage needs what happened and where, not who,
    and the ledger stores no raw command text at all (secrets).
    """
    if not getattr(settings, "PROCEDURE_ENABLED", False):
        return {"enabled": False, "count": 0, "items": []}

    from app.procedures import store as proc_store
    from app.procedures.api import _deployment_workspace

    records = await proc_store.list_deviations(
        redis_client, _deployment_workspace(), limit=proc_store.MAX_DEVIATIONS)
    return {
        "enabled": True,
        "count": len(records),
        "approximate": len(records) >= proc_store.MAX_DEVIATIONS,
        "items": [{
            "at": r.get("at") or "",
            "kind": r.get("kind") or "",
            "skill_id": r.get("skill_id") or "",
            "step_id": r.get("step_id") or "",
            "session": r.get("session") or "",
            "detail": _preview(r.get("detail")),
        } for r in records[:SECTION_ITEM_LIMIT]],
    }


async def eval_dlq(replay_redis) -> dict[str, Any]:
    """Sessions whose eval failed and were dead-lettered.

    These keys have had NO reader since they were introduced: `compute.py`
    writes `rp:eval_dlq:<session_id>` with a 7-day TTL inside a bare
    `except: pass`, and then nothing ever looked. A failure record nobody reads
    is indistinguishable from no failure, which is exactly the state this
    inbox exists to end.

    SCAN, never KEYS — this runs against the live replay Redis on an operator's
    page load, and KEYS blocks the server for the length of the keyspace.
    Counting walks keys only (no GET); bodies are fetched for the rows actually
    shown, so a large DLQ costs one scan rather than one round trip per entry.
    """
    keys: list[str] = []
    count = 0
    capped = False
    async for key in replay_redis.scan_iter(match=f"{_EVAL_DLQ_PREFIX}*", count=200):
        count += 1
        if len(keys) < SECTION_ITEM_LIMIT:
            keys.append(key.decode() if isinstance(key, bytes) else str(key))
        if count >= DLQ_SCAN_CAP:
            capped = True
            break

    items: list[dict[str, Any]] = []
    for key in keys:
        raw = await replay_redis.get(key)
        if raw is None:
            # Expired between the scan and the read. Its TTL did what it was
            # for; it is not an error and must not render as one.
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            items.append({"session_id": key[len(_EVAL_DLQ_PREFIX):], "error": "unparsed"})
            continue
        if not isinstance(entry, dict):
            items.append({"session_id": key[len(_EVAL_DLQ_PREFIX):], "error": "unparsed"})
            continue
        items.append({
            "session_id": entry.get("session_id") or key[len(_EVAL_DLQ_PREFIX):],
            "error": _preview(entry.get("error"), 200),
            "failure_type": entry.get("failure_type") or "unknown",
            "timestamp": entry.get("timestamp") or "",
        })

    return {"count": count, "approximate": capped, "items": items}
