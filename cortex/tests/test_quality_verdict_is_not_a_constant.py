"""The briefing may report that no threshold fired. It may not call that health.

THE BUG
-------
`quality_section` ended with an unconditional else-branch:

    if total and not insights:
        insights.append("quality looks good")

Both thresholds above it -- `tool_success_rate < 0.9` and `failure_rate > 0.1`
-- are ratios over events that CARRY an outcome. Measured on the owner's live
deployment 2026-08-06 (`GET /briefing`, 19 evaluated sessions):

    event_count            48.3158     <- events per session
    tool_success_rate       1.0
    failure_rate            0.0
    sessions_with_failures  0
    insights            ["quality looks good"]

A grep of the repo shows why those are pinned: no production emitter passes
`outcome=` to `replay.emitter.emit` except Bridge's session-lifecycle calls. So
a completed session contains ~48 events of which exactly ONE carries an
outcome, and that one is the session reporting its own success. The rate is
1/1 = 1.0 and 0/1 = 0.0 by construction, neither threshold can fire, and the
else-branch ran every time.

"quality looks good" was therefore not an assessment. It was a constant wearing
a verdict's wording, on the first thing a human reads about their own system --
the same failure class as the dashboard health check that painted 404 green.

WHAT IS AND IS NOT FIXED
------------------------
As of 2026-08-23 (outcome truth), `_failure_rate` returns None on no-outcome
input too, symmetric with `_tool_success_rate` -- SUPERSEDING the note that
stood here through 2026-08-22: `owm.session_success` and the Living
Procedures Tier B gate used to key off this value, so flipping it to None
would have repointed two shipped signals. Both now grade from the recognized
`task_result` pair instead, so nothing load-bearing reads this metric and the
asymmetry is resolved. `outcome_event_count` still exists so a READER can
distinguish an informative 0.0 from an absent metric.

`TestTheOldBranchWouldFailThis` reproduces the pre-change branch against the
live numbers and asserts it produces the reassuring string, so this file cannot
become decoration.
"""
from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.briefing import sections as S
from app.evals.models import EvalResult
from app.evals.scorers import compute_tier1_metrics
from app.evals.store import store_eval


@pytest_asyncio.fixture
async def rr():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


# The live shape: one self-reported success among many outcome-less events.
LIVE_METRICS = {
    "event_count": 48.3158,
    "tool_success_rate": 1.0,
    "failure_rate": 0.0,
    "outcome_event_count": 1.0,
}


class TestTheBug:
    @pytest.mark.asyncio
    async def test_a_single_self_reported_outcome_is_not_a_clean_bill_of_health(self, rr):
        """THE regression, in the exact numbers the deployment produces."""
        await store_eval(rr, EvalResult(
            session_id="s1", trigger="session_complete",
            metrics=LIVE_METRICS, event_count=48,
        ))
        sec = await S.quality_section(rr)
        joined = " ".join(sec["data"]["insights"]).lower()

        assert "looks good" not in joined, "a silent instrument is not good news"
        assert "no failure signal" in joined
        assert "not a clean bill of health" in joined

    @pytest.mark.asyncio
    async def test_the_reader_is_told_how_thin_the_evidence_is(self, rr):
        """Naming the number is what lets someone check the claim."""
        await store_eval(rr, EvalResult(
            session_id="s1", trigger="session_complete",
            metrics=LIVE_METRICS, event_count=48,
        ))
        sec = await S.quality_section(rr)
        assert "1.0 outcome-bearing event" in " ".join(sec["data"]["insights"])


class TestItStillReportsRealSignal:
    """A change that cries wolf on healthy data is the same defect reversed."""

    @pytest.mark.asyncio
    async def test_a_real_outcome_population_gets_a_factual_all_clear(self, rr):
        await store_eval(rr, EvalResult(
            session_id="s1", trigger="session_complete",
            metrics={"tool_success_rate": 0.99, "failure_rate": 0.01,
                     "outcome_event_count": 40.0},
            event_count=60,
        ))
        sec = await S.quality_section(rr)
        joined = " ".join(sec["data"]["insights"]).lower()
        assert "no quality threshold breached" in joined
        assert "no failure signal" not in joined

    @pytest.mark.asyncio
    async def test_real_failures_still_fire_their_thresholds(self, rr):
        """The degeneracy guard must not swallow a genuine problem."""
        await store_eval(rr, EvalResult(
            session_id="s1", trigger="session_complete",
            metrics={"tool_success_rate": 0.5, "failure_rate": 0.3,
                     "outcome_event_count": 1.0},
            event_count=20,
        ))
        sec = await S.quality_section(rr)
        joined = " ".join(sec["data"]["insights"]).lower()
        assert "low tool success" in joined and "failure rate" in joined
        assert "no failure signal" not in joined, (
            "thresholds that DID fire are real evidence regardless of sample size"
        )

    @pytest.mark.asyncio
    async def test_an_eval_predating_the_metric_gets_the_neutral_wording(self, rr):
        """Stored evals carry no `outcome_event_count`. Absence of the counter
        is not evidence of degeneracy -- it must not produce the alarming line."""
        await store_eval(rr, EvalResult(
            session_id="s1", trigger="manual",
            metrics={"tool_success_rate": 1.0, "failure_rate": 0.0},
            event_count=30,
        ))
        sec = await S.quality_section(rr)
        joined = " ".join(sec["data"]["insights"]).lower()
        assert "no quality threshold breached" in joined
        assert "no failure signal" not in joined

    @pytest.mark.asyncio
    async def test_no_evals_at_all_is_still_empty_not_a_warning(self, rr):
        sec = await S.quality_section(rr)
        assert sec["status"] == "empty"
        assert sec["data"]["insights"] == []


class TestTheCounterItself:
    def test_it_counts_only_events_carrying_an_outcome(self):
        events = [
            {"event_type": "memory_read"},
            {"event_type": "memory_write"},
            {"event_type": "session.completed", "outcome": "success"},
        ]
        assert compute_tier1_metrics(events)["outcome_event_count"] == 1.0

    def test_it_is_absent_when_nothing_reports_an_outcome(self):
        """Outcome truth (2026-08-23): grading moved to the task_result pair;
        nothing keys off failure_rate and the documented asymmetry is
        resolved -- both ratios say 'cannot tell' on an empty population."""
        events = [{"event_type": "memory_read"} for _ in range(48)]
        m = compute_tier1_metrics(events)
        assert m["outcome_event_count"] == 0.0
        assert "failure_rate" not in m
        assert "tool_success_rate" not in m

    def test_an_empty_falsy_outcome_does_not_count(self):
        assert compute_tier1_metrics(
            [{"event_type": "x", "outcome": ""}]
        )["outcome_event_count"] == 0.0

    def test_it_counts_failures_too_not_just_successes(self):
        events = [
            {"event_type": "a", "outcome": "success"},
            {"event_type": "b", "outcome": "failure"},
        ]
        m = compute_tier1_metrics(events)
        assert m["outcome_event_count"] == 2.0
        assert m["failure_rate"] == 0.5


class TestTheOldBranchWouldFailThis:
    """Proof this file discriminates."""

    @staticmethod
    def _pre_change(total: int, avg: dict) -> list[str]:
        insights: list[str] = []
        tsr, fr = avg.get("tool_success_rate"), avg.get("failure_rate")
        if tsr is not None and tsr < 0.9:
            insights.append(f"low tool success ({tsr:.0%})")
        if fr is not None and fr > 0.1:
            insights.append(f"elevated failure rate ({fr:.0%})")
        if total and not insights:
            insights.append("quality looks good")
        return insights

    def test_the_old_branch_called_the_live_numbers_good(self):
        assert self._pre_change(19, LIVE_METRICS) == ["quality looks good"], (
            "expected the pre-change constant; if this changed, update the "
            "discriminator rather than deleting it"
        )

    def test_the_old_branch_could_not_be_moved_by_any_outcome_less_session(self):
        """No value of event_count or session count changes the verdict, which
        is what 'constant' means here."""
        for events in (1, 48, 10_000):
            for sessions in (1, 19, 500):
                assert self._pre_change(
                    sessions, {**LIVE_METRICS, "event_count": float(events)}
                ) == ["quality looks good"]
