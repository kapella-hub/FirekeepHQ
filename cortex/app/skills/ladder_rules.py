"""Skill-ladder decision rules (spec: docs/superpowers/specs/2026-09-03-skill-ladder-design.md).

Pure: no I/O, no settings. Every function here turns one skill's `Evidence`
(from `app.skills.ladder_evidence`) — or, for `decide_admit`, a draft's raw
payload — into at most one `Decision`. Thresholds that the settings own
(`min_successes`, `min_agents`, `ttl_days`, `prior_n`) are passed in as
parameters; only the constants that are NOT settings-controlled live here as
module constants.

In PR1 (shadow mode) the caller (Task 7's nightly pass) only records these
decisions — nothing in this module or its caller mutates `skill_status`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.skills.ladder_evidence import Evidence, efficacy

#: Per-identity success cap already applied upstream by `ladder_evidence.gather`;
#: kept here too since callers may want to display/validate it alongside the
#: other ladder constants.
PER_AGENT_CAP = 2
PROMOTE_MIN_EFFICACY = 0.6
DEMOTE_MIN_FAILURES = 3
DEMOTE_MAX_EFFICACY = 0.4
DEMOTE_MIN_N = 5
DUP_THRESHOLD = 0.92
TRIAL_CAP_PER_DOMAIN = 10
ADMIT_PER_RUN = 20

#: Presence (with a truthy value) of any of these on a draft payload blocks
#: admission to trial. Checked in this order — the first present field wins.
PARKED_FIELDS = (
    "demoted_at",
    "ladder_rewrite_requested_at",
    "trial_expired_at",
    "superseded_by",
    "duplicate_of",
)


@dataclass(frozen=True)
class Decision:
    skill_id: str
    action: str        # "expire" | "demote" | "flag" | "promote" | "admit"
    from_status: str
    to_status: str | None    # None for "flag"
    reason: str
    evidence: dict


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp defensively: naive -> assume UTC,
    unparsable/None -> None (never raises)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _evidence_dict(ev: Evidence, prior_n: int) -> dict:
    return {
        "successes": ev.successes,
        "failures": ev.failures,
        "identities": dict(ev.identities),
        "shown": ev.shown,
        "reached": ev.reached,
        "applied": ev.applied,
        "efficacy": round(efficacy(ev, prior_n), 3),
    }


def decide_expire(skill_id: str, status: str, last_shown_at: str | None,
                   ladder_since: str | None, now: datetime,
                   ttl_days: int) -> Decision | None:
    """A trial expires when its last activity — the later of `ladder_since`
    and `last_shown_at` (a never-shown trial's last activity is
    `ladder_since`) — is at least `ttl_days` old."""
    if status != "trial":
        return None
    since_dt = _parse_dt(ladder_since)
    if since_dt is None:
        return None
    shown_dt = _parse_dt(last_shown_at)
    last_activity = max(since_dt, shown_dt) if shown_dt is not None else since_dt
    if now - last_activity < timedelta(days=ttl_days):
        return None
    return Decision(
        skill_id=skill_id,
        action="expire",
        from_status="trial",
        to_status="draft",
        reason="trial_ttl",
        evidence={},
    )


def decide_demote(skill_id: str, status: str, ev: Evidence, prior_n: int) -> Decision | None:
    """Trial only: demote back to draft on sustained low efficacy."""
    if status != "trial":
        return None
    n = ev.successes + ev.failures
    if (ev.failures >= DEMOTE_MIN_FAILURES
            and efficacy(ev, prior_n) < DEMOTE_MAX_EFFICACY
            and n >= DEMOTE_MIN_N):
        return Decision(
            skill_id=skill_id,
            action="demote",
            from_status="trial",
            to_status="draft",
            reason="low_efficacy",
            evidence=_evidence_dict(ev, prior_n),
        )
    return None


def decide_flag(skill_id: str, status: str, ev: Evidence, prior_n: int,
                 already_flagged: bool) -> Decision | None:
    """Active only: flag (never demote) on the same low-efficacy condition
    demote uses for trials."""
    if status != "active" or already_flagged:
        return None
    n = ev.successes + ev.failures
    if (ev.failures >= DEMOTE_MIN_FAILURES
            and efficacy(ev, prior_n) < DEMOTE_MAX_EFFICACY
            and n >= DEMOTE_MIN_N):
        return Decision(
            skill_id=skill_id,
            action="flag",
            from_status="active",
            to_status=None,
            reason="low_efficacy",
            evidence=_evidence_dict(ev, prior_n),
        )
    return None


def decide_promote(skill_id: str, status: str, ev: Evidence, prior_n: int, *,
                    min_successes: int, min_agents: int) -> Decision | None:
    """Trial only: promote to active once earned across distinct identities
    with zero failures and an efficacy floor."""
    if status != "trial":
        return None
    if (ev.successes >= min_successes
            and len(ev.identities) >= min_agents
            and ev.failures == 0
            and efficacy(ev, prior_n) >= PROMOTE_MIN_EFFICACY):
        return Decision(
            skill_id=skill_id,
            action="promote",
            from_status="trial",
            to_status="active",
            reason="earned",
            evidence=_evidence_dict(ev, prior_n),
        )
    return None


def admit_block_reason(payload: dict, dup_match: tuple[str, float] | None,
                        domain_trial_count: int) -> str | None:
    """The reason a draft cannot be admitted to trial, checked in order, or
    None when it is clear to admit."""
    steps = payload.get("steps")
    steps_empty = not steps if isinstance(steps, list) else not str(steps or "").strip()
    if (not str(payload.get("trigger", "")).strip()
            or not str(payload.get("symptoms", "")).strip()
            or steps_empty):
        return "incomplete"
    if payload.get("needs_rereview"):
        return "rereview"
    for field in PARKED_FIELDS:
        if payload.get(field):
            return f"parked:{field}"
    if dup_match is not None and dup_match[1] >= DUP_THRESHOLD:
        return f"duplicate:{dup_match[0]}"
    if domain_trial_count >= TRIAL_CAP_PER_DOMAIN:
        return "domain_cap"
    return None


def decide_admit(skill_id: str, payload: dict, *, dup_match: tuple[str, float] | None,
                  domain_trial_count: int) -> Decision | None:
    """Draft only: admit to trial when nothing blocks it."""
    if payload.get("skill_status") != "draft":
        return None
    if admit_block_reason(payload, dup_match=dup_match, domain_trial_count=domain_trial_count) is not None:
        return None
    return Decision(
        skill_id=skill_id,
        action="admit",
        from_status="draft",
        to_status="trial",
        reason="admitted",
        evidence={
            "dup_score": dup_match[1] if dup_match is not None else None,
            "domain_trial_count": domain_trial_count,
        },
    )


def default_ladder_since(payload: dict) -> str | None:
    """`ladder_since` default for an unstamped skill: the first truthy of
    `approved_at`, `stale_reviewed_at`, `timestamp`."""
    return (payload.get("approved_at") or payload.get("stale_reviewed_at")
            or payload.get("timestamp") or None)
