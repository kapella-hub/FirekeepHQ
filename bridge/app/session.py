"""Session management — CRUD, state transitions, Redis transactions."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import redis
import redis.asyncio as aioredis

from app.config import Settings

logger = logging.getLogger(__name__)

# Recognized task-completion grades (outcome truth, PR1). Bridge is the
# storage layer of record for this tuple — mcp_server.py imports it rather
# than redefining it, so "what counts as a grade" has exactly one home.
TASK_RESULTS = ("success", "partial", "failure")

# complete_session's WATCH/MULTI CAS loop (below) retries this many times on
# WatchError before giving up. Contention here means another authorized
# completer committed between our read and our EXEC — a few retries resolve
# it; exhaustion after 8 means something is pathologically hot, not a normal
# race.
_COMPLETE_CAS_RETRIES = 8

# --------------------------------------------------------------------------
# Replay emitter (best-effort, mirrors cortex/app/main.py:_replay_emit)
# --------------------------------------------------------------------------

_replay_initialized = False


async def _ensure_replay() -> None:
    global _replay_initialized
    if _replay_initialized:
        return
    _replay_initialized = True
    try:
        from replay.emitter import init_emitter
        await init_emitter()
        logger.info("Replay emitter initialized for Bridge")
    except Exception as exc:
        logger.warning("Replay emitter init FAILED: %s", exc)


async def _replay_emit(
    event_type: str,
    session_id: str,
    agent_id: str,
    payload: dict,
    **kwargs,
) -> None:
    try:
        await _ensure_replay()
        from replay.emitter import emit
        await emit(event_type, session_id, agent_id, payload, **kwargs)
    except Exception as exc:
        logger.warning("Replay emit failed for %s: %s", event_type, exc)


# --------------------------------------------------------------------------
# Lua scripts for atomic operations
# --------------------------------------------------------------------------

START_SESSION_LUA = """
local active = redis.call('GET', KEYS[1])
if active and active ~= '' then
    redis.call('HSET', ARGV[4] .. active, 'status', 'paused', 'updated_at', ARGV[1])
    redis.call('ZADD', KEYS[2], ARGV[2], active)
end
redis.call('SET', KEYS[1], ARGV[3])
return active or ''
"""

RESUME_SESSION_LUA = """
local active = redis.call('GET', KEYS[1])
if active and active ~= '' and active ~= ARGV[1] then
    redis.call('HSET', ARGV[3] .. active, 'status', 'paused', 'updated_at', ARGV[2])
    redis.call('ZADD', KEYS[2], ARGV[4], active)
end
-- KEYS[3] is the PREVIOUS owner's active pointer (empty string when the
-- resuming agent already owns the session). A takeover that leaves it in
-- place gives two agents one session: both ctx_get_shadow(agent=old) and
-- ctx_get_shadow(agent=new) resolve to it, and the old owner's later
-- ctx_update writes land in a session it no longer owns. Clearing it inside
-- the same script is what makes the pointer swap atomic with the resume.
if KEYS[3] ~= '' and KEYS[3] ~= KEYS[1] then
    local prev = redis.call('GET', KEYS[3])
    if prev == ARGV[1] then
        redis.call('DEL', KEYS[3])
    end
end
redis.call('SET', KEYS[1], ARGV[1])
return active or ''
"""

COMPLETE_SESSION_LUA = """
local active = redis.call('GET', KEYS[1])
if active == ARGV[1] then
    redis.call('DEL', KEYS[1])
end
return 1
"""


class SessionManager:
    """Manages session lifecycle in Redis with atomic state transitions."""

    def __init__(self, redis: aioredis.Redis, settings: Settings) -> None:
        self._r = redis
        self._s = settings

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _session_key(sid: str) -> str:
        return f"nb:session:{sid}"

    @staticmethod
    def _active_key(agent_id: str) -> str:
        return f"nb:active:{agent_id}"

    @staticmethod
    def _plan_key(sid: str) -> str:
        return f"nb:session:{sid}:plan"

    @staticmethod
    def _decisions_key(sid: str) -> str:
        return f"nb:session:{sid}:decisions"

    @staticmethod
    def _files_key(sid: str) -> str:
        return f"nb:session:{sid}:files"

    @staticmethod
    def _progress_key(sid: str) -> str:
        return f"nb:session:{sid}:progress"

    @staticmethod
    def _scratch_key(sid: str) -> str:
        return f"nb:session:{sid}:scratch"

    @staticmethod
    def _proactive_key(sid: str) -> str:
        return f"nb:session:{sid}:proactive"

    INDEX_KEY = "nb:sessions"

    async def _stale_active_pointers(
        self, session_id: str, *agent_ids: str | None
    ) -> list[str]:
        """Active-pointer keys that name *session_id* and must be cleared.

        Completion used to clear the pointer of ``meta["agent_id"]`` and no
        one else, so a session that had changed hands left the OTHER agent
        pointing at a finished session — and that dangling pointer is what
        made ``ctx_update`` resolve to a completed session and silently drop
        the write. Checking every agent involved in the call (owner AND
        caller) is what makes "completing a session releases it" true rather
        than true-for-one-agent.
        """
        keys: list[str] = []
        for agent_id in dict.fromkeys(a for a in agent_ids if a):
            key = self._active_key(agent_id)
            if key in keys:
                continue
            if await self._r.get(key) == session_id:
                keys.append(key)
        return keys

    def _all_session_keys(self, sid: str) -> list[str]:
        return [
            self._session_key(sid),
            self._plan_key(sid),
            self._decisions_key(sid),
            self._files_key(sid),
            self._progress_key(sid),
            self._scratch_key(sid),
            self._proactive_key(sid),
        ]

    # ------------------------------------------------------------------
    # _meta_key alias (used for collision check)
    # ------------------------------------------------------------------

    _meta_key = _session_key

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    async def start_session(
        self,
        goal: str,
        agent_id: str | None = None,
        tags: list[str] | None = None,
        project: str | None = None,
        briefing_id: str | None = None,
        runtime: str | None = None,
        client_version: str | None = None,
        instr_rendered: str | None = None,
        instr_expected: str | None = None,
        instr_gateway: str | None = None,
        owner_member: str | None = None,
    ) -> dict[str, str]:
        agent_id = agent_id or self._s.DEFAULT_AGENT_ID
        now = datetime.now(timezone.utc).isoformat()
        ts = datetime.now(timezone.utc).timestamp()

        # Generate unique session ID with collision check (fix #7)
        session_id = await self._generate_unique_session_id()

        # Atomically pause any active session and set new active (fix #1)
        active_key = self._active_key(agent_id)
        await self._r.eval(
            START_SESSION_LUA,
            2,
            active_key,
            self.INDEX_KEY,
            now,   # ARGV[1]: updated_at for paused session
            ts,    # ARGV[2]: score for zadd
            session_id,  # ARGV[3]: new session id to set as active
            "nb:session:",  # ARGV[4]: session key prefix for pausing previous active
        )

        # Create new session metadata
        await self._r.hset(self._session_key(session_id), mapping={
            "goal": goal,
            "status": "active",
            "agent_id": agent_id,
            # Written ONCE, here, and never again (outcome truth, PR1). This is
            # the verified-member binding complete_session and resume_session
            # check before honoring a grade or a cross-member takeover; no
            # other code path may write this field.
            "owner_member": owner_member or "",
            "project": project or "",
            "briefing_id": briefing_id or "",
            # Living Instructions round 2 attribution (the five X-Firekeep-*
            # headers, read by ctx_start_session). Absent → "" — the
            # briefing_id precedent; an unattributed session is a normal
            # state, never an error.
            "runtime": runtime or "",
            "client_version": client_version or "",
            "instr_rendered": instr_rendered or "",
            "instr_expected": instr_expected or "",
            "instr_gateway": instr_gateway or "",
            "created_at": now,
            "updated_at": now,
            "tags": json.dumps(tags or []),
            "outcome": "",
            "distillation": "",
        })
        await self._r.set(active_key, session_id)
        await self._r.zadd(self.INDEX_KEY, {session_id: ts})

        # Enforce MAX_SESSIONS
        await self._enforce_max_sessions()

        await _replay_emit(
            event_type="session.started",
            session_id=session_id,
            agent_id=agent_id,
            payload={"goal": (goal or "")[:200]},
        )

        return {"session_id": session_id, "created_at": now}

    async def _generate_unique_session_id(self) -> str:
        """Generate a truncated UUID, retrying on collision (fix #7)."""
        for _ in range(3):
            session_id = str(uuid.uuid4())[:12]
            if not await self._r.exists(self._meta_key(session_id)):
                return session_id
        raise RuntimeError("Failed to generate unique session ID")

    # ------------------------------------------------------------------
    # Update components
    # ------------------------------------------------------------------

    async def update(
        self,
        category: str,
        content: str,
        key: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Write one component into a session.

        ``session_id`` names the target session directly — the MCP layer
        threads the connection's X-Session-Id header through it, so a terminal
        writes into ITS OWN session even when a sibling terminal sharing the
        same agent_id has since re-pointed the shared nb:active:{agent_id}
        pointer. When it is None (header-less clients), resolution falls back
        to the active pointer exactly as before.
        """
        agent_id = agent_id or self._s.DEFAULT_AGENT_ID
        sid = session_id or await self._r.get(self._active_key(agent_id))
        if not sid:
            raise ValueError("No active session")

        # A write into a finished session is a LOST write, and it used to
        # report {"status": "ok"}. This method resolved the target from the
        # agent's active pointer and dispatched on category with no status
        # check at all, so an agent whose pointer still named a completed
        # session wrote entries into it, saw them in ctx_get_shadow, and lost
        # every one of them — distillation ran at completion and never runs
        # again. Refusing here mirrors resume_session's own
        # "Cannot resume completed session". The status read is one HGET on a
        # hash this method already writes to at the end.
        status = await self._r.hget(self._session_key(sid), "status")
        if status in ("completed", "abandoned"):
            raise ValueError(
                f"Cannot update {status} session {sid} — its memory was already "
                f"distilled. Start a new session with ctx_start_session."
            )
        # A NAMED session that does not EXIST must refuse, not materialize.
        # Scoped to the explicitly-named path (param or threaded header): a
        # client-supplied name is untrusted input that can outlive a
        # reaped-then-expired session (the client stash lives 12h; the shim's
        # in-memory id has no TTL), and without this check the write below
        # creates a ghost meta hash holding only updated_at — no status, no
        # owner, no TTL — and reports ok for a write that was lost the moment
        # it landed (external review 2026-08-12). The pointer path is
        # server-owned state: its dangling case predates this guard and the
        # pointer is deleted on completion, so it is not newly reachable.
        if (
            session_id is not None
            and status is None
            and not await self._r.exists(self._session_key(sid))
        ):
            raise ValueError(
                f"Unknown session {sid} — it may have expired. Start a new "
                f"session with ctx_start_session."
            )

        now = datetime.now(timezone.utc).isoformat()
        count = 0

        if category == "plan":
            if len(content.encode("utf-8")) > self._s.PLAN_MAX_BYTES:
                raise ValueError(f"Plan exceeds {self._s.PLAN_MAX_BYTES} bytes")
            await self._r.set(self._plan_key(sid), content)
            count = 1

        elif category == "decision":
            entry = json.dumps({"timestamp": now, "content": content})
            await self._r.lpush(self._decisions_key(sid), entry)
            await self._r.ltrim(self._decisions_key(sid), 0, self._s.DECISIONS_MAX - 1)
            count = await self._r.llen(self._decisions_key(sid))

        elif category == "file":
            if not key:
                raise ValueError("'key' (file path) is required for file updates")
            entry = json.dumps({"summary": content, "last_action": now})
            await self._r.hset(self._files_key(sid), key, entry)
            count = await self._r.hlen(self._files_key(sid))
            if count > self._s.FILES_MAX:
                all_fields = await self._r.hkeys(self._files_key(sid))
                to_remove = all_fields[: count - self._s.FILES_MAX]
                if to_remove:
                    await self._r.hdel(self._files_key(sid), *to_remove)
                    count = await self._r.hlen(self._files_key(sid))

        elif category == "progress":
            entry = json.dumps({"timestamp": now, "content": content})
            await self._r.lpush(self._progress_key(sid), entry)
            await self._r.ltrim(self._progress_key(sid), 0, self._s.PROGRESS_MAX - 1)
            count = await self._r.llen(self._progress_key(sid))

        elif category == "scratch":
            if not key:
                raise ValueError("'key' is required for scratch updates")
            await self._r.hset(self._scratch_key(sid), key, content)
            count = await self._r.hlen(self._scratch_key(sid))
            if count > self._s.SCRATCH_MAX:
                all_fields = await self._r.hkeys(self._scratch_key(sid))
                to_remove = all_fields[: count - self._s.SCRATCH_MAX]
                if to_remove:
                    await self._r.hdel(self._scratch_key(sid), *to_remove)
                    count = await self._r.hlen(self._scratch_key(sid))

        else:
            raise ValueError(f"Unknown category: {category}")

        # Update timestamp
        await self._r.hset(self._session_key(sid), "updated_at", now)
        await self._r.zadd(self.INDEX_KEY, {sid: datetime.now(timezone.utc).timestamp()})

        update_payload: dict[str, Any] = {
            "category": category,
            "content": (content or "")[:200],
        }
        if key:
            update_payload["key"] = key
        await _replay_emit(
            event_type="session.updated",
            session_id=sid,
            agent_id=agent_id,
            payload=update_payload,
        )

        return {"status": "ok", "component_count": count}

    # ------------------------------------------------------------------
    # Get session data (for shadow assembly)
    # ------------------------------------------------------------------

    async def get_session_data(self, session_id: str) -> dict[str, Any] | None:
        meta = await self._r.hgetall(self._session_key(session_id))
        if not meta:
            return None
        plan = await self._r.get(self._plan_key(session_id)) or ""
        decisions_raw = await self._r.lrange(self._decisions_key(session_id), 0, -1)
        files_raw = await self._r.hgetall(self._files_key(session_id))
        progress_raw = await self._r.lrange(self._progress_key(session_id), 0, -1)
        scratch = await self._r.hgetall(self._scratch_key(session_id))

        decisions = [json.loads(d) for d in reversed(decisions_raw)]
        progress = [json.loads(p) for p in reversed(progress_raw)]
        files = {k: json.loads(v) for k, v in files_raw.items()}

        proactive_raw = await self._r.get(self._proactive_key(session_id))
        proactive_memories = json.loads(proactive_raw) if proactive_raw else []

        return {
            **meta,
            "tags": json.loads(meta.get("tags", "[]")),
            # task_result / task_result_source stay as the plain strings
            # already in meta (possibly "") — only task_evidence is stored
            # JSON-encoded and needs decoding.
            "task_evidence": json.loads(meta.get("task_evidence") or "[]"),
            "plan": plan,
            "decisions": decisions,
            "files": files,
            "progress": progress,
            "scratch": scratch,
            "proactive_memories": proactive_memories,
        }

    async def get_shadow_epoch(self, session_id: str) -> str | None:
        """The session's shadow epoch. "" means never bumped; None means the read FAILED.

        The distinction is load-bearing (C2). "" is a real, matchable state — every
        cursor minted before the first compaction carries it. If a read failure also
        returned "", an errored read would silently match a stale cursor and serve a
        delta to an agent that had just lost its context. None is unmatchable.

        precompact writes this via ctx_update(category="scratch", key="shadow_epoch"),
        so there is no dedicated writer and no new MCP tool.
        """
        try:
            value = await self._r.hget(self._scratch_key(session_id), "shadow_epoch")
        except Exception:
            return None          # could not read -> unmatchable -> full restore
        return value or ""

    async def get_active_session_id(self, agent_id: str | None = None) -> str | None:
        agent_id = agent_id or self._s.DEFAULT_AGENT_ID
        return await self._r.get(self._active_key(agent_id))

    # ------------------------------------------------------------------
    # Complete (WATCH/MULTI CAS — read inside the watched section)
    # ------------------------------------------------------------------

    async def complete_session(
        self,
        session_id: str | None = None,
        outcome: str | None = None,
        agent_id: str | None = None,
        *,
        task_result: str | None = None,
        task_evidence: list | None = None,
        verified_member: str | None = None,
    ) -> dict[str, Any]:
        """Complete a session and return the session's AUTHORITATIVE grade.

        Outcome truth (PR1): a session's grade means the TASK succeeded, not
        that this RPC succeeded, so a completion attempt must not be able to
        stomp a grade that is already on record, and a caller must not be
        able to grade — or take over — a session it does not verifiably own.

        OWNERSHIP IS STILL CHECKED first (label match, mirroring
        resume_session — see that method's docstring for the live
        cross-terminal clobber this originally fixed; the reaper is
        unaffected, it passes the session's own owner as agent_id). ON TOP of
        that, a session with a non-empty ``owner_member`` (set once, in
        start_session) refuses completion by anyone whose verified_member
        does not match — this is what makes the grade authoritative: only the
        verified owner's FIRST self-reported grade is ever stored, and no one
        else can overwrite or launder a different one in.

        The label check, the owner_member check, the existing-grade read and
        the active-pointer staleness decision all happen INSIDE one
        WATCH/MULTI transaction, re-run on every retry:
          - Reading meta via ``pipe.hgetall`` (not ``self._r.hgetall``) after
            ``pipe.watch(session_key)`` is what makes the eventual EXEC fail
            if another completion commits first — the fix #2 "read outside
            the pipeline" pattern this method used to follow is exactly
            backwards for a CAS; here the read has to be INSIDE the watched
            section or two racing completions could each believe they own the
            first-grade write.
          - The active-pointer keys are watched and read the same way
            (``pipe.watch`` + ``pipe.mget``, not a pre-pipeline
            ``self._r.get`` loop) so a concurrent start/resume that repoints
            one of them between our read and our EXEC aborts our transaction
            instead of letting a stale DEL delete the NEW session's pointer.
          - The distill enqueue rides the SAME MULTI as the "queued" state
            commit and the first grade write (final-review fix, unchanged
            from before this PR): a crash between a separate state-write and
            enqueue used to be able to strand a session at
            distillation="queued" forever.

        First grade wins: existing_grade is authoritative only when
        owner_member is non-empty, the stored task_result is a recognized
        TASK_RESULTS value, AND task_result_source == "self_reported" — an
        unbound session's stray task_result pair, or a sourceless one, is
        never treated as authority, and a bound session with a real grade
        keeps it (the submitted grade is silently dropped, not stored,
        reported back only via task_result_dropped).

        Retry exhaustion (_COMPLETE_CAS_RETRIES attempts, all WatchError)
        raises RuntimeError before any emit or enqueue — no half-committed
        state.

        This guarantees one authoritative grade and safe pointer cleanup, NOT
        exactly-once side effects: two authorized callers can each eventually
        commit after a WatchError retry, each queueing a distill job / firing
        a session.completed event with the same authoritative grade. That
        at-least-once behavior predates this PR (D13) and is not expanded
        into an outbox here.
        """
        agent_id = agent_id or self._s.DEFAULT_AGENT_ID
        if session_id is None:
            session_id = await self._r.get(self._active_key(agent_id))
        if not session_id:
            raise ValueError("No active session")

        # Final-review fix: the distill job is XADD'd inside the SAME
        # transaction pipeline as the "queued" state commit. Previously the
        # enqueue happened as a separate call after the pipeline committed —
        # a crash between the two left the session pinned at
        # distillation="queued" forever: no queue entry, no TTL, and excluded
        # from _enforce_max_sessions cleanup. Field shape mirrors
        # distill_worker.enqueue_distillation exactly.
        from app.distill_worker import QUEUE_KEY  # lazy: avoids import cycle

        now = datetime.now(timezone.utc).isoformat()
        submitted_grade = task_result if task_result in TASK_RESULTS else None
        session_key = self._session_key(session_id)
        label_owner = ""
        owner_member = ""
        write_grade = False
        authoritative_grade: str | None = None

        for _attempt in range(_COMPLETE_CAS_RETRIES):
            try:
                async with self._r.pipeline(transaction=True) as pipe:
                    await pipe.watch(session_key)
                    meta = await pipe.hgetall(session_key)
                    if not meta:
                        await pipe.unwatch()
                        raise ValueError(f"Session {session_id} not found")

                    label_owner = meta.get("agent_id") or ""
                    if label_owner and label_owner != agent_id:
                        await pipe.unwatch()
                        raise ValueError(
                            f"Session {session_id} belongs to agent '{label_owner}'"
                        )

                    owner_member = meta.get("owner_member") or ""
                    if owner_member and verified_member != owner_member:
                        await pipe.unwatch()
                        raise ValueError(
                            f"Session {session_id} belongs to a different "
                            f"verified owner"
                        )

                    candidate = submitted_grade if owner_member else None
                    existing_grade = meta.get("task_result") or ""
                    if (
                        not owner_member
                        or existing_grade not in TASK_RESULTS
                        or meta.get("task_result_source") != "self_reported"
                    ):
                        existing_grade = ""
                    write_grade = bool(candidate) and not existing_grade
                    authoritative_grade = (
                        candidate if write_grade else existing_grade or None
                    )

                    pointer_keys = [
                        self._active_key(a)
                        for a in dict.fromkeys(
                            a for a in (label_owner, agent_id) if a)
                    ]
                    if pointer_keys:
                        # A start/resume may repoint one after this read.
                        # WATCHing it makes that invalidate EXEC instead of
                        # letting our stale transaction delete the new
                        # session's pointer.
                        await pipe.watch(*pointer_keys)
                        pointer_values = await pipe.mget(pointer_keys)
                        stale_pointers = [
                            key for key, value in zip(
                                pointer_keys, pointer_values, strict=True)
                            if value == session_id
                        ]
                    else:
                        stale_pointers = []

                    mapping = {
                        "status": "completed",
                        "updated_at": now,
                        "outcome": outcome or "",
                        "distillation": "queued",
                    }
                    if write_grade:
                        mapping.update({
                            "task_result": candidate,
                            "task_result_source": "self_reported",
                            "task_evidence": json.dumps(task_evidence or []),
                        })
                    pipe.multi()
                    pipe.hset(session_key, mapping=mapping)
                    for key in stale_pointers:
                        pipe.delete(key)
                    pipe.xadd(QUEUE_KEY, {
                        "session_id": session_id,
                        "attempts": "0",
                        "next_attempt_at": str(time.time()),
                    })
                    await pipe.execute()
                    # State/grade correctness, not exactly-once effects:
                    # another authorized completion may retry and also
                    # commit, producing a duplicate queue row/event with this
                    # same authoritative grade (D13).
                    break
            except redis.WatchError:
                continue
        else:
            raise RuntimeError(
                f"Session {session_id} completion contended repeatedly; retry"
            )

        # D1: TTL is deliberately NOT set here. The distill worker applies the
        # 7-day TTL only after confirmed distillation success or DLQ move —
        # a failing distillation must never let the session keys expire first.

        completed_payload: dict[str, Any] = {"outcome": outcome or ""}
        if authoritative_grade:
            completed_payload.update({
                "task_result": authoritative_grade,
                "task_result_source": "self_reported",
            })
        await _replay_emit(
            event_type="session.completed",
            session_id=session_id,
            agent_id=label_owner or "unknown",
            payload=completed_payload,
        )

        result: dict[str, Any] = {
            "status": "completed",
            "session_id": session_id,
            "task_result": authoritative_grade,
            "task_result_source": "self_reported" if authoritative_grade else None,
        }
        if submitted_grade and not owner_member:
            result["task_result_dropped"] = (
                "pre-upgrade session has no verified owner binding"
            )
        elif submitted_grade and not write_grade:
            result["task_result_dropped"] = "session already has a stored grade"
        return result

    # ------------------------------------------------------------------
    # Abandon (fix #2 — read outside pipeline)
    # ------------------------------------------------------------------

    async def abandon_session(
        self, session_id: str | None = None, agent_id: str | None = None
    ) -> dict[str, str]:
        agent_id = agent_id or self._s.DEFAULT_AGENT_ID
        if session_id is None:
            session_id = await self._r.get(self._active_key(agent_id))
        if not session_id:
            raise ValueError("No active session")

        meta = await self._r.hgetall(self._session_key(session_id))
        if not meta:
            raise ValueError(f"Session {session_id} not found")

        # Same ownership refusal as complete_session above — abandonment is
        # equally terminal (and skips distillation entirely, so a cross-agent
        # abandon destroys MORE than a cross-agent complete). The reaper
        # passes the session's own owner and is unaffected (app/reaper.py).
        owner = meta.get("agent_id") or ""
        if owner and owner != agent_id:
            raise ValueError(
                f"Session {session_id} belongs to agent '{owner}' — refusing "
                f"abandonment by '{agent_id}'."
            )

        now = datetime.now(timezone.utc).isoformat()
        ttl_seconds = self._s.SESSION_TTL_DAYS * 86400

        # Read current active OUTSIDE the pipeline (fix #2)
        stale_pointers = await self._stale_active_pointers(
            session_id, meta.get("agent_id"), agent_id
        )

        async with self._r.pipeline(transaction=True) as pipe:
            pipe.hset(self._session_key(session_id), mapping={
                "status": "abandoned",
                "updated_at": now,
            })
            for key in stale_pointers:
                pipe.delete(key)
            for key in self._all_session_keys(session_id):
                pipe.expire(key, ttl_seconds)
            await pipe.execute()

        await _replay_emit(
            event_type="session.abandoned",
            session_id=session_id,
            agent_id=meta.get("agent_id") or "unknown",
            payload={},
        )

        return {"status": "abandoned", "session_id": session_id}

    # ------------------------------------------------------------------
    # Resume (fix #1 — Lua script for atomicity)
    # ------------------------------------------------------------------

    async def resume_session(
        self,
        session_id: str,
        agent_id: str | None = None,
        *,
        takeover: bool = False,
        verified_member: str | None = None,
    ) -> dict[str, Any]:
        """Resume a session and make it the caller's active one.

        BOUND-OWNER REFUSAL (outcome truth, PR1): a session with a non-empty
        ``owner_member`` (set once, in start_session) refuses ANY resume by a
        caller whose ``verified_member`` does not match — even an explicit
        ``takeover=True``. This runs immediately after the ``hgetall``,
        before the status check and before any Lua/HSET/pointer effect, so a
        cross-member attempt mutates nothing. A SAME-member caller may still
        take over the agent LABEL (``agent_id``) without this touching
        ``owner_member`` — the binding is per-member, not per-label. A
        session whose meta has no ``owner_member`` (legacy, pre-upgrade)
        skips this check entirely and falls through to the label-only
        behavior below, unchanged.

        OWNERSHIP IS CHECKED (audit finding). This method previously read the
        session's status and nothing else, then unconditionally rewrote
        ``agent_id`` and pointed the CALLER's active key at it — so any agent
        that knew a session id owned that session, and ``ctx_list_sessions()``
        with no filter returns every agent's session id on a deployment. The
        consequences compounded: the victim's ``nb:active:<agent>`` pointer was
        never cleared (two agents sharing one session), an ACTIVE session was
        stolen even though the tool documents itself as resuming a PAUSED one,
        and the memory distilled at completion was attributed to the thief.

        ``complete_session`` and ``abandon_session`` above enforce the same
        refusal. (This paragraph once claimed complete_session ALREADY did —
        it did not, and that gap was the enabling half of a live
        cross-terminal clobber on 2026-08-11: two terminals sharing one
        agent_id share one nb:active pointer, and a no-arg
        ctx_complete_session from terminal B resolved via the shared pointer
        and completed terminal A's in-flight session. The checks are real
        now, added 2026-08-12 together with X-Session-Id header precedence in
        app/mcp_server.py.)

        ``takeover=True`` is the explicit hand-off: it still clears the prior
        owner's pointer (inside RESUME_SESSION_LUA, atomically), so a takeover
        transfers the session rather than sharing it.
        """
        agent_id = agent_id or self._s.DEFAULT_AGENT_ID
        meta = await self._r.hgetall(self._session_key(session_id))
        if not meta:
            raise ValueError(f"Session {session_id} not found")

        owner_member = meta.get("owner_member") or ""
        if owner_member and verified_member != owner_member:
            raise ValueError(
                f"Session {session_id} belongs to a different verified owner"
            )

        if meta.get("status") in ("completed", "abandoned"):
            raise ValueError(f"Cannot resume {meta['status']} session")

        owner = meta.get("agent_id") or ""
        if owner and owner != agent_id and not takeover:
            raise ValueError(
                f"Session {session_id} belongs to agent '{owner}'. "
                f"Pass takeover=True to take it over explicitly."
            )
        if owner and owner != agent_id and meta.get("status") == "active":
            # Even an explicit takeover refuses a session someone is actively
            # working in. "Resume" means picking up work that stopped; there is
            # no version of that which involves evicting a live agent.
            raise ValueError(
                f"Session {session_id} is ACTIVE for agent '{owner}'. "
                f"It must be paused, completed or abandoned before another "
                f"agent can take it."
            )

        now = datetime.now(timezone.utc).isoformat()
        ts = datetime.now(timezone.utc).timestamp()

        # Atomically pause current active, clear the PREVIOUS owner's pointer,
        # and set new active (fix #1 + takeover fix)
        active_key = self._active_key(agent_id)
        prev_owner_key = (
            self._active_key(owner) if owner and owner != agent_id else ""
        )
        await self._r.eval(
            RESUME_SESSION_LUA,
            3,
            active_key,
            self.INDEX_KEY,
            prev_owner_key,  # KEYS[3]: previous owner's active pointer ('' = none)
            session_id,    # ARGV[1]: session to resume
            now,           # ARGV[2]: updated_at for paused session
            "nb:session:", # ARGV[3]: session key prefix for pausing previous active
            ts,            # ARGV[4]: score for zadd (updates displaced session's recency)
        )

        # Activate target session
        await self._r.hset(self._session_key(session_id), mapping={
            "status": "active",
            "agent_id": agent_id,
            "updated_at": now,
        })
        await self._r.set(active_key, session_id)

        # Remove TTL (in case it was set during a previous pause/abandon)
        for key in self._all_session_keys(session_id):
            await self._r.persist(key)

        return {"status": "active", "session_id": session_id}

    # ------------------------------------------------------------------
    # List (fix #4 — proper cursor-based iteration)
    # ------------------------------------------------------------------

    async def list_sessions(
        self, status: str | None = None, agent_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        batch_size = max(limit * 3, 50)
        offset = 0

        while len(results) < limit:
            ids = await self._r.zrevrangebyscore(
                self.INDEX_KEY, "+inf", "-inf", start=offset, num=batch_size,
            )
            if not ids:
                break
            offset += len(ids)

            for sid in ids:
                if len(results) >= limit:
                    break
                meta = await self._r.hgetall(self._session_key(sid))
                if not meta:
                    await self._r.zrem(self.INDEX_KEY, sid)
                    continue
                if status and meta.get("status") != status:
                    continue
                if agent_id and meta.get("agent_id") != agent_id:
                    continue
                results.append({
                    "session_id": sid,
                    "goal": meta.get("goal", ""),
                    "status": meta.get("status", ""),
                    "created_at": meta.get("created_at", ""),
                    "updated_at": meta.get("updated_at", ""),
                    "agent_id": meta.get("agent_id", ""),
                    "briefing_id": meta.get("briefing_id", ""),
                })

        return results

    # ------------------------------------------------------------------
    # Cleanup (fix #3 — handle active/paused, hard limit at 2x max)
    # ------------------------------------------------------------------

    async def _enforce_max_sessions(self) -> None:
        count = await self._r.zcard(self.INDEX_KEY)
        if count <= self._s.MAX_SESSIONS:
            return

        oldest = await self._r.zrange(self.INDEX_KEY, 0, count - self._s.MAX_SESSIONS - 1)
        skipped_active = 0
        hard_limit = self._s.MAX_SESSIONS * 2

        for sid in oldest:
            meta = await self._r.hgetall(self._session_key(sid))

            # Expired entry with no metadata — clean up index
            if not meta:
                await self._r.zrem(self.INDEX_KEY, sid)
                continue

            session_status = meta.get("status")

            if session_status in ("active", "paused"):
                skipped_active += 1
                # Hard limit: force-evict oldest paused sessions if count exceeds 2x max
                current_count = await self._r.zcard(self.INDEX_KEY)
                if current_count > hard_limit and session_status == "paused":
                    logger.warning(
                        "Hard limit exceeded (%d > %d), force-evicting paused session %s",
                        current_count, hard_limit, sid,
                    )
                    for key in self._all_session_keys(sid):
                        await self._r.delete(key)
                    await self._r.zrem(self.INDEX_KEY, sid)
                continue

            # D1: completed but not yet distilled — deleting now would lose
            # the session's knowledge forever. The worker resolves this to
            # "success" or "dlq" (both TTL'd), after which it becomes deletable.
            # Legacy pre-queue values ("", "pending", "failed") stay deletable.
            if session_status == "completed" and meta.get("distillation") == "queued":
                skipped_active += 1
                continue

            # completed or abandoned — safe to delete
            for key in self._all_session_keys(sid):
                await self._r.delete(key)
            await self._r.zrem(self.INDEX_KEY, sid)

        if skipped_active:
            logger.warning(
                "Session cleanup skipped %d active/paused sessions; index still over max (%d)",
                skipped_active, self._s.MAX_SESSIONS,
            )

    async def set_proactive_memories(
        self, session_id: str, memories: list[dict]
    ) -> None:
        """Store proactive recall results for inclusion in shadow context."""
        key = self._proactive_key(session_id)
        await self._r.set(key, json.dumps(memories))
        # Match session TTL so proactive data expires with the session
        ttl = self._s.SESSION_TTL_DAYS * 86400
        await self._r.expire(key, ttl)

    async def set_distillation_status(self, session_id: str, status: str) -> None:
        await self._r.hset(self._session_key(session_id), "distillation", status)

    async def expire_session_keys(self, session_id: str) -> None:
        """Apply the 7-day retention TTL to all keys of a session.

        Called ONLY by the distill worker after confirmed distillation
        success or DLQ move (SP0 D1).
        """
        ttl_seconds = self._s.SESSION_TTL_DAYS * 86400
        for key in self._all_session_keys(session_id):
            await self._r.expire(key, ttl_seconds)
