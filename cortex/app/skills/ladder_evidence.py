"""Skill-ladder evidence reader — the ONLY place that reads whether a skill
was shown, reached, applied, and how the session it appeared in graded.

Three signals, one classification, per (skill, session):

  - *shown*: a briefing receipt (`memory_read` trigger `"briefing"`) or a
    skill_recall receipt (`memory_read` trigger `"skill_recall"`) named the
    skill. A briefing impression alone is never a reach.
  - *reached*: a skill_recall receipt named the skill — the agent actually
    pulled it, not just saw it listed.
  - *applied*: a `memory_feedback` event named the skill (any `useful`
    value) — the agent judged it, not just retrieved it.

Classified once per (skill, session), after all of a session's events are
folded:

  - **success** = `grade is True` AND (`feedback_useful is True` OR
    (`feedback_useful is None` AND `reached`)) — an explicit thumbs-up wins
    outright; absent feedback, a graded-success session that actually reached
    the skill still counts.
  - **failure** = `feedback_useful is False` AND `grade is False` — a paired
    negative judgment on a graded-failure session. An unpaired thumbs-down
    (grade unknown or success) is not a failure; a failure grade with no
    feedback is not a failure either — silence is not evidence.
  - everything else counts toward shown/reached/applied only.

Successes are capped per identity (`payload.member_id` else the event's
`agent_id`, else `"unknown"`) so one agent's streak cannot promote a skill on
its own; failures are never capped. Sessions older than the caller's
per-skill `since_by_skill` cutoff (`ladder_since`) are skipped entirely for
that skill — evidence from before a skill entered the ladder does not count.

This module writes nothing and never touches Qdrant — it only reads the
replay store, mirroring the primitives `app.owm` already uses for its own
outcome join: `_default_events_fn` for a session's hydrated event list,
`session_success` for the True/False/None grade (Bridge `abandoned` overrides
to False inside that helper — this module never re-implements that rule), and
`_fetch_bridge_statuses` as the default when the caller has no pre-fetched
map. The `rp:eval_index` / `rp:eval:<sid>` scan below is the same shape
`app.owm.run_pass` uses. Task 6 (pure decision rules) consumes the `Evidence`
this produces by field name; this module makes no promotion/demotion
decisions itself.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.owm import _default_events_fn, _fetch_bridge_statuses, session_success

logger = logging.getLogger(__name__)

#: Newest-first, capped — enough for a human to spot-check without the
#: payload growing unbounded over a long trial window.
_MAX_FAILURE_SESSIONS = 5


@dataclass
class Evidence:
    shown: int = 0                              # briefing + skill_recall receipts (sessions)
    reached: int = 0                             # skill_recall receipts (sessions)
    applied: int = 0                             # feedback events (any useful value)
    successes: int = 0                           # per spec decision 2, after per-identity cap
    failures: int = 0                            # paired useful=false + failed/abandoned grade
    identities: dict[str, int] = field(default_factory=dict)   # identity -> successes counted
    last_failure_sessions: list[str] = field(default_factory=list)  # newest first, <=5
    last_shown_at: str | None = None
    last_feedback_comment: str | None = None     # never set here (payload holds it); left for callers


@dataclass
class _SkillFlags:
    """One session's exposure/feedback state for one skill, folded from its
    events. Two separate identity slots because success can be won by either
    branch of the classification formula, and each branch has its own
    contributing event."""
    shown: bool = False
    reached: bool = False
    applied: bool = False
    feedback_useful: bool | None = None
    reached_identity: str | None = None
    feedback_identity: str | None = None


def _identity_of(ev: dict, payload: dict) -> str:
    """The independence key: `payload.member_id` when present, else the
    event's `agent_id`. No `member_id` is emitted anywhere today, but the
    branch must exist and be exercised — it is forward-compatible, not dead
    code."""
    return payload.get("member_id") or ev.get("agent_id") or "unknown"


def _fold_session_events(events: list[dict]) -> dict[str, _SkillFlags]:
    """Fold one session's full event list into per-skill exposure/feedback
    flags. Recognizes only `memory_read` (trigger `briefing`/`skill_recall`)
    and `memory_feedback`; every other `memory_read` trigger (regular memory
    recall) is neither shown nor reached for ladder purposes and is ignored.
    Skill-id filtering against the caller's `since_by_skill` happens in
    `gather`, not here — this helper is unit-testable on its own."""
    flags: dict[str, _SkillFlags] = {}
    for ev in events or []:
        payload = ev.get("payload") or {}
        et = ev.get("event_type")
        if et == "memory_read":
            trigger = payload.get("trigger")
            if trigger not in ("briefing", "skill_recall"):
                continue
            for skill_id in (payload.get("memory_ids") or []):
                if not skill_id:
                    continue
                f = flags.setdefault(str(skill_id), _SkillFlags())
                f.shown = True
                if trigger == "skill_recall":
                    f.reached = True
                    f.reached_identity = _identity_of(ev, payload)
        elif et == "memory_feedback":
            for skill_id in (payload.get("memory_ids") or []):
                if not skill_id:
                    continue
                f = flags.setdefault(str(skill_id), _SkillFlags())
                f.applied = True
                f.feedback_useful = payload.get("useful")
                f.feedback_identity = _identity_of(ev, payload)
    return flags


def _classify(grade: bool | None, flags: _SkillFlags) -> str:
    """'success', 'failure', or 'none' for one (skill, session) pair, per the
    module docstring's formula."""
    if grade is True and (flags.feedback_useful is True
                          or (flags.feedback_useful is None and flags.reached)):
        return "success"
    if flags.feedback_useful is False and grade is False:
        return "failure"
    return "none"


def _success_identity(flags: _SkillFlags) -> str:
    if flags.feedback_useful is True:
        return flags.feedback_identity or "unknown"
    return flags.reached_identity or "unknown"


def _parse_ts(value: object) -> datetime | None:
    """Accept either an epoch float/int or an ISO-8601 string (replay
    `timestamp` fields are ISO strings; eval `created_at` is stored the same
    way, but this stays defensive against either shape)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return None


def _session_timestamp(eval_data: dict, events: list[dict]) -> datetime | None:
    """The session's timestamp for the `since_by_skill` cutoff: the eval's
    own `created_at` when parseable, else the earliest parseable event
    timestamp. An unknown timestamp is never treated as "too old" — it fails
    open (counted), since this reader has no basis to discard evidence it
    cannot date."""
    dt = _parse_ts(eval_data.get("created_at"))
    if dt is not None:
        return dt
    for ev in events or []:
        dt = _parse_ts(ev.get("timestamp"))
        if dt is not None:
            return dt
    return None


def _ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def gather(replay_r, settings, *, since_by_skill: dict[str, datetime],
                 events_fn=None, bridge_statuses: dict[str, str] | None = None,
                 now: datetime | None = None,
                 per_identity_cap: int = 2) -> dict[str, Evidence]:
    """One evidence pass over the outcome window, for every skill named in
    `since_by_skill` (skill ids are recognised ONLY by presence there — this
    never queries Qdrant to discover what a skill id is).

    Mirrors `app.owm.run_pass`'s own scan: `rp:eval_index` (a zset scored by
    epoch) windowed to `now - settings.OWM_WINDOW_DAYS` days, then each
    session's `rp:eval:<sid>` JSON and full (capped) event list via
    `events_fn` (defaulting to `app.owm._default_events_fn`). `bridge_statuses`
    is fetched via `app.owm._fetch_bridge_statuses` only when the caller does
    not pass a pre-fetched map — tests should always pass one so they never
    reach Bridge over the network.
    """
    events_fn = events_fn or _default_events_fn
    now = now or datetime.now(timezone.utc)
    if bridge_statuses is None:
        bridge_statuses = await _fetch_bridge_statuses(settings)

    evidence: dict[str, Evidence] = {skill_id: Evidence() for skill_id in since_by_skill}
    since_aware = {skill_id: _ensure_aware(since) for skill_id, since in since_by_skill.items()}

    window_start = now.timestamp() - settings.OWM_WINDOW_DAYS * 86400
    session_ids = await replay_r.zrangebyscore("rp:eval_index", window_start, "+inf")

    for sid_raw in session_ids:
        sid = sid_raw.decode() if isinstance(sid_raw, bytes) else sid_raw
        try:
            raw = await replay_r.get(f"rp:eval:{sid}")
            if not raw:
                continue  # dangling index entry (value TTL beat it) — same as OWM
            eval_data = json.loads(raw)
            grade = session_success(eval_data, bridge_statuses.get(sid))

            events = await events_fn(replay_r, sid)
            flags_by_skill = _fold_session_events(events)
            if not flags_by_skill:
                continue

            session_ts = _session_timestamp(eval_data, events)

            for skill_id, flags in flags_by_skill.items():
                if skill_id not in evidence:
                    continue  # not a skill this caller cares about
                since = since_aware.get(skill_id)
                if since is not None and session_ts is not None and session_ts < since:
                    continue  # evidence predates this skill's ladder_since

                ev = evidence[skill_id]
                if flags.shown:
                    ev.shown += 1
                    if session_ts is not None:
                        ev.last_shown_at = session_ts.isoformat()
                if flags.reached:
                    ev.reached += 1
                if flags.applied:
                    ev.applied += 1

                outcome = _classify(grade, flags)
                if outcome == "success":
                    identity = _success_identity(flags)
                    count = ev.identities.get(identity, 0)
                    if count < per_identity_cap:
                        ev.identities[identity] = count + 1
                        ev.successes += 1
                elif outcome == "failure":
                    ev.failures += 1
                    ev.last_failure_sessions.insert(0, sid)
                    del ev.last_failure_sessions[_MAX_FAILURE_SESSIONS:]
        except Exception as exc:  # noqa: BLE001 — one bad session never stops the pass
            logger.warning("ladder evidence: session %s skipped: %s", sid, exc)

    return evidence


def efficacy(ev: Evidence, prior_n: int) -> float:
    """Beta-shrunk success fraction over this evidence's paired outcomes:
    (successes + prior/2) / (n + prior), n = successes + failures. 0.5 at
    n=0 — same shrinkage `app.owm.compute_efficacy` uses, kept as a separate
    function because ladder evidence is a distinct `Evidence` shape, not a
    bare (successes, n) pair."""
    n = ev.successes + ev.failures
    return (ev.successes + prior_n * 0.5) / (n + prior_n)
