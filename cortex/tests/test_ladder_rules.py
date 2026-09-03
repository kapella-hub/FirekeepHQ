"""Skill-ladder decision rules: pure functions turning one skill's Evidence
(and/or draft payload) into at most one Decision. No I/O, no settings —
thresholds are parameters or module constants."""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from app.skills.ladder_evidence import Evidence
from app.skills.ladder_rules import (
    ADMIT_PER_RUN,
    DEMOTE_MAX_EFFICACY,
    DEMOTE_MIN_FAILURES,
    DEMOTE_MIN_N,
    DUP_THRESHOLD,
    PARKED_FIELDS,
    PER_AGENT_CAP,
    PROMOTE_MIN_EFFICACY,
    TRIAL_CAP_PER_DOMAIN,
    Decision,
    admit_block_reason,
    decide_admit,
    decide_demote,
    decide_expire,
    decide_flag,
    decide_promote,
    default_ladder_since,
)
from tests.skill_payloads import real_skill_payload

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Constants sanity                                                            #
# --------------------------------------------------------------------------- #


def test_constants_exact():
    assert PER_AGENT_CAP == 2
    assert PROMOTE_MIN_EFFICACY == 0.6
    assert DEMOTE_MIN_FAILURES == 3
    assert DEMOTE_MAX_EFFICACY == 0.4
    assert DEMOTE_MIN_N == 5
    assert DUP_THRESHOLD == 0.92
    assert TRIAL_CAP_PER_DOMAIN == 10
    assert ADMIT_PER_RUN == 20
    assert PARKED_FIELDS == (
        "demoted_at",
        "ladder_rewrite_requested_at",
        "trial_expired_at",
        "superseded_by",
        "duplicate_of",
    )


# --------------------------------------------------------------------------- #
# decide_expire                                                               #
# --------------------------------------------------------------------------- #


def test_expire_young_never_shown_trial_does_not_expire():
    ladder_since = (NOW - timedelta(days=10)).isoformat()
    d = decide_expire("s1", "trial", None, ladder_since, NOW, ttl_days=60)
    assert d is None


def test_expire_old_never_shown_trial_expires():
    ladder_since = (NOW - timedelta(days=61)).isoformat()
    d = decide_expire("s1", "trial", None, ladder_since, NOW, ttl_days=60)
    assert d is not None
    assert d.skill_id == "s1"
    assert d.action == "expire"
    assert d.from_status == "trial"
    assert d.to_status == "draft"
    assert d.reason == "trial_ttl"
    assert d.evidence == {
        "ladder_since": ladder_since,
        "last_shown_at": None,
        "ttl_days": 60,
    }


def test_expire_recently_shown_old_trial_does_not_expire():
    ladder_since = (NOW - timedelta(days=90)).isoformat()
    last_shown_at = (NOW - timedelta(days=5)).isoformat()
    d = decide_expire("s1", "trial", last_shown_at, ladder_since, NOW, ttl_days=60)
    assert d is None


def test_expire_old_shown_then_stale_expires():
    ladder_since = (NOW - timedelta(days=90)).isoformat()
    last_shown_at = (NOW - timedelta(days=61)).isoformat()
    d = decide_expire("s1", "trial", last_shown_at, ladder_since, NOW, ttl_days=60)
    assert d is not None
    assert d.reason == "trial_ttl"
    assert d.evidence == {
        "ladder_since": ladder_since,
        "last_shown_at": last_shown_at,
        "ttl_days": 60,
    }


def test_expire_active_never_expires():
    ladder_since = (NOW - timedelta(days=999)).isoformat()
    d = decide_expire("s1", "active", None, ladder_since, NOW, ttl_days=60)
    assert d is None


def test_expire_unparsable_ladder_since_returns_none():
    d = decide_expire("s1", "trial", None, "not-a-date", NOW, ttl_days=60)
    assert d is None


def test_expire_exactly_at_ttl_boundary_expires():
    ladder_since = (NOW - timedelta(days=60)).isoformat()
    d = decide_expire("s1", "trial", None, ladder_since, NOW, ttl_days=60)
    assert d is not None
    assert d.evidence == {
        "ladder_since": ladder_since,
        "last_shown_at": None,
        "ttl_days": 60,
    }


def test_expire_naive_ladder_since_assumed_utc():
    ladder_since = (NOW - timedelta(days=61)).replace(tzinfo=None).isoformat()
    d = decide_expire("s1", "trial", None, ladder_since, NOW, ttl_days=60)
    assert d is not None
    assert d.evidence["ladder_since"] == ladder_since
    assert d.evidence["ttl_days"] == 60


def test_expire_naive_now_matches_aware_now():
    ladder_since = (NOW - timedelta(days=61)).isoformat()
    naive_now = NOW.replace(tzinfo=None)
    d_naive = decide_expire("s1", "trial", None, ladder_since, naive_now, ttl_days=60)
    d_aware = decide_expire("s1", "trial", None, ladder_since, NOW, ttl_days=60)
    assert d_naive is not None
    assert d_aware is not None
    assert d_naive == d_aware


def test_expire_last_shown_at_older_than_ladder_since_uses_ladder_since():
    # last_shown_at strictly older than ladder_since: max() must pick
    # ladder_since as the last-activity anchor, so a recent ladder_since
    # keeps the trial from expiring even though last_shown_at is ancient.
    ladder_since = (NOW - timedelta(days=10)).isoformat()
    last_shown_at = (NOW - timedelta(days=200)).isoformat()
    d = decide_expire("s1", "trial", last_shown_at, ladder_since, NOW, ttl_days=60)
    assert d is None


# --------------------------------------------------------------------------- #
# decide_demote                                                               #
# --------------------------------------------------------------------------- #


def test_demote_trial_low_efficacy_enough_n_demotes():
    ev = Evidence(successes=0, failures=5)
    d = decide_demote("s1", "trial", ev, prior_n=5)
    assert d is not None
    assert d.action == "demote"
    assert d.from_status == "trial"
    assert d.to_status == "draft"
    assert d.reason == "low_efficacy"
    assert d.evidence["successes"] == 0
    assert d.evidence["failures"] == 5
    assert "efficacy" in d.evidence


def test_demote_not_enough_failures_no_demote():
    # failures below DEMOTE_MIN_FAILURES (3)
    ev = Evidence(successes=0, failures=2)
    d = decide_demote("s1", "trial", ev, prior_n=5)
    assert d is None


def test_demote_efficacy_not_low_enough_no_demote():
    # efficacy = (successes + prior/2)/(n+prior) must be < 0.4
    ev = Evidence(successes=4, failures=3)  # n=7, eff=(4+2.5)/12=0.54
    d = decide_demote("s1", "trial", ev, prior_n=5)
    assert d is None


def test_demote_n_below_min_no_demote():
    # failures>=3 satisfied but total n < DEMOTE_MIN_N (5)
    ev = Evidence(successes=0, failures=3)  # n=3 < 5
    d = decide_demote("s1", "trial", ev, prior_n=5)
    assert d is None


def test_demote_non_trial_status_returns_none():
    ev = Evidence(successes=0, failures=5)
    assert decide_demote("s1", "draft", ev, prior_n=5) is None
    assert decide_demote("s1", "active", ev, prior_n=5) is None


# --------------------------------------------------------------------------- #
# decide_flag                                                                 #
# --------------------------------------------------------------------------- #


def test_flag_active_low_efficacy_flags():
    ev = Evidence(successes=0, failures=5)
    d = decide_flag("s1", "active", ev, prior_n=5, already_flagged=False)
    assert d is not None
    assert d.action == "flag"
    assert d.from_status == "active"
    assert d.to_status is None
    assert d.reason == "low_efficacy"


def test_flag_already_flagged_returns_none():
    ev = Evidence(successes=0, failures=5)
    d = decide_flag("s1", "active", ev, prior_n=5, already_flagged=True)
    assert d is None


def test_flag_non_active_status_returns_none():
    ev = Evidence(successes=0, failures=5)
    d = decide_flag("s1", "trial", ev, prior_n=5, already_flagged=False)
    assert d is None


def test_flag_efficacy_not_low_enough_no_flag():
    ev = Evidence(successes=4, failures=3)
    d = decide_flag("s1", "active", ev, prior_n=5, already_flagged=False)
    assert d is None


# --------------------------------------------------------------------------- #
# decide_promote                                                              #
# --------------------------------------------------------------------------- #


def test_promote_meets_all_thresholds():
    ev = Evidence(successes=3, failures=0, identities={"a": 2, "b": 1})
    d = decide_promote("s1", "trial", ev, prior_n=5, min_successes=3, min_agents=2)
    assert d is not None
    assert d.action == "promote"
    assert d.from_status == "trial"
    assert d.to_status == "active"
    assert d.reason == "earned"


def test_promote_one_agent_does_not_promote():
    ev = Evidence(successes=3, identities={"a": 3})
    d = decide_promote("s1", "trial", ev, prior_n=5, min_successes=3, min_agents=2)
    assert d is None


def test_promote_efficacy_floor_blocks_low_n():
    # hypothetical low thresholds: successes=1, failures=0, min_successes=1,
    # min_agents=1, prior_n=5 -> efficacy = (1+2.5)/6 = 0.5833... < 0.6
    ev = Evidence(successes=1, failures=0, identities={"a": 1})
    d = decide_promote("s1", "trial", ev, prior_n=5, min_successes=1, min_agents=1)
    assert d is None


def test_promote_any_failure_blocks():
    ev = Evidence(successes=5, failures=1, identities={"a": 2, "b": 2, "c": 1})
    d = decide_promote("s1", "trial", ev, prior_n=5, min_successes=3, min_agents=2)
    assert d is None


def test_promote_not_enough_successes_no_promote():
    ev = Evidence(successes=2, failures=0, identities={"a": 1, "b": 1})
    d = decide_promote("s1", "trial", ev, prior_n=5, min_successes=3, min_agents=2)
    assert d is None


def test_promote_non_trial_status_returns_none():
    ev = Evidence(successes=3, failures=0, identities={"a": 2, "b": 1})
    d = decide_promote("s1", "active", ev, prior_n=5, min_successes=3, min_agents=2)
    assert d is None


# --------------------------------------------------------------------------- #
# admit_block_reason / decide_admit                                          #
# --------------------------------------------------------------------------- #


def _clean_payload(**over):
    """A draft shaped the way `POST /skills` actually stores one (see
    tests/skill_payloads.py): the steps live in `content` under `## Steps`,
    NOT in a `steps` payload key. `steps=` here is the CONTENT body, so
    `steps=""` produces the real "heading present, body empty" shape."""
    return real_skill_payload(**over)


def test_admit_block_reason_incomplete_missing_trigger():
    payload = _clean_payload(trigger="")
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) == "incomplete"


def test_admit_block_reason_incomplete_missing_symptoms():
    payload = _clean_payload(symptoms="   ")
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) == "incomplete"


def test_admit_block_reason_incomplete_empty_steps_list():
    # An explicit (forward-compatible) `steps` key that is empty falls THROUGH
    # to the content body; with that empty too, neither source supplies steps.
    payload = _clean_payload(steps="")
    payload["steps"] = []
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) == "incomplete"


def test_admit_block_reason_incomplete_whitespace_steps_string():
    payload = _clean_payload(steps="   ")
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) == "incomplete"


def test_admit_block_reason_incomplete_explicit_none_trigger():
    payload = _clean_payload(trigger=None)
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) == "incomplete"


def test_admit_block_reason_incomplete_explicit_none_symptoms():
    payload = _clean_payload(symptoms=None)
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) == "incomplete"


def test_admit_block_reason_incomplete_wins_over_duplicate():
    payload = _clean_payload(trigger="", duplicate_of="other-id")
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) == "incomplete"


def test_admit_block_reason_parked_field_with_none_value_not_parked():
    payload = _clean_payload(demoted_at=None)
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) is None


def test_admit_block_reason_duplicate_of_none_un_parks_the_draft():
    """I3's clear path relies on this: `PATCH {clear_duplicate_of: true}`
    writes `duplicate_of: None`, and PARKED_FIELDS is present-AND-TRUTHY, so
    the key survives on the payload while no longer blocking admission."""
    payload = _clean_payload(duplicate_of=None)
    assert "duplicate_of" in payload
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) is None


def test_admit_block_reason_real_draft_with_steps_in_content_admits():
    """C1, the regression this suite previously certified: a draft created
    through `POST /skills` carries NO `steps` payload key — the steps are in
    `content` under `## Steps` — and it must be admissible."""
    payload = _clean_payload()
    assert "steps" not in payload
    assert "## Steps" in payload["content"]
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) is None


def test_admit_block_reason_real_draft_with_empty_steps_body_is_incomplete():
    """The heading alone is not steps: `create_skill` emits `## Steps\\n{steps}`
    even when the author supplied none, so the BODY is what is tested."""
    payload = _clean_payload(steps="")
    assert "## Steps" in payload["content"]
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) == "incomplete"


def test_admit_block_reason_explicit_steps_key_without_content_admits():
    """Forward-compatible: if a future writer stores a real `steps` key, it is
    read directly and no `content` is needed."""
    payload = _clean_payload()
    del payload["content"]
    payload["steps"] = ["do a", "do b"]
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) is None


def test_admit_block_reason_steps_body_survives_a_hash_inside_a_step():
    """`## ` is only a terminator at line start — a shell comment inside a step
    must not truncate the body to nothing."""
    payload = _clean_payload(steps="run `docker ps ## note` then restart")
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) is None


def test_admit_block_reason_rereview():
    payload = _clean_payload(needs_rereview=True)
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) == "rereview"


def test_admit_block_reason_parked_field_first_in_order():
    # both demoted_at and duplicate_of present -> demoted_at wins (first in PARKED_FIELDS)
    payload = _clean_payload(demoted_at="2026-01-01", duplicate_of="other-id")
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) == "parked:demoted_at"


def test_admit_block_reason_each_parked_field():
    for field in PARKED_FIELDS:
        payload = _clean_payload(**{field: "x"})
        assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) == f"parked:{field}"


def test_admit_block_reason_duplicate():
    payload = _clean_payload()
    assert admit_block_reason(payload, dup_match=("other-id", 0.95), domain_trial_count=0) == "duplicate:other-id"


def test_admit_block_reason_duplicate_below_threshold_not_blocked():
    payload = _clean_payload()
    assert admit_block_reason(payload, dup_match=("other-id", 0.91), domain_trial_count=0) is None


def test_admit_block_reason_domain_cap_at_exactly_ten():
    payload = _clean_payload()
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=10) == "domain_cap"


def test_admit_block_reason_domain_cap_below_ten_ok():
    payload = _clean_payload()
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=9) is None


def test_admit_block_reason_clean_returns_none():
    payload = _clean_payload()
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) is None


def test_admit_block_reason_order_incomplete_before_rereview():
    payload = _clean_payload(trigger="", needs_rereview=True)
    assert admit_block_reason(payload, dup_match=None, domain_trial_count=0) == "incomplete"


def test_decide_admit_clean_draft_admits():
    payload = _clean_payload()
    d = decide_admit("s1", payload, dup_match=None, domain_trial_count=0)
    assert d is not None
    assert d.action == "admit"
    assert d.from_status == "draft"
    assert d.to_status == "trial"
    assert d.reason == "admitted"
    assert d.evidence == {"dup_score": None, "domain_trial_count": 0}


def test_decide_admit_blocked_payload_returns_none():
    payload = _clean_payload(needs_rereview=True)
    d = decide_admit("s1", payload, dup_match=None, domain_trial_count=0)
    assert d is None


def test_decide_admit_non_draft_status_returns_none():
    payload = _clean_payload(skill_status="trial")
    d = decide_admit("s1", payload, dup_match=None, domain_trial_count=0)
    assert d is None


def test_decide_admit_carries_dup_score_in_evidence():
    payload = _clean_payload()
    d = decide_admit("s1", payload, dup_match=("other-id", 0.5), domain_trial_count=3)
    assert d is not None
    assert d.evidence == {"dup_score": 0.5, "domain_trial_count": 3}


# --------------------------------------------------------------------------- #
# default_ladder_since                                                        #
# --------------------------------------------------------------------------- #


def test_default_ladder_since_prefers_approved_at():
    payload = {"approved_at": "a", "stale_reviewed_at": "b", "timestamp": "c"}
    assert default_ladder_since(payload) == "a"


def test_default_ladder_since_falls_back_to_stale_reviewed_at():
    payload = {"stale_reviewed_at": "b", "timestamp": "c"}
    assert default_ladder_since(payload) == "b"


def test_default_ladder_since_falls_back_to_timestamp():
    payload = {"timestamp": "c"}
    assert default_ladder_since(payload) == "c"


def test_default_ladder_since_all_none_returns_none():
    assert default_ladder_since({}) is None


def test_default_ladder_since_falsy_values_skipped():
    payload = {"approved_at": "", "stale_reviewed_at": None, "timestamp": "c"}
    assert default_ladder_since(payload) == "c"


# --------------------------------------------------------------------------- #
# Decision dataclass shape                                                    #
# --------------------------------------------------------------------------- #


def test_decision_is_frozen():
    d = Decision(skill_id="s1", action="admit", from_status="draft",
                 to_status="trial", reason="admitted", evidence={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.action = "promote"
