"""FirekeepBridge MCP Server — shadow context tools for AI agents."""

from __future__ import annotations

import atexit
import asyncio
import logging

from fastmcp import FastMCP

try:
    from fastmcp.server.dependencies import get_http_headers, get_http_request
except ImportError as exc:
    logging.getLogger(__name__).error(
        "fastmcp get_http_headers unavailable — header-based identity DISABLED; "
        "all MCP calls will default to unknown/default identity: %s",
        exc,
    )

    def get_http_headers(*_args, **_kwargs) -> dict[str, str]:
        """Fallback used when fastmcp does not provide get_http_headers."""
        return {}

    def get_http_request(*_args, **_kwargs):
        """Fallback used when fastmcp does not provide get_http_request."""
        raise RuntimeError("get_http_request unavailable")

from app.config import get_settings
from app.prior_art import assemble_prior_art, render_prior_art
from app.proactive_recall import fetch_relevant_memories
from app.redis_client import get_redis, close_redis
from app.session import SessionManager, TASK_RESULTS, _experiment_group, member_token
from app.shadow import assemble_shadow
from app import residency

logger = logging.getLogger(__name__)

settings = get_settings()

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(server):
    """Start the background workers for the lifetime of the server.

    Two of them: the distillation worker (SP0 D1) and the crashed-session reaper
    (see app/reaper.py — a session whose agent died never reaches a terminal
    state on its own, so it is never scored). The reaper is registered
    unconditionally and no-ops per pass when NB_REAPER_ENABLED is false.
    """
    from app.distill_worker import close_distiller, distill_worker_loop
    from app.reaper import reaper_loop

    worker_task = asyncio.create_task(distill_worker_loop())
    reaper_task = asyncio.create_task(reaper_loop())
    try:
        yield {}
    finally:
        for task in (worker_task, reaper_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await close_distiller()


# Served in the MCP `initialize` handshake. This is the ONLY instruction channel
# that needs no client-side adapter, so it is the only one that reaches Codex (which
# has no hook surface and no instruction file) and a user who has deleted the
# rendered block from their own instruction file.
#
# It exists because of a real failure: a user asked "deploy to my vps" and the agent
# said it did not know, while the answer sat in memory as a 100%-confidence first
# result. Storage and retrieval worked; nothing triggered them. Tool descriptions do
# not fix this -- memory_recall's description already states its trigger and still
# does not fire (same lesson as decision_board in client 0.1.11).
#
# Keep it SHORT. It is sent once per session, not per request, but it competes for
# attention with everything else in the handshake.
# REACHABILITY (measured 2026-08-21): the shipped client kit does NOT receive
# this string. Every runtime mounts exactly one MCP entry -- the local gateway
# (FIREKEEP_MCP_KEYS = ("firekeep",), client/firekeep_client/adapters/base.py) --
# and the gateway discards each backend's `initialize` result and reads only
# tools/list (client/firekeep_client/gateway.py, Backend.start / Backend.discover).
# What a kit agent actually receives in its system prompt is the gateway's own
# GATEWAY_INSTRUCTIONS.
#
# So this text reaches ONLY a hand-configured client connected straight to this
# service's port (docs/INTEGRATIONS.md). That is a real audience and the reason
# the string stays -- but it means editing it changes nothing for any kit user.
# Adding behaviour here is the same trap adapters/base.py records having cost a
# release: a paragraph added where no runtime could see it. If you need an agent
# to do something, put it in GATEWAY_INSTRUCTIONS.
_INSTRUCTIONS = """Firekeep -- persistent team memory for agents.

Recall BEFORE answering, and treat not knowing as the trigger: if the user names a
host, IP, path, service, credential or convention you cannot name from the current
conversation ("my VPS", "our server"), or uses history words ("again", "still",
"last time", "how did we"), call memory_recall(task=<their request>) first. Never
claim you don't know about the user's own systems before calling it once. If a
result names a vault key, follow up with vault_retrieve.

Write as you go: ctx_update after each meaningful step, memory_learn the moment a
fix works (including what failed first), skill_create after a hard-won fix,
ctx_complete_session when done. Secrets go to vault_store, never memory_learn.
"""

mcp = FastMCP("FirekeepBridge", lifespan=_lifespan, instructions=_INSTRUCTIONS)


def _default_agent_id(agent_id: str | None = None) -> str:
    """Resolve agent identity: explicit param > X-Agent-Id header > "default".

    "default" is the sentinel tool default, so a connection header overrides
    it; any other explicit value wins (SP0 D3). get_http_headers() never
    raises and returns {} outside a request context.
    """
    if agent_id and agent_id != "default":
        return agent_id
    headers = get_http_headers()
    return headers.get("x-agent-id") or agent_id or "default"


def _header_session_id() -> str | None:
    """Read the MCP connection's X-Session-Id header. None when absent/empty.

    Mirrors cortex/app/mcp_server.py's _header_identity. The header is a
    per-connection transport property, so it names THIS terminal's session even
    when two terminals share one agent_id — the case the shared
    nb:active:{agent_id} pointer cannot express, and the enabling half of the
    2026-08-11 cross-terminal clobber (a no-arg ctx_complete_session from
    terminal B resolved via the shared pointer and completed terminal A's
    in-flight session). Header-name matching is case-insensitive.
    get_http_headers() never raises and returns {} outside a request context,
    so this is safe in unit tests and stdio transports.
    """
    headers = get_http_headers() or {}
    lowered = {str(name).lower(): value for name, value in headers.items()}
    return lowered.get("x-session-id") or None


# Outcome truth (PR1) — self-reported grade limits, mirrored in
# SessionManager.complete_session's docstring (app/session.py).
_MAX_EVIDENCE_ITEMS = 10
_MAX_EVIDENCE_CHARS = 300


def _verified_member_id() -> str | None:
    """The authenticated member behind this request, or None when unknowable.

    FirekeepKeyAuthMiddleware (installed on /mcp in __main__) validates
    X-API-Key and attaches the verified identity to scope['state'];
    principal_from_scope also handles the auth-disabled case (anonymous owner
    principal). Outside an authenticated HTTP context (in-memory tests, auth
    enabled but identity missing) this returns None — and a None principal
    can never authorize a bound public terminal operation (D13)."""
    try:
        from auth.principal import principal_from_scope
        return principal_from_scope(get_http_request().scope).get("member_id")
    except Exception:
        return None


# Living Instructions round 2 — the measurement contract
# (docs/superpowers/specs/2026-08-11-living-instructions-design.md). Five
# attribution headers, attached by the gateway and hook cores from client
# 0.1.41: header name -> session field name. Trust level is exactly
# X-Agent-Id's — an untrusted observability label, never a gate.
_ATTRIBUTION_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-firekeep-runtime", "runtime"),
    ("x-firekeep-client", "client_version"),
    ("x-firekeep-instr-rendered", "instr_rendered"),
    ("x-firekeep-instr-expected", "instr_expected"),
    ("x-firekeep-instr-gateway", "instr_gateway"),
)


def _attribution_from_headers() -> dict[str, str]:
    """The X-Firekeep-* attribution headers that arrived, keyed by field name.

    Header-name matching is case-insensitive. A header that did not arrive —
    every client before 0.1.41 — is simply absent from the result: the session
    reads as unattributed, which is a normal state, never an error.
    """
    try:
        headers = get_http_headers() or {}
    except Exception:  # pragma: no cover — get_http_headers documents it never raises
        headers = {}
    lowered = {str(name).lower(): value for name, value in headers.items()}
    out: dict[str, str] = {}
    for header, field in _ATTRIBUTION_HEADERS:
        value = lowered.get(header)
        if value:
            out[field] = str(value)
    return out

# ---------------------------------------------------------------------------
# Replay emitter (fire-and-forget trace events)
# ---------------------------------------------------------------------------

_replay_initialized = False


async def _ensure_replay() -> None:
    """Lazy-init the replay emitter on first use."""
    global _replay_initialized
    if _replay_initialized:
        return
    _replay_initialized = True
    try:
        from replay.emitter import init_emitter
        await init_emitter()
        logger.info("Replay emitter initialized for Bridge")
    except Exception as exc:
        logger.debug("Replay emitter init failed (non-critical): %s", exc)


async def _replay_emit(event_type: str, session_id: str, agent_id: str, payload: dict, **kwargs) -> None:
    """Emit a replay event. Never raises, never blocks."""
    try:
        await _ensure_replay()
        from replay.emitter import emit
        await emit(event_type, session_id, agent_id, payload, **kwargs)
    except Exception as exc:
        logger.debug("Replay emit failed (non-critical): %s", exc)

# ---------------------------------------------------------------------------
# Detached background tasks (SP0 D5)
# ---------------------------------------------------------------------------

_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    """Run a coroutine as a detached task, holding a strong reference so the
    event loop cannot garbage-collect it mid-flight."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

async def _get_manager() -> SessionManager:
    r = await get_redis()
    return SessionManager(r, get_settings())


async def _shutdown() -> None:
    """Cleanup Redis and HTTP connections on shutdown."""
    await close_redis()
    logger.info("FirekeepBridge shutdown complete")


def _atexit_cleanup() -> None:
    """Best-effort cleanup at interpreter exit (fix #6)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_shutdown())
        else:
            loop.run_until_complete(_shutdown())
    except RuntimeError:
        pass


atexit.register(_atexit_cleanup)


async def _trigger_eval(
    api_url: str, session_id: str, max_retries: int = 3,
    *, task_result: str | None = None,
):
    """Trigger eval computation on Cortex with retry. Fire-and-forget.

    D8: task_result rides as a HINT for the compute path — it survives a lost
    replay emit (the eval trigger and the replay emit are two independent
    fire-and-forget effects, so either can fail alone), and Cortex honors it
    only under eval:grade. It is best effort, never authoritative: computed
    eval grading still derives from persisted session state, not this param.
    """
    import httpx
    headers: dict[str, str] = {}
    if settings.FIREKEEP_API_KEY:
        headers["X-API-Key"] = settings.FIREKEEP_API_KEY
    last_error: str | None = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                params: dict[str, str] = {"trigger": "session_complete"}
                if task_result is not None:
                    params["task_result"] = task_result
                resp = await client.post(
                    f"{api_url}/evals/sessions/{session_id}/compute",
                    headers=headers,
                    params=params,
                )
                if resp.status_code < 400:  # 2xx success
                    return True
                if resp.status_code < 500:  # 4xx permanent failure — don't retry
                    # ERROR, not WARNING: a 4xx here is a CONFIGURATION fault
                    # that repeats on every completion and silently starves
                    # OWM, quality trends and the pattern A/B join. The live
                    # deployment logged this at WARNING on every session for
                    # 12 days (403 — the compute route was admin-gated while
                    # the internal key holds eval:write) and nobody saw it.
                    logger.error(
                        "Eval trigger PERMANENT failure for session %s: HTTP %d. "
                        "Auto-evals are not being computed; check the key's "
                        "scopes against POST /evals/sessions/{id}/compute.",
                        session_id, resp.status_code,
                    )
                    return False
                # 5xx transient — retry
                last_error = f"HTTP {resp.status_code}"
                logger.debug(
                    "Eval trigger transient failure for session %s (attempt %d): %s",
                    session_id, attempt + 1, last_error,
                )
        except Exception as exc:
            last_error = f"connection error: {exc}"
            logger.debug(
                "Eval trigger transient failure for session %s (attempt %d): %s",
                session_id, attempt + 1, last_error,
            )
        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s
    logger.warning(
        "Eval trigger failed for session %s after %d retries: %s",
        session_id, max_retries, last_error,
    )
    return False


async def _trigger_skill_evaluate(api_url: str, session_id: str, skill_worthy: bool = False) -> bool:
    """Fire-and-forget POST /skill/evaluate on Cortex."""
    import httpx
    headers: dict[str, str] = {}
    if settings.FIREKEEP_API_KEY:
        headers["X-API-Key"] = settings.FIREKEEP_API_KEY
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{api_url}/skill/evaluate",
                json={"session_id": session_id, "skill_worthy": skill_worthy},
                headers=headers,
            )
            return resp.status_code < 400
    except Exception as exc:
        logger.debug("Skill evaluate trigger failed for session %s: %s", session_id, exc)
        return False


async def after_abandon(session_id: str, agent_id: str, *, reaped: bool = False) -> None:
    """Post-abandon effects shared by ctx_abandon_session and the reaper.

    SessionManager.abandon_session owns the Redis invariants (status, pointer
    cleanup, TTL, the `session.abandoned` replay event). These two effects sit
    OUTSIDE it and are what make an abandonment visible to scoring: the
    `session_end` event carrying outcome="partial", and the eval trigger that
    turns it into a computed eval. A reaped session that skipped either one
    would be abandoned in Redis and invisible to OWM — which is the whole
    defect the reaper exists to close.

    It lives here rather than in reaper.py so there is exactly one definition:
    a copy-pasted second version is how the two paths silently diverge, and the
    divergence would only ever show up as a gap in scoring nobody is watching.

    `reaped` marks the event as the reaper's work rather than a human's explicit
    ctx_abandon_session. The flag is added only on the reaper path, leaving the
    already-shipped human-abandon payload byte-identical.
    """
    payload: dict = {"outcome": "abandoned", "distilled": False}
    if reaped:
        payload["reaped"] = True
    await _replay_emit("session_end", session_id, agent_id, payload, outcome="partial")

    # Detached fire-and-forget (SP0 D5) — the caller must not wait on Cortex.
    _spawn_background(_trigger_eval(settings.FIREKEEP_API_URL, session_id))


@mcp.tool()
async def ctx_start_session(
    goal: str,
    agent_id: str = "default",
    tags: list[str] | None = None,
    project: str | None = None,
    briefing_id: str | None = None,
) -> dict:
    """Start a new working session with a goal description.

    Call this when beginning a new task. If you already have an active session,
    it will be automatically paused.

    Args:
        goal: What you are trying to accomplish (e.g., "implement namespace normalization").
        agent_id: Your agent identifier (use different IDs for parallel terminals).
        tags: Optional categorization tags.
        project: Optional project name for team memory attribution. Stored with
            the session and forwarded to Cortex when the session is distilled.
        briefing_id: Optional id minted by the server-side GET /briefing endpoint.
            Stored on the session so the pre-flight A/B tip-shown recording can be
            attributed to this session (closes the strategy-pattern feedback loop).

    Returns the new session id, and — when there is anything to say — `prior_art`
    (what the team already built, plus who is mid-flight on similar work) with
    `prior_art_text`, the block written for you to read. Treat it as a recall
    trigger, not a summary: the entries are one-line summaries of longer
    memories, so call `memory_recall` before rebuilding anything it names.
    """
    agent_id = _default_agent_id(agent_id)
    attribution = _attribution_from_headers()
    owner_member = _verified_member_id()
    mgr = await _get_manager()
    result = await mgr.start_session(
        goal, agent_id=agent_id, tags=tags, project=project, briefing_id=briefing_id,
        runtime=attribution.get("runtime"),
        client_version=attribution.get("client_version"),
        instr_rendered=attribution.get("instr_rendered"),
        instr_expected=attribution.get("instr_expected"),
        instr_gateway=attribution.get("instr_gateway"),
        owner_member=owner_member,
    )

    # Replay: trace session start. briefing_id and the attribution headers ride
    # this payload — compute_session_eval reads them from the timeline it
    # already loads, so this event is the only place they need to appear.
    sid = result.get("session_id", "")
    if sid:
        payload: dict = {
            "goal": goal,
            "tags": tags or [],
            "briefing_id": briefing_id or "",
            # Pre-registered arm assignment (outcome truth, PR4 D1) — computed
            # from the SAME verified owner_member just passed to
            # start_session above, never re-derived. Orthogonal to the grade:
            # this is assigned before any grade exists.
            "experiment_group": _experiment_group(owner_member),
            # PR5 D13: rides the same path experiment_group does, into the
            # parsed eval record, so members-per-arm is computable there.
            "member_token": member_token(owner_member),
        }
        payload.update(attribution)  # only the headers that actually arrived
        await _replay_emit("session_start", sid, agent_id, payload)

    # Prior art — pushed at the moment of intent, AFTER the session exists.
    # Ordering is the whole safety argument: `result` is already a created
    # session by the time anything here runs, so no failure below can cost the
    # caller the session it asked for. assemble_prior_art swallows its own
    # errors and returns {} — the try/except is the floor under that floor.
    if sid and settings.PRIOR_ART_ENABLED:
        try:
            prior_art = await assemble_prior_art(
                goal,
                mgr=mgr,
                agent_id=agent_id,
                api_url=settings.FIREKEEP_API_URL,
                api_key=settings.FIREKEEP_API_KEY,
                top_k=settings.PRIOR_ART_TOP_K,
                min_score=settings.PRIOR_ART_MIN_SCORE,
                in_flight_max=settings.PRIOR_ART_IN_FLIGHT_MAX,
                timeout=settings.PRIOR_ART_TIMEOUT_SECONDS,
            )
            if prior_art:
                block = render_prior_art(prior_art)
                if block:
                    result["prior_art"] = prior_art
                    result["prior_art_text"] = block
        except Exception as exc:
            logger.info("Prior art skipped for session %s (non-fatal): %s", sid, exc)

    return result


@mcp.tool()
async def ctx_update(
    category: str, content: str, key: str | None = None, agent_id: str = "default"
) -> dict:
    """Update your working context with new information.

    Call this as you work to record your plan, decisions, file knowledge, and progress.
    This data persists across context compressions and session restarts.

    Args:
        category: What to update — "plan", "decision", "file", "progress", or "scratch".
        content: The content to store. For "plan", send the full current plan (Markdown checklist).
        key: Required for "file" (file path) and "scratch" (entry name). Ignored for others.
        agent_id: Your agent identifier.
    """
    agent_id = _default_agent_id(agent_id)
    # The tool's public signature deliberately stays session_id-free (no
    # adapter/instruction churn): the connection's X-Session-Id header alone
    # scopes the write to the caller's own session. SessionManager.update
    # falls back to the shared active pointer when it is None — every
    # pre-header client's behavior, unchanged.
    header_session_id = _header_session_id()
    mgr = await _get_manager()
    try:
        result = await mgr.update(
            category, content, key=key, agent_id=agent_id,
            session_id=header_session_id,
        )
    except ValueError as e:
        return {"error": str(e)}

    # Replay: trace context update — resolved the same way the write itself
    # was (header first), so the event lands on the session actually written.
    session_id = header_session_id or await mgr.get_active_session_id(agent_id)
    if session_id:
        # Context snapshots only at decision points (plan/decision categories)
        ctx_ref = None
        if category in ("decision", "plan"):
            try:
                from replay.emitter import store_context_snapshot
                from app.shadow import assemble_shadow
                data = await mgr.get_session_data(session_id)
                if data:
                    ctx_ref = await store_context_snapshot(assemble_shadow(data))
            except Exception:
                pass
        await _replay_emit(
            "ctx_update", session_id, agent_id,
            {"category": category, "content_length": len(content), "key": key},
            context_ref=ctx_ref,
        )

    # Proactive recall: fetch relevant memories for qualifying categories
    if (
        settings.PROACTIVE_RECALL_ENABLED
        and category in settings.PROACTIVE_RECALL_CATEGORIES.split(",")
    ):
        try:
            # session_id was already resolved above (header first, pointer as
            # fallback) — reuse it so proactive results attach to the session
            # the write landed in, not whatever the shared pointer names now.
            if session_id:
                memories = await fetch_relevant_memories(
                    content,
                    api_url=settings.FIREKEEP_API_URL,
                    api_key=settings.FIREKEEP_API_KEY,
                    # Deliberately NOT settings.FIREKEEP_NAMESPACE. That value
                    # ("default") is the namespace Bridge WRITES distillates
                    # under, and on Cortex a namespace is a category, not a
                    # partition — passing it here would scope proactive recall
                    # to one category and hide everything an agent filed under
                    # another. Omitting it searches them all.
                    namespace=None,
                    top_k=settings.PROACTIVE_RECALL_TOP_K,
                    min_score=settings.PROACTIVE_RECALL_MIN_SCORE,
                )
                if memories:
                    await mgr.set_proactive_memories(session_id, memories)
                    result["proactive_memories"] = len(memories)
        except Exception as exc:
            logger.debug("Proactive recall trigger failed (non-fatal): %s", exc)

    return result


@mcp.tool()
async def ctx_get_shadow(session_id: str | None = None, agent_id: str = "default",
                         since: str | None = None) -> dict:
    """Retrieve your working context as a Markdown document.

    Call this after context compression or when starting a new conversation to restore
    your working state. Returns everything by default: your plan, decisions, file
    knowledge, progress, and scratchpad.

    Args:
        session_id: Specific session to retrieve (defaults to the connection's
            X-Session-Id header, then your active session).
        agent_id: Your agent identifier.
        since: OPTIONAL. The `shadow_cursor` from an earlier response in THIS
            conversation. Pass it ONLY if that earlier shadow is still visible in
            your context — it returns just what has changed since. If you are
            unsure, or your context was compacted, OMIT it and receive the full
            document. Omitting it is always correct.
    """
    agent_id = _default_agent_id(agent_id)
    mgr = await _get_manager()
    # Precedence: explicit param > connection header > active pointer (cortex's
    # documented order). The header step keeps two terminals sharing one
    # agent_id from restoring each other's session via the shared pointer.
    # NOTE: get_active_session_ID — verified name (session.py:332). Guard shape
    # copied from the current implementation, which uses `is None`, not falsiness.
    if session_id is None:
        session_id = _header_session_id()
    if session_id is None:
        session_id = await mgr.get_active_session_id(agent_id)
    if not session_id:
        return {"error": "No active session. Start one with ctx_start_session.", "delta": False}

    data = await mgr.get_session_data(session_id)
    if not data:
        return {"error": f"Session {session_id} not found.", "delta": False}

    # AMENDED 2026-07-30 (C1 + C2).
    epoch = await mgr.get_shadow_epoch(session_id)
    if epoch is None:
        # C2: the epoch read FAILED, so we cannot tell whether a cursor is stale.
        # Force a full restore AND mint no cursor — a response carrying no cursor
        # cannot produce a later delta, which is the safe outcome. Never coerce a
        # failed read to "", which would match every pre-compaction cursor.
        return {
            "session_id": session_id,
            "goal": data.get("goal", ""),
            "status": data.get("status", ""),
            "shadow": assemble_shadow(data),
            "delta": False,
        }

    try:
        rendered, omitted = residency.filter_since(
            data, since, session_id=session_id, epoch=epoch)
        # C1: the omission report goes INTO the rendered document, not just beside it.
        # Without this, an omitted section renders as '*No decisions recorded*' — an
        # affirmative denial that the agent's own work exists.
        shadow = assemble_shadow(rendered, omitted=omitted)
    except Exception as exc:
        # M1: the filter-and-render pair is guarded for exactly the reason the cursor
        # mint below already is. This is the post-compaction lifeline — an agent calls
        # it precisely when it has lost its working state — so a malformed session
        # turning it into an exception is strictly worse than any token cost, and the
        # module's stated contract is that every doubtful path returns the full
        # document. Same shape as C2: the FULL, unfiltered document, delta=False, and
        # NO cursor minted (a cursor handed back could seed a later delta on a session
        # whose state was never filterable). A degraded full restore is always correct;
        # a raised exception never is.
        #
        # residency.filter_since refuses the malformed container shapes we ENUMERATED,
        # so this catches the ones nobody thought of; assemble_shadow is total (see its
        # docstring), so the fallback below is a floor rather than a second cliff.
        logger.warning(
            "shadow delta failed for session %s; serving a full restore: %s",
            session_id, exc,
        )
        return {
            "session_id": session_id,
            "goal": data.get("goal", ""),
            "status": data.get("status", ""),
            "shadow": assemble_shadow(data),
            "delta": False,
        }

    result = {
        "session_id": session_id,
        "goal": data.get("goal", ""),
        "status": data.get("status", ""),
        "shadow": shadow,
        "delta": omitted is not None,
    }
    try:
        # Always minted from the FULL data, never the filtered copy: the cursor
        # describes what the caller now holds in total, not what this response
        # carried. Guarded: a malformed timestamp anywhere in the session (e.g. a
        # non-string truthy stamp reaching high_water_of's max()) must not crash
        # the post-compaction lifeline — a response with no cursor is a safe dead
        # end, the same principle C2 already established for a failed epoch read.
        result["shadow_cursor"] = residency.encode_cursor(
            session_id, epoch, residency.high_water_of(data), residency.plan_sha_of(data))
    except Exception as exc:
        logger.warning("shadow_cursor mint failed for session %s: %s", session_id, exc)
    if omitted is not None:
        note = residency.omission_notice(omitted)
        if note:
            result["note"] = note   # belt and braces; the markdown now says it too
    return result


@mcp.tool()
async def ctx_complete_session(
    session_id: str | None = None, outcome: str | None = None,
    agent_id: str = "default", skill_worthy: bool = False,
    task_result: str | None = None, task_evidence: list[str] | None = None,
) -> dict:
    """Mark the current session as completed and save learnings to long-term memory.

    Call this when your task is done. Pass `task_result` — "success", "partial", or
    "failure" — grading the TASK you were doing, not whether this RPC call itself
    worked. That grade is the default expectation of every call, not an optional
    extra: an ungraded completion tells the memory system nothing about whether the
    work actually landed.

    Reporting "failure" or "partial" is expected and safe to report — an honest
    failure teaches the memory system more than an optimistic "success" and carries
    no penalty. Back the grade with `task_evidence`: what you actually verified
    (tests run, commands that passed, files changed), not what you intended to do.

    The session is enqueued for distillation into a FirekeepCortex memory (a
    background worker drains the queue with retry/backoff) so future sessions can
    benefit from what you learned. Set `skill_worthy=True` if this session involved
    a hard-won fix worth saving as a reusable skill.

    Args:
        session_id: Session to complete (defaults to the connection's
            X-Session-Id header, then your active session).
        outcome: Summary of what was accomplished (free text — prose, not the grade).
        agent_id: Your agent identifier.
        skill_worthy: Set True if this session involved a hard-won fix worth saving as a skill.
        task_result: Structured self-grade of the TASK itself: "success" (the goal
            was verifiably achieved), "partial" (real progress, goal not reached),
            or "failure". Omit when genuinely unsure — an honest absence beats a
            guessed grade. Accepted only from the session's verified owner.
        task_evidence: Up to 10 short verifiable claims backing the grade
            (tests run, commands that passed, files changed). Ignored without a grade.
    """
    agent_id = _default_agent_id(agent_id)
    # Precedence: explicit param > connection header > active pointer. The
    # active pointer is shared by every terminal running under one agent_id,
    # so a no-arg completion (nudged by the stop hook's reminder) used to
    # resolve to — and finish — a SIBLING terminal's in-flight session. The
    # header names the caller's own session; the pointer remains only the
    # last-resort fallback for header-less clients.
    if session_id is None:
        session_id = _header_session_id()

    # Spec D1: coerce invalid VALUES, never fail. (Wrong TYPES are rejected by
    # FastMCP validation pre-function; wire-tested.)
    graded = task_result if task_result in TASK_RESULTS else None
    evidence = [
        e.strip()[:_MAX_EVIDENCE_CHARS]
        for e in (task_evidence or []) if isinstance(e, str) and e.strip()
    ][:_MAX_EVIDENCE_ITEMS] if graded else []

    mgr = await _get_manager()
    try:
        result = await mgr.complete_session(
            session_id=session_id, outcome=outcome, agent_id=agent_id,
            task_result=graded, task_evidence=evidence,
            verified_member=_verified_member_id(),
        )
    except (ValueError, RuntimeError) as e:
        return {"error": str(e)}

    sid = result["session_id"]
    # Task 1 (SessionManager.complete_session) returns the stored winner —
    # never reconstruct authority from this call's own input. A re-grade that
    # lost the CAS still reports back the ALREADY-STORED grade below, not
    # what this caller submitted.
    authoritative_grade = result.get("task_result")
    authoritative_source = result.get("task_result_source")
    if (
        authoritative_grade not in TASK_RESULTS
        or authoritative_source != "self_reported"
    ):
        authoritative_grade = None
        authoritative_source = None
        result["task_result"] = None
        result["task_result_source"] = None

    # Replay: trace session end. Spec D3 — the emit kwarg carries the GRADE,
    # or None (event carries no outcome) when ungraded. The old hard-coded
    # outcome="success" meant "the RPC worked" and fed every downstream
    # success metric; see docs/superpowers/specs/2026-08-23-outcome-truth-design.md.
    payload: dict = {"outcome": outcome or "", "distilled": False}
    if authoritative_grade and authoritative_source:
        payload["task_result"] = authoritative_grade
        payload["task_result_source"] = authoritative_source
    await _replay_emit(
        "session_end", sid, agent_id, payload, outcome=authoritative_grade)

    # D1: distillation is enqueued by SessionManager.complete_session and
    # drained by the distill worker with retry/backoff. No inline distillation.
    result["distillation"] = "queued"

    # Auto-eval: detached fire-and-forget (SP0 D5) — completion must return
    # as soon as the session state + distill enqueue are committed. The
    # authoritative grade rides along as a hint (D8) — see _trigger_eval.
    _spawn_background(_trigger_eval(
        settings.FIREKEEP_API_URL, sid, task_result=authoritative_grade))
    # "dispatched", not "scheduled": the call is detached by design (SP0 D5 —
    # completion returns as soon as session state + distill enqueue commit),
    # so this response CANNOT know whether the eval computed. It said
    # "scheduled" while every single trigger was 403-ing, which told the agent
    # a thing that had not happened. This word claims only what is true — the
    # request left this process.
    result["eval_triggered"] = "dispatched"

    # Skill synthesis: trigger async (fire-and-forget)
    skill_ok = await _trigger_skill_evaluate(settings.FIREKEEP_API_URL, sid, skill_worthy)
    result["skill_synthesis_triggered"] = skill_ok

    if task_result is not None and task_result not in TASK_RESULTS:
        result["task_result_note"] = (
            f"ignored invalid task_result {task_result!r}; "
            f"expected one of {', '.join(TASK_RESULTS)}"
        )

    return result


@mcp.tool()
async def ctx_abandon_session(session_id: str | None = None, agent_id: str = "default") -> dict:
    """Abandon a session without saving to long-term memory.

    Use this for sessions started by mistake or no longer relevant.

    Args:
        session_id: Session to abandon (defaults to the connection's
            X-Session-Id header, then your active session).
        agent_id: Your agent identifier.
    """
    agent_id = _default_agent_id(agent_id)
    # Same precedence as ctx_complete_session: explicit param > connection
    # header > shared active pointer (the clobber-prone fallback).
    if session_id is None:
        session_id = _header_session_id()

    mgr = await _get_manager()
    try:
        # Resolve and FREEZE one SID before touching the manager's mutating
        # call — explicit > header (above) > active-pointer, read here
        # rather than left for SessionManager.abandon_session to resolve
        # later. owner_member is immutable (Task 1: written once, in
        # start_session), so reading it here and checking it before the
        # manager call below is authorization-safe: it cannot change between
        # this read and that call. Legacy-unbound sessions (no owner_member)
        # keep today's label-only behavior — an explicit D13 residual.
        resolved_sid = session_id
        if resolved_sid is None:
            resolved_sid = await mgr.get_active_session_id(agent_id)
        if not resolved_sid:
            raise ValueError("No active session")

        data = await mgr.get_session_data(resolved_sid)
        if not data:
            raise ValueError(f"Session {resolved_sid} not found")
        owner_member = data.get("owner_member") or ""
        if owner_member and _verified_member_id() != owner_member:
            raise ValueError(
                f"Session {resolved_sid} belongs to a different verified owner")

        # SessionManager.abandon_session and the reaper (app/reaper.py) are
        # UNCHANGED — this preflight is the only gate on the public tool, and
        # it always passes the resolved SID explicitly, never None, so no
        # later re-resolution of the pointer can race past this check.
        result = await mgr.abandon_session(
            session_id=resolved_sid, agent_id=agent_id)
    except ValueError as e:
        return {"error": str(e)}

    # Replay: trace session abandoned, then auto-eval. Shared with the reaper —
    # see after_abandon's docstring for why these two effects are not optional.
    sid = result.get("session_id", resolved_sid)
    if sid:
        await after_abandon(sid, agent_id)

    return result


@mcp.tool()
async def ctx_list_sessions(
    status: str | None = None, agent_id: str | None = None, limit: int = 10
) -> dict:
    """List recent sessions.

    Args:
        status: Filter by status — "active", "paused", "completed", "abandoned".
        agent_id: Filter by agent ID.
        limit: Maximum number of sessions to return.
    """
    mgr = await _get_manager()
    sessions = await mgr.list_sessions(status=status, agent_id=agent_id, limit=limit)
    return {"sessions": sessions}


@mcp.tool()
async def ctx_resume_session(
    session_id: str, agent_id: str = "default", takeover: bool = False
) -> dict:
    """Resume a PAUSED session of your own and get its working context.

    Resuming a session that belongs to another agent is refused unless you
    pass takeover=True, and a session that is currently ACTIVE for another
    agent is refused outright — resume picks up work that stopped, it never
    evicts a live agent.

    Args:
        session_id: The session ID to resume.
        agent_id: Your agent identifier.
        takeover: Explicitly adopt a paused session owned by another agent.
            The previous owner loses it (their active pointer is cleared), so
            only use this for a deliberate hand-off.
    """
    agent_id = _default_agent_id(agent_id)
    mgr = await _get_manager()
    try:
        await mgr.resume_session(
            session_id, agent_id=agent_id, takeover=takeover,
            verified_member=_verified_member_id(),
        )
    except ValueError as e:
        return {"error": str(e)}

    data = await mgr.get_session_data(session_id)
    if not data:
        return {"error": f"Session {session_id} not found after resume."}

    shadow = assemble_shadow(data)
    result = {
        "session_id": session_id,
        "goal": data.get("goal", ""),
        "status": "active",
        "shadow": shadow,
    }
    # A resume always delivers the COMPLETE document (never a delta — a resumed
    # session is by definition one the agent cannot vouch for), so minting a cursor
    # here is exactly as safe as on a full ctx_get_shadow. Deliberately no `delta`
    # key: it would always be False, and an always-false flag invites a caller to
    # start passing `since` to a tool that must never accept it.
    epoch = await mgr.get_shadow_epoch(session_id)
    if epoch is not None:
        try:
            # Guarded for the same reason as ctx_get_shadow's mint: a malformed
            # timestamp must not crash a resume, which is the crash-recovery path
            # itself. No cursor is a safe dead end here too.
            result["shadow_cursor"] = residency.encode_cursor(
                session_id, epoch, residency.high_water_of(data), residency.plan_sha_of(data))
        except Exception as exc:
            logger.warning("shadow_cursor mint failed for session %s: %s", session_id, exc)
    return result


from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse as StarletteJSONResponse
from auth.asgi import require_scope_asgi, ScopeError
from app.distill_worker import requeue_dlq as requeue_distill_dlq_records


@mcp.custom_route("/ops/distill-dlq/requeue", methods=["POST"], name="requeue_distill_dlq")
async def _requeue_distill_dlq(request: StarletteRequest) -> StarletteJSONResponse:
    """Requeue dead-lettered distillation jobs (admin) — Bridge's one
    state-changing ops route, mirroring cortex POST /ops/dlq/requeue.
    Expired sessions (post-DLQ 7d TTL) are dropped-with-log, counted
    expired_dropped; see distill_worker.requeue_dlq."""
    try:
        require_scope_asgi(request, "admin")
        try:
            limit = int(request.query_params.get("limit", "1000"))
        except (ValueError, TypeError):
            limit = 1000
        limit = min(max(limit, 1), 10_000)
        redis = await get_redis()
        result = await requeue_distill_dlq_records(redis, get_settings(), limit=limit)
        return StarletteJSONResponse({"queue": "distill_dlq", **result})
    except ScopeError as e:
        return StarletteJSONResponse({"error": e.detail}, status_code=e.status_code)
    except Exception as e:
        logger.error("POST /ops/distill-dlq/requeue failed: %s", e)
        return StarletteJSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/sessions", methods=["GET"], name="list_sessions")
async def _list_sessions(request: StarletteRequest) -> StarletteJSONResponse:
    """List sessions with optional file data. Consumed by the SP1b-server
    GET /briefing aggregator (resumable-sessions source) and the
    session-resumption flow."""
    try:
        require_scope_asgi(request, "session:read")
        status_filter = request.query_params.get("status")
        agent_filter = request.query_params.get("agent_id")
        try:
            limit = int(request.query_params.get("limit", "20"))
        except (ValueError, TypeError):
            limit = 20
        limit = min(max(limit, 1), 200)

        mgr = await _get_manager()
        sessions = await mgr.list_sessions(
            status=status_filter, agent_id=agent_filter, limit=limit,
        )

        # Enrich with files for the briefing's resumables view (active sessions only, to limit cost)
        for sess in sessions:
            if sess.get("status") == "active":
                data = await mgr.get_session_data(sess["session_id"])
                if data:
                    sess["files"] = data.get("files", {})

        return StarletteJSONResponse({"sessions": sessions})
    except ScopeError as e:
        return StarletteJSONResponse({"error": e.detail}, status_code=e.status_code)
    except Exception as e:
        logger.error("GET /sessions failed: %s", e)
        return StarletteJSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/sessions/{session_id}", methods=["GET"], name="get_session")
async def _get_session(request: StarletteRequest) -> StarletteJSONResponse:
    """REST endpoint: get a single session by ID including its shadow data."""
    try:
        require_scope_asgi(request, "session:read")
        session_id = request.path_params["session_id"]
        mgr = await _get_manager()
        data = await mgr.get_session_data(session_id)
        if data is None:
            return StarletteJSONResponse({"error": "Session not found"}, status_code=404)
        shadow = assemble_shadow(data)
        return StarletteJSONResponse({
            "session_id": session_id,
            "goal": data.get("goal", ""),
            "outcome": data.get("outcome", ""),
            "duration_seconds": data.get("duration_seconds"),
            "shadow": shadow,
        })
    except ScopeError as e:
        return StarletteJSONResponse({"error": e.detail}, status_code=e.status_code)
    except Exception as e:
        logger.error("GET /sessions/%s failed: %s", request.path_params.get("session_id", ""), e)
        return StarletteJSONResponse({"error": str(e)}, status_code=500)


async def handle_post_session_context(mgr: SessionManager, *, agent_id: str, category: str, content: str, key: str | None = None) -> dict:
    """Testable handler — REST equivalent of the ctx_update MCP tool.

    Lets Relay persist Bridge decisions for origin:"mcp" scope sessions
    (SP2 D-S18), which have no local companion to call ctx_update over MCP.
    Raises ValueError on bad input or no active Bridge session for agent_id,
    same as ctx_update.
    """
    return await mgr.update(category, content, key=key, agent_id=agent_id)


@mcp.custom_route("/sessions/{agent_id}/context", methods=["POST"], name="post_session_context")
async def _post_session_context(request: StarletteRequest) -> StarletteJSONResponse:
    try:
        require_scope_asgi(request, "session:write")
        agent_id = request.path_params["agent_id"]
        body = await request.json()
        category = body.get("category")
        content = body.get("content")
        key = body.get("key")
        if not category or content is None:
            return StarletteJSONResponse({"error": "category and content are required"}, status_code=400)

        mgr = await _get_manager()
        result = await handle_post_session_context(mgr, agent_id=agent_id, category=category, content=content, key=key)
        return StarletteJSONResponse(result)
    except ScopeError as e:
        return StarletteJSONResponse({"error": e.detail}, status_code=e.status_code)
    except ValueError as e:
        return StarletteJSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error("POST /sessions/%s/context failed: %s", request.path_params.get("agent_id", ""), e)
        return StarletteJSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/health", methods=["GET"], name="health")
async def _health(request: StarletteRequest) -> StarletteJSONResponse:
    return StarletteJSONResponse({"status": "ok", "service": "bridge"})


@mcp.custom_route("/version", methods=["GET"], name="version")
async def _version(request: StarletteRequest) -> StarletteJSONResponse:
    """Build provenance. Unauthenticated and probes no backends — answers
    'what code is running here?' without introspection, so it works even when
    the service's dependencies are down."""
    from provenance import get_version_info

    return StarletteJSONResponse(get_version_info("bridge"))


if __name__ == "__main__":
    from auth.asgi import build_auth_middleware
    from auth.config import get_auth_settings

    mcp.run(
        transport="http",
        host=settings.MCP_HOST,
        port=settings.MCP_PORT,
        stateless_http=True,
        middleware=build_auth_middleware(get_auth_settings(), skip_paths=("/health", "/version")),
    )
