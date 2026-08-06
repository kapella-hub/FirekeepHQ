"""Every Redis read and write for Living Procedures.

Keys live on the CORTEX DATA db (REDIS_URL), not the replay db: these are
feature state, not trace events, and they must not inherit replay's retention
conventions. Evals are read from the replay db by the harden pass instead.

I3: nothing here writes to Qdrant. Step specs are owned by the skills PATCH
path alone, because a semantic PATCH does retrieve->merge->re-embed->full
upsert (skills/api.py) and would silently discard a concurrent set_payload.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchValue

logger = logging.getLogger(__name__)

INDEX_KEY = "proc:index"
_EXEC_PREFIX = "proc:exec:"
_EXEC_INDEX = "proc:exec:__index"
_STATS_PREFIX = "proc:stats:"
_PROPOSALS_PREFIX = "proc:proposals:"
_PROPOSAL_OWNER = "proc:proposal_owner"


def exec_key(session_id: str, skill_id: str) -> str:
    return f"{_EXEC_PREFIX}{session_id}:{skill_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def rebuild_index(vector, redis_client, settings) -> int:
    """Denormalise every active skill's file_glob specs into one key.

    I5: the pre-edit path must not touch Qdrant, so the scan happens here — on
    write and on the nightly pass — never on the hot path.
    """
    points, _ = await vector._client.scroll(
        collection_name=settings.QDRANT_COLLECTION,
        scroll_filter=Filter(must=[
            FieldCondition(key="memory_type", match=MatchValue(value="skill")),
            FieldCondition(key="skill_status", match=MatchValue(value="active")),
        ]),
        limit=1000,
        with_payload=True,
        with_vectors=False,
    )
    entries: list[dict[str, Any]] = []
    for p in points:
        payload = p.payload or {}
        specs = payload.get("step_specs") or []
        if not isinstance(specs, list):
            continue
        max_specs = int(getattr(settings, "PROCEDURE_MAX_SPECS", 50))
        for order, spec in enumerate(specs[:max_specs]):
            if not isinstance(spec, dict):
                continue
            if spec.get("kind") != "file_glob":
                continue  # unobservable steps are not matchable — but see `order`
            pattern = (spec.get("pattern") or "").strip()
            step_id = spec.get("id")
            if not pattern or not step_id:
                continue
            entries.append({
                "skill_id": str(p.id),
                "skill_trigger": payload.get("trigger") or "",
                "step_id": step_id,
                "step_text": spec.get("text") or "",
                "pattern": pattern,
                "load_bearing": bool(spec.get("load_bearing")),
                # POSITION IN THE FULL SPEC LIST, not in the filtered list:
                # "earlier step" is defined over step_specs, and renumbering
                # here would make the earlier-step check compare wrong steps.
                "order": order,
            })
    await redis_client.set(INDEX_KEY, json.dumps(entries))
    return len(entries)


async def load_index(redis_client) -> list[dict[str, Any]]:
    """Never raises: a corrupt or absent index degrades to no matching."""
    try:
        raw = await redis_client.get(INDEX_KEY)
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001
        logger.debug("procedure index unreadable: %s", exc)
        return []


async def record_observation(
    redis_client, settings, *, session_id: str, skill_id: str, step_id: str,
    action_id: str, target: str, agent_id: str, adapter: str,
) -> str:
    """Open-or-extend the execution for (session, skill). Returns its exec_id."""
    key = exec_key(session_id, skill_id)
    raw = await redis_client.hgetall(key)
    if raw:
        exec_id = raw.get("exec_id") or f"proc_{uuid.uuid4().hex[:12]}"
        observed = json.loads(raw.get("observed") or "{}")
    else:
        exec_id = f"proc_{uuid.uuid4().hex[:12]}"
        observed = {}
        await redis_client.hset(key, mapping={
            "exec_id": exec_id, "skill_id": skill_id, "session_id": session_id,
            "agent_id": agent_id,
            # NOTE: `adapter` is a TRANSPORT class (shell-hook|mcp|rest), not a
            # runtime — pre_tool hardcodes "shell-hook" on every runtime. It is
            # stored for diagnostics only and must never be used to infer
            # observability; I2 is what does that.
            "adapter": adapter,
            "opened_at": _now(), "warned": "{}",
        })
        await redis_client.sadd(_EXEC_INDEX, key)
    observed.setdefault(step_id, []).append(
        {"action_id": action_id, "target": target, "ts": _now()}
    )
    await redis_client.hset(key, mapping={
        "observed": json.dumps(observed), "last_seen_at": _now(),
    })
    ttl = int(getattr(settings, "PROCEDURE_EXEC_TTL_DAYS", 90)) * 86400
    await redis_client.expire(key, ttl)
    return exec_id


async def get_execution(redis_client, session_id: str, skill_id: str) -> dict | None:
    raw = await redis_client.hgetall(exec_key(session_id, skill_id))
    if not raw:
        return None
    out = dict(raw)
    out["observed"] = json.loads(raw.get("observed") or "{}")
    out["warned"] = json.loads(raw.get("warned") or "{}")
    return out


async def claim_warn(redis_client, settings, *, session_id: str, skill_id: str,
                     step_id: str) -> bool:
    """True exactly once per (execution, step) — the RethinkCounter shape."""
    key = f"{exec_key(session_id, skill_id)}:warned:{step_id}"
    ttl = int(getattr(settings, "PROCEDURE_EXEC_TTL_DAYS", 90)) * 86400
    return bool(await redis_client.set(key, _now(), nx=True, ex=ttl))


async def iter_executions(redis_client) -> list[dict[str, Any]]:
    """All live executions. Members whose key has expired are pruned from the
    index as they are found — the set has no TTL of its own."""
    out: list[dict[str, Any]] = []
    members = await redis_client.smembers(_EXEC_INDEX)
    for key in members:
        raw = await redis_client.hgetall(key)
        if not raw:
            await redis_client.srem(_EXEC_INDEX, key)
            continue
        rec = dict(raw)
        rec["observed"] = json.loads(raw.get("observed") or "{}")
        out.append(rec)
    return out


async def write_step_stats(redis_client, settings, skill_id: str, stats: dict) -> None:
    await redis_client.set(f"{_STATS_PREFIX}{skill_id}", json.dumps(stats))


async def get_step_stats(redis_client, skill_id: str) -> dict:
    try:
        raw = await redis_client.get(f"{_STATS_PREFIX}{skill_id}")
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


async def write_proposals(redis_client, skill_id: str, proposals: list[dict]) -> None:
    """Replaces this skill's proposals wholesale. A proposal with no supporting
    evidence in the window must DISAPPEAR (OWM's stale-reset shape) rather than
    stand forever — verdicts decay to neutral, they do not ratchet."""
    old = await list_proposals(redis_client, skill_id)
    for p in old:
        await redis_client.hdel(_PROPOSAL_OWNER, p["id"])
    await redis_client.set(f"{_PROPOSALS_PREFIX}{skill_id}", json.dumps(proposals))
    for p in proposals:
        await redis_client.hset(_PROPOSAL_OWNER, p["id"], skill_id)


async def list_proposals(redis_client, skill_id: str | None = None) -> list[dict]:
    if skill_id is not None:
        try:
            raw = await redis_client.get(f"{_PROPOSALS_PREFIX}{skill_id}")
            return json.loads(raw) if raw else []
        except Exception:  # noqa: BLE001
            return []
    out: list[dict] = []
    owners = await redis_client.hgetall(_PROPOSAL_OWNER)
    for sid in set(owners.values()):
        out.extend(await list_proposals(redis_client, sid))
    return out


async def dismiss_proposal(redis_client, proposal_id: str) -> bool:
    skill_id = await redis_client.hget(_PROPOSAL_OWNER, proposal_id)
    if not skill_id:
        return False
    remaining = [p for p in await list_proposals(redis_client, skill_id)
                 if p.get("id") != proposal_id]
    await redis_client.set(f"{_PROPOSALS_PREFIX}{skill_id}", json.dumps(remaining))
    await redis_client.hdel(_PROPOSAL_OWNER, proposal_id)
    return True
