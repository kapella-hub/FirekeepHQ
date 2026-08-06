"""Every Redis read and write for Living Procedures.

Keys live on the CORTEX DATA db (REDIS_URL), not the replay db: these are
feature state, not trace events, and they must not inherit replay's retention
conventions. Evals are read from the replay db by the harden pass instead.

DECODE-AGNOSTIC BY CONSTRUCTION. `app.state.redis_client` — the client both the
gateway stage and the /procedures router are handed — is built with no
`decode_responses` (`app/main.py`), so every read here arrives as BYTES, while
the harden pass builds its own decoding client. Reading `raw.get("observed")`
off a bytes-keyed dict returns None silently: it destroyed the execution record
on every write, emptied the receipts endpoint, and made a dismiss report success
while writing to `proc:proposals:b'<id>'`. Same `_s()` shape as
`collectors/state.py` and `dreams/state.py`, which carry it for the same reason.

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
COVERAGE_KEY = "proc:coverage"
_EXEC_PREFIX = "proc:exec:"
_EXEC_INDEX = "proc:exec:__index"
_STATS_PREFIX = "proc:stats:"
_PROPOSALS_PREFIX = "proc:proposals:"
_PROPOSAL_OWNER = "proc:proposal_owner"
_WRITTEN_KEY = "proc:written"
_RUN_KEY = "proc:run"
_UNJOINABLE_KEY = "proc:unjoinable"

# Qdrant page size for the index scan. Not a cap — the scan pages until the
# cursor is exhausted; this is only how much is asked for at a time.
_SCROLL_PAGE = 500

# One execution's per-step observation list. Every consumer of `observed` reads
# its KEYS (observe.py, harden.py), so the entries are receipts, not evidence —
# but the whole blob is HGETALL'd, parsed, appended to and re-serialised on the
# blocking pre-edit path for every matching edit, which is quadratic in the
# number of matches in one session. The TRUE count is kept in `observed_counts`,
# so the bound loses receipts, never a number anyone reports.
MAX_OBSERVATIONS_PER_STEP = 25

_UNKNOWN_RUN: dict[str, Any] = {
    "last_run": None,
    "executions": 0,
    "skills": 0,
    "proposals": 0,
    "spec_drift": 0,
    "orphans_cleared": 0,
    "orphan_sweep": "unknown",
    "outcome_backed_executions": 0,
    "outcome_success": 0,
    "outcome_failure": 0,
    "tier_b": "unknown",
    "verdict_ready_steps": 0,
    "health": "unknown",
    "error": None,
}


def exec_key(session_id: str, skill_id: str) -> str:
    return f"{_EXEC_PREFIX}{session_id}:{skill_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _s(value: Any) -> Any:
    """Decode one Redis reply. Non-bytes passes through untouched."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _smap(raw: Any) -> dict[str, str]:
    return {_s(k): _s(v) for k, v in (raw or {}).items()}


async def scan_active_skills(vector, settings) -> list[dict[str, Any]]:
    """Every active skill point, PAGED to exhaustion.

    The previous version asked for `limit=1000` once and dropped the scroll
    cursor on the floor, so a deployment with more than 1000 active skills
    indexed a truncated set and said nothing about it. `content` comes back too
    because the hardening pass compares each spec's text against it (spec_drift)
    — one read, two readers, rather than a second scan of the same collection.
    """
    out: list[dict[str, Any]] = []
    offset = None
    pages = 0
    while True:
        points, offset = await vector._client.scroll(
            collection_name=settings.QDRANT_COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="memory_type", match=MatchValue(value="skill")),
                FieldCondition(key="skill_status", match=MatchValue(value="active")),
            ]),
            limit=_SCROLL_PAGE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        pages += 1
        for p in points or []:
            payload = p.payload or {}
            out.append({
                "skill_id": str(p.id),
                "trigger": payload.get("trigger") or "",
                "content": payload.get("content") or "",
                "step_specs": payload.get("step_specs") or [],
                # The tenancy boundary, carried through to the index and the
                # coverage summary so the read surface has something to scope
                # on. Skill points already hold it (skills/api.py writes it from
                # the verified principal); nothing downstream of this scan did.
                "workspace_id": payload.get("workspace_id") or "",
            })
        if not offset or not points:
            break
        if pages > 10_000:  # a cursor that never advances, not a cap on data
            logger.warning("procedure skill scan stopped: scroll cursor did not "
                           "advance after %s pages (%s skills)", pages, len(out))
            break
    logger.debug("procedure skill scan: %s skills over %s page(s)", len(out), pages)
    return out


def index_entries(skills: list[dict[str, Any]], settings) -> tuple[list[dict], dict]:
    """Denormalise scanned skills into (matcher entries, coverage summary).

    Coverage is a SEPARATE product of the same pass because the matcher index
    holds file_glob specs only: deriving the rollup from it made a procedure
    whose steps are all `unobservable` vanish entirely, and reported
    `specs_total: 0` — which is the cold-start message, shown to a human who has
    just compiled the specs. H2 requires the opposite: say "0 of 7 observable".
    """
    max_specs = int(getattr(settings, "PROCEDURE_MAX_SPECS", 50))
    entries: list[dict[str, Any]] = []
    coverage: dict[str, dict[str, Any]] = {}
    for skill in skills:
        specs = skill.get("step_specs")
        if not isinstance(specs, list) or not specs:
            continue
        kept = [s for s in specs[:max_specs] if isinstance(s, dict)]
        observable = 0
        for order, spec in enumerate(specs[:max_specs]):
            if not isinstance(spec, dict):
                continue
            if spec.get("kind") != "file_glob":
                continue  # unobservable steps are not matchable — but see `order`
            pattern = (spec.get("pattern") or "").strip()
            step_id = spec.get("id")
            if not pattern or not step_id:
                continue
            observable += 1
            entries.append({
                "skill_id": skill["skill_id"],
                "skill_trigger": skill.get("trigger") or "",
                # Present but NOT enforced on the pre-edit path: the matcher
                # index is machine-global and ActionBeforeRequest carries no
                # principal, so the warn cannot be scoped without threading one
                # through the gateway (spec §6 H6). Carried so the data is there
                # when it can be.
                "workspace_id": skill.get("workspace_id") or "",
                "step_id": step_id,
                "step_text": spec.get("text") or "",
                "pattern": pattern,
                "load_bearing": bool(spec.get("load_bearing")),
                # POSITION IN THE FULL SPEC LIST, not in the filtered list:
                # "earlier step" is defined over step_specs, and renumbering
                # here would make the earlier-step check compare wrong steps.
                "order": order,
            })
        coverage[skill["skill_id"]] = {
            "trigger": skill.get("trigger") or "",
            "spec_count": len(kept),
            "observable": observable,
            "workspace_id": skill.get("workspace_id") or "",
        }
    return entries, coverage


async def write_index(redis_client, skills: list[dict[str, Any]], settings) -> int:
    entries, coverage = index_entries(skills, settings)
    await redis_client.set(INDEX_KEY, json.dumps(entries))
    await redis_client.set(COVERAGE_KEY, json.dumps(coverage))
    return len(entries)


async def rebuild_index(vector, redis_client, settings) -> int:
    """Denormalise every active skill's specs into the matcher index.

    I5: the pre-edit path must not touch Qdrant, so the scan happens here — on
    write and on the nightly pass — never on the hot path.
    """
    skills = await scan_active_skills(vector, settings)
    return await write_index(redis_client, skills, settings)


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


async def load_coverage(redis_client) -> dict[str, dict[str, Any]]:
    try:
        raw = await redis_client.get(COVERAGE_KEY)
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("procedure coverage unreadable: %s", exc)
        return {}


def new_exec_id() -> str:
    """Mint an exec_id ahead of the write.

    The advisory quotes it as its receipt (`Advisory.evidence_event_id`) and the
    advisory is built BEFORE the decision is settled, while the write happens
    only once it is — so the id has to exist independently of the write that
    stores it.
    """
    return f"proc_{uuid.uuid4().hex[:12]}"


async def record_observation(
    redis_client, settings, *, session_id: str, skill_id: str, step_id: str,
    action_id: str, target: str, agent_id: str, adapter: str,
    exec_id: str = "",
) -> str:
    """Open-or-extend the execution for (session, skill). Returns its exec_id."""
    key = exec_key(session_id, skill_id)
    raw = _smap(await redis_client.hgetall(key))
    if raw:
        exec_id = raw.get("exec_id") or exec_id or new_exec_id()
        observed = json.loads(raw.get("observed") or "{}")
        counts = json.loads(raw.get("observed_counts") or "{}")
    else:
        exec_id = exec_id or new_exec_id()
        observed, counts = {}, {}
        await redis_client.hset(key, mapping={
            "exec_id": exec_id, "skill_id": skill_id, "session_id": session_id,
            "agent_id": agent_id,
            # NOTE: `adapter` is a TRANSPORT class (shell-hook|mcp|rest), not a
            # runtime — pre_tool hardcodes "shell-hook" on every runtime. It is
            # stored for diagnostics only and must never be used to infer
            # observability; I2 is what does that.
            "adapter": adapter,
            "opened_at": _now(),
        })
        await redis_client.sadd(_EXEC_INDEX, key)
    counts[step_id] = int(counts.get(step_id, 0) or 0) + 1
    receipts = observed.setdefault(step_id, [])
    if len(receipts) < MAX_OBSERVATIONS_PER_STEP:
        receipts.append(
            {"action_id": action_id, "target": target, "ts": _now()}
        )
    await redis_client.hset(key, mapping={
        "observed": json.dumps(observed),
        "observed_counts": json.dumps(counts),
        "last_seen_at": _now(),
    })
    ttl = int(getattr(settings, "PROCEDURE_EXEC_TTL_DAYS", 90)) * 86400
    await redis_client.expire(key, ttl)
    return exec_id


def _parse_execution(raw: Any) -> dict[str, Any]:
    rec = _smap(raw)
    # A record written before the vestigial field was removed still carries it.
    # Dropped on read so the shape is one thing, not two, and so nothing can come
    # to depend on a latch that was never consulted.
    rec.pop("warned", None)
    try:
        rec["observed"] = json.loads(rec.get("observed") or "{}")
    except (TypeError, ValueError):
        rec["observed"] = {}
    try:
        rec["observed_counts"] = json.loads(rec.get("observed_counts") or "{}")
    except (TypeError, ValueError):
        rec["observed_counts"] = {}
    return rec


async def get_execution(redis_client, session_id: str, skill_id: str) -> dict | None:
    raw = await redis_client.hgetall(exec_key(session_id, skill_id))
    if not raw:
        return None
    return _parse_execution(raw)


async def claim_warn(redis_client, settings, *, session_id: str, skill_id: str,
                     step_id: str) -> bool:
    """True exactly once per (execution, step) — the RethinkCounter shape.

    This SET NX is the ONLY warn-once mechanism. The execution hash used to
    carry a `warned` field as well, written and parsed on every read and
    consulted by nobody; two mechanisms where one is real is how the dead one
    comes to be trusted.
    """
    key = _warn_key(session_id, skill_id, step_id)
    ttl = int(getattr(settings, "PROCEDURE_EXEC_TTL_DAYS", 90)) * 86400
    return bool(await redis_client.set(key, _now(), nx=True, ex=ttl))


async def release_warn(redis_client, *, session_id: str, skill_id: str,
                       step_id: str) -> None:
    """Give back a claim made for an action the gateway then refused.

    The claim is taken while the advisory is built, which is BEFORE the decision
    is final — `decide()` can still escalate a rethink into a block. Keeping the
    claim for an edit that never happened spends the one warn this execution
    gets on a step the agent is about to resubmit, so the resubmission (the edit
    that actually lands) is never warned. Best-effort: a lost release costs one
    advisory, never correctness.
    """
    await redis_client.delete(_warn_key(session_id, skill_id, step_id))


def _warn_key(session_id: str, skill_id: str, step_id: str) -> str:
    return f"{exec_key(session_id, skill_id)}:warned:{step_id}"


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
        out.append(_parse_execution(raw))
    return out


async def write_step_stats(redis_client, settings, skill_id: str, stats: dict) -> None:
    await redis_client.set(f"{_STATS_PREFIX}{skill_id}", json.dumps(stats))
    await redis_client.sadd(_WRITTEN_KEY, skill_id)


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
    await redis_client.sadd(_WRITTEN_KEY, skill_id)


async def list_proposals(redis_client, skill_id: str | None = None) -> list[dict]:
    if skill_id is not None:
        try:
            raw = await redis_client.get(f"{_PROPOSALS_PREFIX}{skill_id}")
            return json.loads(raw) if raw else []
        except Exception:  # noqa: BLE001
            return []
    out: list[dict] = []
    owners = _smap(await redis_client.hgetall(_PROPOSAL_OWNER))
    for sid in sorted(set(owners.values())):
        out.extend(await list_proposals(redis_client, sid))
    return out


async def dismiss_proposal(redis_client, proposal_id: str) -> bool:
    skill_id = _s(await redis_client.hget(_PROPOSAL_OWNER, proposal_id))
    if not skill_id:
        return False
    remaining = [p for p in await list_proposals(redis_client, skill_id)
                 if p.get("id") != proposal_id]
    await redis_client.set(f"{_PROPOSALS_PREFIX}{skill_id}", json.dumps(remaining))
    await redis_client.hdel(_PROPOSAL_OWNER, proposal_id)
    return True


async def written_skills(redis_client) -> set[str]:
    """Skills this pass wrote derived state for, LAST time.

    The stale-reset sweep needs the previous run's set, not this one's: a skill
    that has left the index — deleted, flipped to draft, or had its specs
    removed — can never reappear in the current run's tallies, which is exactly
    why iterating them cannot clear it. Owners of standing proposals are folded
    in so state written before this key existed is still reachable.
    """
    out: set[str] = set()
    try:
        for m in await redis_client.smembers(_WRITTEN_KEY):
            out.add(_s(m))
        owners = _smap(await redis_client.hgetall(_PROPOSAL_OWNER))
        out.update(owners.values())
    except Exception as exc:  # noqa: BLE001
        logger.debug("written-skill set unreadable: %s", exc)
    return {s for s in out if s}


async def clear_skill(redis_client, skill_id: str) -> None:
    """Drop every derived number for a skill that no longer has any."""
    await write_proposals(redis_client, skill_id, [])
    await redis_client.delete(f"{_STATS_PREFIX}{skill_id}")
    await redis_client.delete(f"{_PROPOSALS_PREFIX}{skill_id}")
    await redis_client.srem(_WRITTEN_KEY, skill_id)


async def record_run(redis_client, result: dict) -> None:
    """Persist what the pass did — the DreamState / CollectorState precedent.

    Without it the `tier_b: "insufficient outcome signal"` verdict existed only
    as a Celery return value, so a deployment where the gate is CLOSED (the
    expected state on today's data, per spec F1) was indistinguishable from one
    where it is open and found nothing, with no last_run to say whether the pass
    had ever executed at all.
    """
    try:
        payload = dict(_UNKNOWN_RUN)
        payload.update(result)
        payload["last_run"] = _now()
        await redis_client.set(_RUN_KEY, json.dumps(payload))
    except Exception as exc:  # noqa: BLE001
        logger.warning("procedure run record not written: %s", exc)


async def get_run(redis_client) -> dict:
    try:
        raw = await redis_client.get(_RUN_KEY)
        if not raw:
            return dict(_UNKNOWN_RUN)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return dict(_UNKNOWN_RUN)
        out = dict(_UNKNOWN_RUN)
        out.update(data)
        return out
    except Exception:  # noqa: BLE001
        return dict(_UNKNOWN_RUN)


async def bump_unjoinable(redis_client) -> None:
    """Count an edit that MATCHED a procedure but carried no joinable session.

    Spec §4 Stage 2 says of this drop: "This is counted and surfaced, not
    hidden." Deliberately bumped only after a match, so a deployment with no
    specs still performs no write on the edit path (I5, cold start).
    """
    try:
        await redis_client.incr(_UNJOINABLE_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.debug("unjoinable counter not bumped: %s", exc)


async def get_unjoinable(redis_client) -> int:
    try:
        raw = await redis_client.get(_UNJOINABLE_KEY)
        return int(_s(raw) or 0)
    except Exception:  # noqa: BLE001
        return 0
