"""Briefing section builders.

Each builder returns a section dict:
    {"status": "ok"|"empty"|"unavailable", "error": str|None, "data": dict|None}

Builders return "ok"/"empty" on normal paths and RAISE on genuine upstream
failure; the api.py orchestrator wraps each call in a timeout and converts any
raise/timeout into {"status": "unavailable", ...}. This keeps per-section
isolation in one place (SP1b spec §5.1).
"""
from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.dreams.store import profile_point_id
from app.evals.store import get_eval_summary
from app.patterns.store import get_relevant_patterns, get_observed_patterns, record_tip_shown
from app.ops import collect_queue_depths
from app.skills import internal_key_headers
from app.skills.search import search_skill_points
from vault.store import list_secrets

from datetime import datetime, timedelta, timezone

from qdrant_client.models import FieldCondition, MatchValue

Section = dict[str, Any]

_ACTIVE_PRESENCE_THRESHOLD = 600  # mirror relay ACTIVE_THRESHOLD (presence.py:17)


def _to_epoch(iso_ts: str) -> float | None:
    """Parse a tz-aware ISO timestamp to epoch seconds; assume UTC if naive."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _presence_is_live_for(presence: dict | None, session: dict) -> bool:
    """D1 (corrected): presence is live evidence AGAINST a crash only if it
    belongs to the same session OR registered BEFORE the session's last update.

    A newer-than presence (a fresh sidecar for an unrelated process) is NOT
    evidence — reincarnating audit defect #20 otherwise.
    """
    if not presence or presence.get("status") != "active":
        return False
    if presence.get("session_id") and presence.get("session_id") == session.get("session_id"):
        return True
    try:
        started = float(presence.get("started_at") or 0)
    except (ValueError, TypeError):
        started = 0.0
    sess_updated = _to_epoch(session.get("updated_at", "")) or 0.0
    return bool(started and sess_updated and started < sess_updated)


def _environment_summary(health: dict) -> str:
    """Derive a human summary from the Sentinel /environment payload.

    Sentinel returns status/event_count/collectors/healthy — not a prebuilt
    'summary' string — so build one here (matches the legacy briefing line).
    """
    event_count = health.get("event_count", 0)
    collectors = health.get("collectors", {}) or {}
    down = sorted(name for name, ok in collectors.items() if not ok)
    if down:
        coll = f"Collector(s) degraded: {', '.join(down)}."
    elif collectors:
        coll = "All collectors healthy."
    else:
        coll = ""
    return f"Events: {event_count}. {coll}".strip()


def _empty() -> Section:
    return {"status": "empty", "error": None, "data": {}}


async def _get_json(http_client, url: str, internal_key: str | None,
                    params: dict | None = None) -> dict:
    """GET a JSON body with the SP1a internal key attached. Raises on error."""
    resp = await http_client.get(url, headers=internal_key_headers(internal_key), params=params)
    resp.raise_for_status()
    return resp.json()


def vault_visible(scopes: list[str]) -> bool:
    """Vault section is admin-gated (D4): visible only to admin/wildcard keys."""
    return "admin" in scopes or "*" in scopes


# --- Task 6 stubs (all replaced in Tasks 7–9) ------------------------------

async def quality_section(replay_redis) -> Section:
    """From /evals summary: total sessions + threshold-based insights.

    FAIL-LOUD LIMITATION (accepted): `get_eval_summary` (app/evals/store.py:117)
    wraps its whole body in `try/except Exception` and returns an empty
    `EvalSummary()` on any error, so a genuine backend outage (Redis down,
    corrupt data) reaches this builder as `total_sessions == 0` and is reported
    as status "empty", NOT "unavailable". We do NOT re-raise in the shared store
    fn — its other callers (the `/evals` REST routes) depend on the
    swallow-and-degrade contract. This read-only briefing accepts the loss of
    the outage signal for this section; the 4 outbound sections (Task 8) and the
    other in-process sections that don't sit behind a swallowing source (skills,
    vault, discipline, dlq) still fail loud.
    """
    summary = await get_eval_summary(replay_redis, limit=50)
    total = summary.total_sessions_evaluated
    avg = summary.avg_metrics or {}
    insights: list[str] = []
    tsr = avg.get("tool_success_rate")
    fr = avg.get("failure_rate")
    if tsr is not None and tsr < 0.9:
        insights.append(f"low tool success ({tsr:.0%})")
    if fr is not None and fr > 0.1:
        insights.append(f"elevated failure rate ({fr:.0%})")
    if total and not insights:
        insights.append("quality looks good")
    if total == 0:
        return {"status": "empty", "error": None,
                "data": {"total_sessions": 0, "avg_metrics": {}, "insights": []}}
    return {"status": "ok", "error": None, "data": {
        "total_sessions": total,
        "sessions_with_failures": summary.sessions_with_failures,
        "avg_metrics": avg,
        "insights": insights,
    }}


async def strategy_tips_section(replay_redis, goal: str, briefing_id: str, ab_group: str) -> Section:
    """Relevant patterns + A/B tip-shown recording keyed by briefing_id (D2/D6).

    treatment: patterns rendered, recorded shown. control: recorded (withheld)
    but patterns==[] and shown==False (consistent shape, no client render).

    FAIL-LOUD LIMITATION (accepted): `get_relevant_patterns`
    (app/patterns/store.py:222) swallows all exceptions and returns `[]`, so a
    genuine backend outage reads as "no patterns" → status "empty", NOT
    "unavailable". We do NOT re-raise in the shared store fn (the `/patterns`
    routes rely on its degrade-to-empty behavior). Accepted for this read-only
    briefing. NOTE: `record_tip_shown` is NOT wrapped this way — if the tip-shown
    write itself fails, that raise IS surfaced (as an `error` on an otherwise-ok
    section, per §5.7 — an unrecorded tip corrupts the A/B loop).
    """
    patterns = await get_relevant_patterns(replay_redis, goal=goal, limit=3)
    pattern_ids = [p.id for p in patterns]
    dumped = [{"id": p.id, "category": p.category, "confidence": p.confidence,
               "recommendation": p.recommendation or p.description} for p in patterns]

    shown = False
    error = None
    # A/B tip-effectiveness needs session volume — freeze it behind the same flag
    # as the promotion ladder (Task 1). When frozen we still return the section,
    # but with no A/B write and shown/patterns empty (the N=1 "observed" surface
    # carries the value instead).
    if pattern_ids and get_settings().PATTERN_VALIDATION_ENABLED:
        try:
            # Record for both arms so effectiveness can compare treatment vs control.
            await record_tip_shown(replay_redis, briefing_id, pattern_ids, group=ab_group)
        except Exception as exc:  # §5.7: unrecorded tips corrupt the A/B loop → don't show
            error = f"tip-shown record failed: {exc}"
            return {"status": "ok", "error": error, "data": {
                "ab_group": ab_group, "shown": False, "patterns": [],
                "briefing_id": briefing_id}}
        shown = ab_group == "treatment"

    status = "ok" if pattern_ids else "empty"
    return {"status": status, "error": error, "data": {
        "ab_group": ab_group,
        "shown": shown,
        "patterns": dumped if shown else [],   # D6: control -> []
        "briefing_id": briefing_id,
    }}


async def observed_patterns_section(replay_redis, agent_id: str, goal: str) -> Section:
    """N=1 surface: the caller's OWN recent candidate/observed patterns, described
    (NOT validated). Provenance-tagged so the agent sees the payoff of having logged
    — the value that lands on session 1, with no >=25-evidence promotion gate.

    Distinct from strategy_tips (trial+ promoted cards): these are pre-promotion,
    always labelled 'observed (unvalidated)', and NOT gated on
    PATTERN_VALIDATION_ENABLED (detectors feed this surface regardless — constraint
    #2). Degrades to 'empty' on any backend error (get_observed_patterns swallows,
    same contract as get_relevant_patterns).
    """
    pats = await get_observed_patterns(replay_redis, agent_id=agent_id, goal=goal, limit=1)
    items = [{
        "recommendation": p.recommendation or p.description,
        # PatternCard has no source_session field (models.py) -> getattr guards it;
        # real cards fall back to source_agent, the grounded provenance we have.
        # (source_agent == agent_id here, since get_observed_patterns filters on it —
        # intentional: at N=1 the provenance IS "your own prior session".)
        "provenance": (getattr(p, "source_session", "") or p.source_agent or "a prior session"),
        "confidence": p.confidence,
    } for p in pats]
    return {"status": "ok" if items else "empty", "error": None,
            "data": {"items": items, "note": "observed (unvalidated)"}}


async def cross_agent_section(replay_redis, goal: str, agent_id: str) -> Section:
    """Patterns from OTHER agents' sessions (excludes caller). No-op for 'default'.

    FAIL-LOUD LIMITATION (accepted): same as strategy_tips — the underlying
    `get_relevant_patterns` (app/patterns/store.py:222) swallows exceptions and
    returns `[]`, so a backend outage degrades this section to status "empty",
    NOT "unavailable". Not re-raised in the shared store fn (other callers
    depend on it); accepted for this read-only briefing.
    """
    if not agent_id or agent_id == "default":
        return {"status": "empty", "error": None, "data": {"patterns": []}}
    patterns = await get_relevant_patterns(replay_redis, goal=goal, limit=2, exclude_agent=agent_id)
    dumped = [{"id": p.id, "category": p.category, "confidence": p.confidence,
               "source_agent": p.source_agent,
               "recommendation": p.recommendation or p.description} for p in patterns]
    status = "ok" if dumped else "empty"
    return {"status": status, "error": None, "data": {"patterns": dumped}}


async def skills_section(vector, settings, goal: str, project: str | None) -> Section:
    """Active skills for the session goal.

    Semantic cosine match (floored at SKILL_MATCH_SCORE_FLOOR) when a goal is present;
    an ID-ordered scroll when the goal is empty — the production-NORMAL case, since a
    standard Claude Code SessionStart supplies no goal — or when the embed fails or
    nothing clears the floor.

    This was previously an inline copy of `list_skills`' scroll-then-substring shape,
    which is how one bug came to live in two files. Both now share
    `search_skill_points`. Note this section must never RAISE on an embedding failure:
    `_run_section` converts any raise or timeout into status='unavailable', which sets
    degraded=true on the whole envelope and prints '[SKILLS unavailable: ...]' into
    every session's briefing. The helper's internal fallback is what guarantees that.
    """
    must = [FieldCondition(key="memory_type", match=MatchValue(value="skill")),
            FieldCondition(key="skill_status", match=MatchValue(value="active"))]
    if project:
        must.append(FieldCondition(key="project", match=MatchValue(value=project.lower())))
    points, semantic = await search_skill_points(
        vector, settings, must=must, query=goal, limit=3,
    )
    skills = []
    ql = (goal or "").lower()
    for p in points:
        payload = p.payload or {}
        trigger = payload.get("trigger", "")
        # Ranked-and-floored points must not be re-narrowed by substring (see
        # search_skill_points' two-path contract); the legacy filter still guards the
        # degraded scroll path, where it is the only thing preventing three arbitrary
        # ID-ordered skills being presented as "relevant".
        if (ql and not semantic
                and ql not in trigger.lower()
                and ql not in payload.get("domain", "").lower()):
            continue
        skills.append({"id": str(p.id), "trigger": trigger,
                       "symptoms": payload.get("symptoms", "")})
    status = "ok" if skills else "empty"
    return {"status": status, "error": None, "data": {"skills": skills}}


async def vault_section(scopes: list[str]) -> Section:
    """Admin-gated (D4): non-admin keys get explicit omitted_reason, not silence."""
    if not vault_visible(scopes):
        return {"status": "empty", "error": None,
                "data": {"omitted_reason": "insufficient scope"}}
    secrets = await list_secrets(limit=50)
    status = "ok" if secrets else "empty"
    return {"status": status, "error": None,
            "data": {"count": len(secrets), "secrets": [
                {"key": s.get("key"), "category": s.get("category")} for s in secrets]}}


async def profile_section(vector, settings, member_id: str | None, workspace_id: str) -> Section:
    """The one continuously-updated person profile for member_id (Dreaming
    Task 8) -- closes work -> memories -> nightly dream -> next briefing.

    A direct point-id lookup on store.profile_point_id, NEVER a vector search:
    the id is already known, so search would only add latency and subject the
    read to RECALL_SCORE_FLOOR for no benefit.

    Degrades to "empty" -- never "unavailable" -- when there simply is no
    profile yet: that is the normal state on every fresh install, before the
    first dream run has profiled this member, and is not a failure. An
    unresolvable member_id (identity carries none) is the same "nothing to
    show" case and also returns empty rather than raising. A genuinely broken
    vector backend still RAISES here (per the module contract above); the
    api.py orchestrator is what converts that into status='unavailable' for
    this section alone, without touching the rest of the briefing.

    Short-circuits on DREAM_ENABLED (final-review Minor 1). api.py registers
    this section unconditionally -- deliberately, so the briefing envelope
    keeps a fixed section set on every deployment -- but with the flag off
    NOTHING can ever have written a profile point, so the retrieve is a
    guaranteed-empty Qdrant round trip on every GET /briefing of every
    deployment in existence. The check belongs here rather than at the
    registration site for that reason: the envelope shape must not depend on
    a feature flag.
    """
    if not getattr(settings, "DREAM_ENABLED", False):
        return _empty()
    if not member_id:
        return _empty()
    points = await vector._client.retrieve(
        collection_name=settings.QDRANT_COLLECTION,
        ids=[profile_point_id(member_id, workspace_id)],
        with_payload=True,
        with_vectors=False,
    )
    if not points:
        return _empty()
    payload = points[0].payload or {}
    # A profile point is REPLACED in place, never accumulated, so the only
    # status it should ever hold is "active". If something did supersede or
    # archive it (a rail failing, or a future round-2 archival path), the
    # briefing must stop rendering it rather than keep serving retired
    # content forever -- the direct point-id read bypasses every lifecycle
    # gate that ordinary recall applies, so this is the only place the check
    # can happen (final-review I1).
    if str(payload.get("status") or "active") != "active":
        return _empty()
    text = payload.get("text", "")
    if not text:
        return _empty()
    return {"status": "ok", "error": None, "data": {
        "member_id": member_id,
        "text": text,
        "updated_at": payload.get("timestamp"),
    }}


async def discipline_section(redis_client, replay_redis=None) -> Section:
    """Untagged memory-call count for the past day (mirrors /admin/untagged-calls?days=1)
    plus recall_hit_rate — the flywheel north-star, averaged over the eval window.

    Untagged counts live on the Cortex data Redis (`redis_client`); eval metrics
    live on the replay Redis (`replay_redis`, DB 6). `replay_redis` is optional so
    single-arg callers (and the untagged-only tests) keep working; when absent the
    recall_hit_rate is simply None. Status stays keyed to untagged_total (the
    discipline-lapse signal); recall_hit_rate is informational.
    """
    now = datetime.now(timezone.utc)
    by_day: dict[str, int] = {}
    total = 0
    for i in range(1):  # days=1
        date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        val = await redis_client.get(f"cortex:untagged_calls:{date}")
        n = int(val) if val else 0
        by_day[date] = n
        total += n
    # recall_hit_rate = average recall_used_rate over the eval window (mirrors the
    # window quality_section uses). get_eval_summary swallows backend errors and
    # returns an empty summary, so this never fails the section.
    recall_hit_rate = None
    if replay_redis is not None:
        summary = await get_eval_summary(replay_redis, limit=50)
        recall_hit_rate = (summary.avg_metrics or {}).get("recall_used_rate")
    status = "ok" if total > 0 else "empty"
    return {"status": status, "error": None,
            "data": {"untagged_total": total, "by_day": by_day,
                     "recall_hit_rate": recall_hit_rate}}


async def dlq_section() -> Section:
    """Backfill/distill DLQ depths + pre-worded warnings (mirrors briefing.sh 4c)."""
    depths = await collect_queue_depths()
    warnings: list[str] = []
    if depths["memory_backfill_dlq"] > 0:
        warnings.append(f"Memory backfill DLQ has {depths['memory_backfill_dlq']} stuck item(s)")
    elif depths["memory_backfill"] > 0:
        warnings.append(f"{depths['memory_backfill']} memory backfill item(s) pending")
    if depths["distill_dlq"] > 0:
        warnings.append(f"Distill DLQ has {depths['distill_dlq']} stuck distillation(s)")
    status = "ok" if warnings else "empty"
    return {"status": status, "error": None, "data": {
        "memory_backfill": depths["memory_backfill"],
        "memory_backfill_dlq": depths["memory_backfill_dlq"],
        "distill_dlq": depths["distill_dlq"],
        "warnings": warnings,
    }}


async def environment_section(http_client, settings) -> Section:
    """Sentinel full health (/environment) + recent errors (/events severity=error).

    Two upstreams composed into one section (matches the §2 schema, where
    recent_errors — a get_events shape — lives inside environment.data). The
    /environment call is primary: if it fails the section is unavailable; if
    only /events fails, recent_errors degrades to [] but the section stays ok.
    """
    key = settings.FIREKEEP_INTERNAL_KEY
    health = await _get_json(http_client, f"{settings.SENTINEL_URL}/environment", key)
    recent_errors: list = []
    try:
        events = await _get_json(
            http_client, f"{settings.SENTINEL_URL}/events", key,
            params={"severity": "error", "limit": 3},
        )
        recent_errors = events.get("events", []) or []
    except Exception:  # /events is secondary — never fail the whole section on it
        recent_errors = []
    return {"status": "ok", "error": None, "data": {
        "summary": _environment_summary(health),
        "collectors": health.get("collectors", {}),
        "event_count": health.get("event_count", 0),
        "recent_errors": recent_errors,
    }}


async def tasks_section(http_client, settings, agent_id: str) -> Section:
    """Relay pending task inbox for this agent."""
    body = await _get_json(
        http_client, f"{settings.RELAY_URL}/tasks", settings.FIREKEEP_INTERNAL_KEY,
        params={"assignee": agent_id, "status": "pending", "limit": 3},
    )
    tasks = body.get("tasks", []) or []
    status = "ok" if tasks else "empty"
    return {"status": status, "error": None, "data": {"count": len(tasks), "tasks": tasks}}


async def bulletins_section(http_client, settings) -> Section:
    """Relay bulletin board headlines."""
    body = await _get_json(
        http_client, f"{settings.RELAY_URL}/bulletin", settings.FIREKEEP_INTERNAL_KEY,
        params={"limit": 2},
    )
    posts = body.get("posts", []) or []
    status = "ok" if posts else "empty"
    return {"status": status, "error": None, "data": {"posts": posts}}


async def resumable_sessions_section(http_client, settings, agent_id: str) -> Section:
    """Bridge paused+active sessions fanned in with Relay presence for crash check.

    Bridge /sessions is primary (its failure -> section unavailable). Relay
    presence is secondary: a failure means we cannot prove liveness, so
    presence_live stays False (fail toward surfacing unfinished work).

    Crash direction rule (D1, review-corrected): an orphaned active session is
    flagged "crashed" unless a live Relay presence record is evidence against
    it — same session_id, or a presence that predates the session's last
    update (see `_presence_is_live_for`). A newer-than presence for an
    unrelated process must NOT suppress the crash flag (audit defect #20).
    """
    key = settings.FIREKEEP_INTERNAL_KEY
    paused = await _get_json(http_client, f"{settings.BRIDGE_URL}/sessions", key,
                             params={"status": "paused", "agent_id": agent_id, "limit": 3})
    active = await _get_json(http_client, f"{settings.BRIDGE_URL}/sessions", key,
                             params={"status": "active", "agent_id": agent_id, "limit": 1})

    presence = None
    try:
        presence = await _get_json(
            http_client, f"{settings.RELAY_URL}/presence/{agent_id}", key)
    except Exception:
        presence = None  # secondary — folded into crash_check below

    now = datetime.now(timezone.utc).timestamp()

    def _age_hours(iso_ts: str) -> float | None:
        ep = _to_epoch(iso_ts)
        return round((now - ep) / 3600.0, 1) if ep is not None else None

    sessions: list[dict] = []
    for s in paused.get("sessions", []) or []:
        sessions.append({
            "session_id": s["session_id"], "goal": s.get("goal", ""),
            "reason": "paused", "updated_at": s.get("updated_at", ""),
            "age_hours": _age_hours(s.get("updated_at", "")),
        })

    active_sessions = active.get("sessions", []) or []
    presence_live = False
    performed = True  # Bridge was reachable, so the crash evaluation ran
    for s in active_sessions:
        alive = _presence_is_live_for(presence, s)
        presence_live = presence_live or alive
        if not alive:
            # Orphaned active session with no live-or-predating presence -> crashed.
            sessions.append({
                "session_id": s["session_id"], "goal": s.get("goal", ""),
                "reason": "crashed", "updated_at": s.get("updated_at", ""),
                "age_hours": _age_hours(s.get("updated_at", "")),
            })

    # Recommended = newest resumable (<72h -> strong nudge).
    recommended = None
    if sessions:
        newest = min(
            sessions,
            key=lambda x: x["age_hours"] if x["age_hours"] is not None else float("inf"),
        )
        age = newest.get("age_hours")
        recommended = {"session_id": newest["session_id"], "goal": newest["goal"],
                       "age_hours": age, "strong_nudge": age is not None and age < 72}

    return {"status": "ok" if sessions else "empty", "error": None, "data": {
        "crash_check": {"performed": performed, "presence_live": presence_live},
        "sessions": sessions,
        "recommended": recommended,
    }}
