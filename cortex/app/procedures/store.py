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

# Round 2 (enforced runbooks) key families — spec 2026-08-15, "Wire contract".
_PENDING_PREFIX = "proc:pending:"      # command evidence awaiting reconcile
_ATTEMPT_PREFIX = "proc:attempt:"      # reconciled-but-not-successful; audit only
_CHALLENGE_PREFIX = "proc:challenge:"  # require_ack rethink receipts
_ACK_PREFIX = "proc:ack:"              # recorded ack reasons (audit)
_PERMIT_PREFIX = "proc:permit:"        # one-use permits, consumed via GETDEL
_MODE_PREFIX = "proc:mode:"            # proc:mode:{workspace}:{skill_id}
_BUNDLE_ACK_PREFIX = "proc:bundle_acks:"  # per-workspace hash: session -> holding
_DEVIATION_PREFIX = "proc:deviations:"  # per-workspace LIST, newest first

# Spec-pinned lifetimes: challenge and permit both live 10 minutes.
CHALLENGE_TTL_SECONDS = 600
PERMIT_TTL_SECONDS = 600
# How long a session's bundle-ack holding is reported. Not spec-pinned; the
# dashboard's "recent sessions lack acks" warning needs a horizon, and 7 days
# covers any session that could still be running.
BUNDLE_ACK_TTL_SECONDS = 7 * 86400

# Ledger depth per workspace — a DISCLOSED cap (the endpoint docs state it):
# the ledger is a triage/audit surface, not an archive, so LTRIM keeps the
# newest 200 and a full ledger reads as "the most recent 200", never as the
# whole history reported as if it were complete.
MAX_DEVIATIONS = 200

ENFORCEMENT_MODES = ("advise", "require_ack", "block")
DEFAULT_MODE = "advise"

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


def exec_key(session_id: str, skill_id: str, workspace_id: str = "") -> str:
    """Execution record key.

    With a verified workspace the key carries it (round-2 tenancy: evidence
    keys gain the workspace dimension). An empty workspace keeps the round-1
    machine-global shape — the path every pre-tenancy caller and test is on.
    Clean break, no migration: records written under the old shape are simply
    not evidence for workspace-scoped lookups (the feature is default-off).
    """
    if workspace_id:
        return f"{_EXEC_PREFIX}{workspace_id}:{session_id}:{skill_id}"
    return f"{_EXEC_PREFIX}{session_id}:{skill_id}"


def workspace_visible(recorded: str, caller: str) -> bool:
    """Is a row recorded under workspace `recorded` visible to `caller`?

    A row with NO recorded workspace belongs to the deployment's own — the
    same rule `workspace_migration.backfill_memories` applies to an
    unattributed Qdrant point. An EMPTY caller sees everything: that is the
    round-1 unscoped path (direct service calls with no verified principal),
    kept so legacy behaviour stays byte-identical where no tenancy exists.
    """
    if not caller:
        return True
    if recorded:
        return recorded == caller
    try:
        from auth.principal import deployment_workspace_id

        return caller == deployment_workspace_id()
    except Exception as exc:  # noqa: BLE001
        logger.debug("deployment workspace unresolved: %s", exc)
        return False


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
            kind = spec.get("kind")
            if kind not in ("file_glob", "command"):
                continue  # unobservable steps are not matchable — but see `order`
            pattern = (spec.get("pattern") or "").strip()
            step_id = spec.get("id")
            if not pattern or not step_id:
                continue
            observable += 1
            entries.append({
                "skill_id": skill["skill_id"],
                "skill_trigger": skill.get("trigger") or "",
                # Round 2: the tenancy dimension the matcher/enforcement path
                # scopes on. Skill points hold it from the verified principal
                # (skills/api.py); decide() now threads the caller's verified
                # workspace, so lookups actually filter on this.
                "workspace_id": skill.get("workspace_id") or "",
                "step_id": step_id,
                "step_text": spec.get("text") or "",
                # Absent on round-1 entries; readers default it to "file_glob".
                "kind": kind,
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
    entries, _ = await load_index_result(redis_client)
    return entries


async def load_index_result(redis_client) -> tuple[list[dict[str, Any]], bool]:
    """(entries, ok) — ok distinguishes ABSENT (a deploy with no runbooks:
    evaluated, nothing to match) from UNREADABLE (Redis down, corrupt JSON:
    nothing was evaluated). The command-enforcement path needs the difference:
    the client's block-mode branch lowers its exit code only on a verdict that
    POSITIVELY evaluated runbooks, and an unreadable index must not produce
    one (external review 2026-08-15: server-internal failure must not convert
    block mode into an authenticated allow). Never raises."""
    try:
        raw = await redis_client.get(INDEX_KEY)
        if not raw:
            return [], True
        data = json.loads(raw)
        return (data, True) if isinstance(data, list) else ([], False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("procedure index unreadable: %s", exc)
        return [], False


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


def _exec_no(raw: dict) -> int:
    """The record's execution number. Records written before round 2 carry
    none; they are execution 1."""
    try:
        return max(1, int(raw.get("execution_no") or 1))
    except (TypeError, ValueError):
        return 1


def effective_execution_no(raw: dict | None) -> int:
    """The execution number the NEXT observation will belong to.

    A closed record's number is spent — the next match opens execution_no+1
    with a fresh evidence scope (spec: execution boundaries). No record at all
    is execution 1.
    """
    if not raw:
        return 1
    n = _exec_no(raw)
    return n + 1 if raw.get("closed_at") else n


async def _archive_closed_execution(redis_client, settings, key: str,
                                    raw: dict) -> None:
    """Move a closed execution's record aside so the next one starts fresh.

    Archived under `{key}:{execution_no}` and kept in the execution index, so
    `iter_executions` (the hardening pass and the receipts endpoint) still
    sees every completed run rather than only the latest."""
    n = _exec_no(raw)
    archive = f"{key}:{n}"
    mapping = {k: v for k, v in raw.items() if v is not None}
    mapping.setdefault("execution_no", str(n))
    await redis_client.hset(archive, mapping=mapping)
    ttl = int(getattr(settings, "PROCEDURE_EXEC_TTL_DAYS", 90)) * 86400
    await redis_client.expire(archive, ttl)
    await redis_client.sadd(_EXEC_INDEX, archive)


async def record_observation(
    redis_client, settings, *, session_id: str, skill_id: str, step_id: str,
    action_id: str, target: str, agent_id: str, adapter: str,
    exec_id: str = "", workspace_id: str = "",
    expected_execution_no: int | None = None, closes_execution: bool = False,
) -> str:
    """Open-or-extend the execution for (workspace, session, skill).

    Returns its exec_id, or "" when `expected_execution_no` names an execution
    that has already ended (a stale command reconcile must not fabricate
    evidence inside a run it was no part of).

    Round-2 boundaries: a record whose terminal step committed successfully is
    CLOSED (`closed_at`); the next observation archives it and opens
    execution_no+1 with a fresh evidence scope. `closes_execution=True` marks
    THIS observation as the terminal-step commit.
    """
    key = exec_key(session_id, skill_id, workspace_id)
    raw = _smap(await redis_client.hgetall(key))
    if raw and raw.get("closed_at"):
        n = effective_execution_no(raw)
        if expected_execution_no is not None and expected_execution_no != n:
            return ""
        stale_exec_id = raw.get("exec_id") or ""
        await _archive_closed_execution(redis_client, settings, key, raw)
        await redis_client.delete(key)
        raw = {}
        # A reopen must not inherit the closed run's exec_id: the id is the
        # advisory's receipt, and quoting a finished run's receipt for fresh
        # evidence would join the two.
        if exec_id and exec_id == stale_exec_id:
            exec_id = ""
        opened_no = n
    elif raw:
        opened_no = _exec_no(raw)
        if expected_execution_no is not None and expected_execution_no != opened_no:
            return ""
    else:
        opened_no = 1
        if expected_execution_no is not None and expected_execution_no != 1:
            return ""

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
            "execution_no": str(opened_no),
            "workspace_id": workspace_id,
        })
        await redis_client.sadd(_EXEC_INDEX, key)
    counts[step_id] = int(counts.get(step_id, 0) or 0) + 1
    receipts = observed.setdefault(step_id, [])
    if len(receipts) < MAX_OBSERVATIONS_PER_STEP:
        receipts.append(
            {"action_id": action_id, "target": target, "ts": _now()}
        )
    mapping = {
        "observed": json.dumps(observed),
        "observed_counts": json.dumps(counts),
        "last_seen_at": _now(),
    }
    if closes_execution:
        mapping["closed_at"] = _now()
    await redis_client.hset(key, mapping=mapping)
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


async def get_execution(redis_client, session_id: str, skill_id: str,
                        workspace_id: str = "") -> dict | None:
    raw = await redis_client.hgetall(exec_key(session_id, skill_id, workspace_id))
    if not raw:
        return None
    return _parse_execution(raw)


async def claim_warn(redis_client, settings, *, session_id: str, skill_id: str,
                     step_id: str, workspace_id: str = "",
                     execution_no: int = 1) -> bool:
    """True exactly once per (execution, step) — the RethinkCounter shape.

    This SET NX is the ONLY warn-once mechanism. The execution hash used to
    carry a `warned` field as well, written and parsed on every read and
    consulted by nobody; two mechanisms where one is real is how the dead one
    comes to be trusted.

    `execution_no` is in the key because "once" is scoped to ONE execution: a
    runbook re-run in the same session (round-2 close-and-reopen) earns its
    warnings afresh.
    """
    key = _warn_key(session_id, skill_id, step_id, workspace_id, execution_no)
    ttl = int(getattr(settings, "PROCEDURE_EXEC_TTL_DAYS", 90)) * 86400
    return bool(await redis_client.set(key, _now(), nx=True, ex=ttl))


async def release_warn(redis_client, *, session_id: str, skill_id: str,
                       step_id: str, workspace_id: str = "",
                       execution_no: int = 1) -> None:
    """Give back a claim made for an action the gateway then refused.

    The claim is taken while the advisory is built, which is BEFORE the decision
    is final — `decide()` can still escalate a rethink into a block. Keeping the
    claim for an edit that never happened spends the one warn this execution
    gets on a step the agent is about to resubmit, so the resubmission (the edit
    that actually lands) is never warned. Best-effort: a lost release costs one
    advisory, never correctness.
    """
    await redis_client.delete(
        _warn_key(session_id, skill_id, step_id, workspace_id, execution_no))


def _warn_key(session_id: str, skill_id: str, step_id: str,
              workspace_id: str = "", execution_no: int = 1) -> str:
    return (f"{exec_key(session_id, skill_id, workspace_id)}"
            f":warned:{execution_no}:{step_id}")


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


# ---------------------------------------------------------------------------
# Round 2 — pending/attempt command evidence (spec: "Pending evidence")
# ---------------------------------------------------------------------------


def pending_key(action_id: str) -> str:
    return f"{_PENDING_PREFIX}{action_id}"


async def write_pending(redis_client, action_id: str, record: dict,
                        ttl_seconds: int) -> None:
    """A command observation that has NOT happened yet. TTL is the gateway's
    reconcile deadline: no reconcile before it means the pending expires and
    satisfies nothing."""
    await redis_client.set(pending_key(action_id), json.dumps(record),
                           ex=max(1, int(ttl_seconds)))


async def take_pending(redis_client, action_id: str) -> dict | None:
    """Read-and-delete the pending record (GETDEL, atomic): exactly one
    reconcile can ever settle one action."""
    try:
        raw = await redis_client.getdel(pending_key(action_id))
        if not raw:
            return None
        data = json.loads(_s(raw))
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("pending observation unreadable for %s: %s", action_id, exc)
        return None


async def record_attempt(redis_client, settings, action_id: str,
                         record: dict) -> None:
    """A reconciled-but-unsuccessful (or exit-status-less) command. Retained
    for the ledger/audit; satisfies NOTHING."""
    try:
        ttl = int(getattr(settings, "PROCEDURE_EXEC_TTL_DAYS", 90)) * 86400
        await redis_client.set(f"{_ATTEMPT_PREFIX}{action_id}",
                               json.dumps(record), ex=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.debug("attempt not recorded for %s: %s", action_id, exc)


async def get_attempt(redis_client, action_id: str) -> dict | None:
    try:
        raw = await redis_client.get(f"{_ATTEMPT_PREFIX}{action_id}")
        if not raw:
            return None
        data = json.loads(_s(raw))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Round 2 — challenge / ack / permit (spec: "Permit protocol")
# ---------------------------------------------------------------------------


async def mint_challenge(redis_client, challenge_id: str, record: dict) -> None:
    """Write (or refresh) a require_ack challenge. Refreshing on every
    re-challenge is deliberate: the id is deterministic over the bound tuple,
    so the retry loop converges on one challenge rather than minting a pile."""
    await redis_client.set(f"{_CHALLENGE_PREFIX}{challenge_id}",
                           json.dumps(record), ex=CHALLENGE_TTL_SECONDS)


async def get_challenge(redis_client, challenge_id: str) -> dict | None:
    try:
        raw = await redis_client.get(f"{_CHALLENGE_PREFIX}{challenge_id}")
        if not raw:
            return None
        data = json.loads(_s(raw))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


async def record_ack(redis_client, settings, challenge_id: str,
                     record: dict) -> None:
    """The audit half of an ack: who accepted responsibility, and why.

    The future deviation ledger reads these; losing one costs an audit row,
    never a decision, so this is best-effort."""
    try:
        ttl = int(getattr(settings, "PROCEDURE_EXEC_TTL_DAYS", 90)) * 86400
        await redis_client.set(f"{_ACK_PREFIX}{challenge_id}",
                               json.dumps(record), ex=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ack not recorded for %s: %s", challenge_id, exc)


async def mint_permit(redis_client, challenge_id: str, record: dict) -> None:
    await redis_client.set(f"{_PERMIT_PREFIX}{challenge_id}",
                           json.dumps(record), ex=PERMIT_TTL_SECONDS)


async def consume_permit(redis_client, challenge_id: str) -> dict | None:
    """Atomic GETDEL: a permit authorises exactly one command, ever.

    The caller still verifies the bound tuple against the live request; a
    mismatched permit stays consumed (destroyed), which fails toward
    re-challenge — the safe side."""
    try:
        raw = await redis_client.getdel(f"{_PERMIT_PREFIX}{challenge_id}")
        if not raw:
            return None
        data = json.loads(_s(raw))
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("permit unreadable for %s: %s", challenge_id, exc)
        return None


# ---------------------------------------------------------------------------
# Round 2 — enforcement modes (spec: "Modes")
# ---------------------------------------------------------------------------


def mode_key(workspace_id: str, skill_id: str) -> str:
    return f"{_MODE_PREFIX}{workspace_id}:{skill_id}"


async def get_mode(redis_client, workspace_id: str, skill_id: str) -> dict:
    """{mode, set_by, set_at}; default advise. NEVER raises — a mode read sits
    on the blocking pre-tool path, and an unreadable mode must degrade to the
    least-forceful posture, not to an exception (and not to block)."""
    fallback = {"mode": DEFAULT_MODE, "set_by": "", "set_at": ""}
    try:
        raw = await redis_client.get(mode_key(workspace_id, skill_id))
        if not raw:
            return fallback
        data = json.loads(_s(raw))
        if (not isinstance(data, dict)
                or data.get("mode") not in ENFORCEMENT_MODES):
            return fallback
        return {"mode": data["mode"], "set_by": _s(data.get("set_by") or ""),
                "set_at": _s(data.get("set_at") or "")}
    except Exception as exc:  # noqa: BLE001
        logger.debug("mode unreadable for %s/%s: %s", workspace_id, skill_id, exc)
        return fallback


async def set_mode(redis_client, workspace_id: str, skill_id: str, mode: str,
                   set_by: str) -> dict:
    """Human-set, admin-gated (the router enforces the scope). The skill PATCH
    path cannot reach this key: agents may propose runbooks, never arm them."""
    if mode not in ENFORCEMENT_MODES:
        raise ValueError(f"mode must be one of {ENFORCEMENT_MODES}")
    record = {"mode": mode, "set_by": set_by, "set_at": _now()}
    await redis_client.set(mode_key(workspace_id, skill_id), json.dumps(record))
    return record


# ---------------------------------------------------------------------------
# Round 2 — bundle acks (spec: "Bundle")
# ---------------------------------------------------------------------------


async def record_bundle_ack(redis_client, workspace_id: str, session_id: str,
                            version: str) -> None:
    """Which sessions hold which bundle version. Coverage is REPORTED, never
    assumed: the server enforces whatever reaches it regardless of acks."""
    key = f"{_BUNDLE_ACK_PREFIX}{workspace_id}"
    await redis_client.hset(key, session_id, json.dumps(
        {"version": version, "at": _now()}))
    await redis_client.expire(key, BUNDLE_ACK_TTL_SECONDS)


async def list_bundle_acks(redis_client, workspace_id: str) -> dict[str, dict]:
    try:
        raw = _smap(await redis_client.hgetall(f"{_BUNDLE_ACK_PREFIX}{workspace_id}"))
        out: dict[str, dict] = {}
        for session_id, blob in raw.items():
            try:
                data = json.loads(blob)
                if isinstance(data, dict):
                    out[session_id] = data
            except (TypeError, ValueError):
                continue
        return out
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Phase C — the deviation ledger (spec: "Deviation ledger")
# ---------------------------------------------------------------------------


async def record_deviation(redis_client, settings, workspace_id: str,
                           record: dict) -> None:
    """Append one deviation (block / ack / failed_attempt) to the workspace's
    ledger, newest first. Records carry the COMMAND HASH, never the command
    text — pending records already deliberately omit it (secrets). Losing one
    costs an audit row, never a decision, so this never raises."""
    try:
        key = f"{_DEVIATION_PREFIX}{workspace_id}"
        record.setdefault("at", _now())
        await redis_client.lpush(key, json.dumps(record))
        await redis_client.ltrim(key, 0, MAX_DEVIATIONS - 1)
        # Same horizon as the execution records the deviations annotate,
        # refreshed on every write.
        ttl = int(getattr(settings, "PROCEDURE_EXEC_TTL_DAYS", 90)) * 86400
        await redis_client.expire(key, ttl)
    except Exception as exc:  # noqa: BLE001
        logger.debug("deviation not recorded for %s: %s", workspace_id, exc)


async def list_deviations(redis_client, workspace_id: str,
                          limit: int = 50) -> list[dict]:
    """Newest-first ledger read. Never raises; an undecodable or non-dict
    entry is skipped rather than failing the whole read."""
    try:
        n = int(limit)
        if n <= 0:
            return []
        raw = await redis_client.lrange(
            f"{_DEVIATION_PREFIX}{workspace_id}", 0, n - 1)
        out: list[dict] = []
        for item in raw or []:
            try:
                data = json.loads(_s(item))
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                out.append(data)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("deviation ledger unreadable for %s: %s",
                     workspace_id, exc)
        return []
