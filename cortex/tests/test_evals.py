"""Tests for the auto-eval system — models and Tier 1 scorers."""

import json
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.evals.models import EvalResult, EvalSummary
from app.evals.scorers import compute_tier1_metrics


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

def _make_events(specs: list[dict]) -> list[dict]:
    """Helper to create mock replay events from specs."""
    events = []
    for i, spec in enumerate(specs):
        events.append({
            "id": f"event-{i}",
            "event_type": spec.get("type", "ctx_update"),
            "outcome": spec.get("outcome"),
            "timestamp": f"2026-03-18T10:{i:02d}:00+00:00",
            "payload": spec.get("payload", {}),
            "context_ref": spec.get("context_ref"),
            "session_id": "test-session",
            "agent_id": "default",
        })
    return events


@pytest_asyncio.fixture
async def rr():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


async def _compute(monkeypatch, events, *, id_events=None, task_result_hint=None,
                   replay_redis=None, webhook_sink=None):
    """Drive compute_session_eval with patched replay readers.

    Task 4 (outcome truth PR2 D3): compute_session_eval's metrics scan AND
    find_terminal_grade's grade lift both read through the SAME
    (get_session_event_ids, get_event_batch) primitives now — there is no
    separate get_session_timeline fetch to fake. `id_events`, when given
    (truthy), models the session-tail SNAPSHOT as an ordered (oldest→newest)
    list of (event_id, event_or_None); None means the body is MISSING from
    get_event_batch (trimmed/expired) — the snapshot ID stays but hydration
    omits it. This exercises find_terminal_grade's real shape: snapshot the
    IDs once, hydrate backward in windows. When `id_events` is omitted/empty,
    the backing snapshot is derived from `events` instead (each already
    carries an "id" from _make_events) — the common case, where a test only
    cares about the metrics/Tier1 side and has no grade-lift scenario to model.

    replay_redis defaults to a fresh fakeredis so store_eval actually persists
    (under the authoritative-store rule an unstored candidate returns None)."""
    import replay.reader as reader_mod
    from app.evals import compute as compute_mod

    owned_redis = replay_redis is None
    if owned_redis:
        import fakeredis.aioredis
        replay_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    if id_events:
        pairs = list(id_events)
    else:
        pairs = [(e.get("id") or f"auto-{i}", e) for i, e in enumerate(events)]
    id_to_ev = {i: ev for i, ev in pairs if ev is not None}

    async def fake_summary(*a, **k):
        return {"event_count": max(len(pairs), 1), "duration_ms": 1000,
                "agents": ["default"]}

    async def fake_ids(r, sid, *, limit=5000):
        return [i for i, _ in pairs][-limit:]

    async def fake_batch(r, ids):
        return [id_to_ev[i] for i in ids if i in id_to_ev]

    monkeypatch.setattr(reader_mod, "get_session_summary", fake_summary)
    monkeypatch.setattr(reader_mod, "get_session_event_ids", fake_ids)
    monkeypatch.setattr(reader_mod, "get_event_batch", fake_batch)
    # Keep every test in-process: the production webhook client targets the
    # compose hostname `redis`, which is deliberately unreachable here.
    from unittest.mock import AsyncMock
    import app.webhooks as webhooks_mod
    sink = webhook_sink if webhook_sink is not None else AsyncMock()
    webhook_redis = AsyncMock()
    monkeypatch.setattr(compute_mod.aioredis, "from_url",
                        lambda *a, **k: webhook_redis)
    monkeypatch.setattr(webhooks_mod, "fire_webhooks", sink)
    try:
        return await compute_mod.compute_session_eval(
            replay_redis=replay_redis, session_id="s1",
            task_result_hint=task_result_hint)
    finally:
        if owned_redis:
            await replay_redis.aclose()


# ---------------------------------------------------------------------------
# Scorer tests
# ---------------------------------------------------------------------------


class TestComputeTier1Metrics:
    def test_empty_events(self):
        assert compute_tier1_metrics([]) == {}

    def test_event_count(self):
        events = _make_events([{"type": "ctx_update"}, {"type": "memory_read"}])
        metrics = compute_tier1_metrics(events)
        assert metrics["event_count"] == 2.0

    def test_tool_success_rate_all_success(self):
        events = _make_events([
            {"type": "memory_read", "outcome": "success"},
            {"type": "memory_write", "outcome": "success"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["tool_success_rate"] == 1.0

    def test_tool_success_rate_mixed(self):
        events = _make_events([
            {"type": "memory_read", "outcome": "success"},
            {"type": "memory_read", "outcome": "failure"},
            {"type": "ctx_update", "outcome": "success"},
            {"type": "claim", "outcome": "failure"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["tool_success_rate"] == 0.5

    def test_tool_success_rate_no_outcomes(self):
        events = _make_events([
            {"type": "ctx_update"},
            {"type": "session_start"},
        ])
        metrics = compute_tier1_metrics(events)
        assert "tool_success_rate" not in metrics  # None excluded

    def test_memory_read_count(self):
        events = _make_events([
            {"type": "memory_read"},
            {"type": "memory_read"},
            {"type": "ctx_update"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["memory_read_count"] == 2.0

    def test_memory_write_count(self):
        events = _make_events([
            {"type": "memory_write"},
            {"type": "ctx_update"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["memory_write_count"] == 1.0

    def test_memory_freshness_at_recall(self):
        events = _make_events([
            {"type": "memory_read", "payload": {"top_score": 0.9}},
            {"type": "memory_read", "payload": {"top_score": 0.7}},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["memory_freshness_at_recall"] == 0.8

    def test_memory_freshness_no_reads(self):
        events = _make_events([{"type": "ctx_update"}])
        metrics = compute_tier1_metrics(events)
        assert "memory_freshness_at_recall" not in metrics

    def test_claim_contention_rate(self):
        events = _make_events([
            {"type": "claim", "outcome": "success"},
            {"type": "claim", "outcome": "failure"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["claim_contention_rate"] == 0.5

    def test_failure_rate(self):
        events = _make_events([
            {"type": "memory_read", "outcome": "success"},
            {"type": "memory_read", "outcome": "success"},
            {"type": "ctx_update", "outcome": "failure"},
        ])
        metrics = compute_tier1_metrics(events)
        assert abs(metrics["failure_rate"] - 0.3333) < 0.01

    def test_session_duration(self):
        events = _make_events([
            {"type": "session_start"},
            {"type": "ctx_update"},
            {"type": "ctx_update"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["session_duration_ms"] == 120000.0  # 2 minutes

    def test_unique_event_types(self):
        events = _make_events([
            {"type": "session_start"},
            {"type": "memory_read"},
            {"type": "memory_read"},
            {"type": "claim"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["unique_event_types"] == 3.0

    def test_context_snapshot_count(self):
        events = _make_events([
            {"type": "ctx_update", "context_ref": "abc123"},
            {"type": "ctx_update"},
            {"type": "ctx_update", "context_ref": "def456"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["context_snapshot_count"] == 2.0


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestEvalResult:
    def test_minimal(self):
        result = EvalResult(
            session_id="test-1",
            trigger="session_complete",
        )
        assert result.event_count == 0
        assert result.metrics == {}
        assert result.has_failures is False

    def test_with_metrics(self):
        result = EvalResult(
            session_id="test-2",
            trigger="session_complete",
            metrics={"tool_success_rate": 0.85, "event_count": 42.0},
            event_count=42,
            has_failures=False,
        )
        assert result.metrics["tool_success_rate"] == 0.85

    def test_with_failures(self):
        result = EvalResult(
            session_id="test-3",
            trigger="session_abandon",
            failure_event_ids=["ev-1", "ev-2"],
            has_failures=True,
        )
        assert len(result.failure_event_ids) == 2


class TestEvalSummary:
    def test_empty(self):
        summary = EvalSummary()
        assert summary.total_sessions_evaluated == 0

    def test_with_data(self):
        summary = EvalSummary(
            total_sessions_evaluated=10,
            sessions_with_failures=2,
            avg_metrics={"tool_success_rate": 0.9},
        )
        assert summary.avg_metrics["tool_success_rate"] == 0.9


# ---------------------------------------------------------------------------
# compute_session_eval integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_includes_brier_score_when_predict_events_present(monkeypatch):
    """If replay has predict + reconcile events, brier_score appears in metrics."""
    import replay.reader as reader_mod
    from app.evals import compute as compute_mod
    from app.evals.store import store_eval  # noqa: F401 — imported to allow patching

    fake_events = [
        {
            "event_type": "agent.action.predict",
            "payload": {"action_id": "a1", "prediction": {"confidence": 0.9}},
        },
        {
            "event_type": "agent.action.reconcile",
            "payload": {"action_id": "a1", "prediction_match_score": 1.0},
        },
        {
            "event_type": "agent.action.predict",
            "payload": {"action_id": "a2", "prediction": {"confidence": 0.3}},
        },
        {
            "event_type": "agent.action.reconcile",
            "payload": {"action_id": "a2", "prediction_match_score": 0.0},
        },
    ]

    async def fake_get_session_summary(*args, **kwargs):
        return {"event_count": len(fake_events), "duration_ms": 1000}

    # Task 4: the metrics scan and find_terminal_grade's grade lift both read
    # through get_session_event_ids/get_event_batch now (no get_session_timeline
    # call left in compute_session_eval) — fake both to serve fake_events.
    async def fake_ids(*args, **kwargs):
        return [f"b{i}" for i in range(len(fake_events))]

    async def fake_batch(*args, **kwargs):
        return list(fake_events)

    saved = {}

    async def fake_store_eval(_r, result):
        saved["result"] = result
        return True

    async def fake_get_eval(_r, _sid):
        return saved.get("result")

    # Patch replay reader functions used inside compute_session_eval
    monkeypatch.setattr(reader_mod, "get_session_summary", fake_get_session_summary)
    monkeypatch.setattr(reader_mod, "get_session_event_ids", fake_ids)
    monkeypatch.setattr(reader_mod, "get_event_batch", fake_batch)

    # Patch store_eval/get_eval so we don't need Redis
    monkeypatch.setattr(compute_mod, "store_eval", fake_store_eval)
    monkeypatch.setattr(compute_mod, "get_eval", fake_get_eval)
    monkeypatch.setattr(
        compute_mod.aioredis, "from_url", lambda *a, **k: AsyncMock())
    monkeypatch.setattr("app.webhooks.fire_webhooks", AsyncMock())

    # Also disable pattern extraction and webhooks side effects
    monkeypatch.setenv("EVAL_LLM_ENABLED", "false")

    result = await compute_mod.compute_session_eval(
        replay_redis=None,  # type: ignore[arg-type]
        session_id="s1",
    )

    assert result is not None, "compute_session_eval returned None unexpectedly"
    # (0.9 - 1.0)^2 + (0.3 - 0.0)^2 = 0.01 + 0.09 = 0.10 / 2 = 0.05
    assert result.metrics.get("brier_score") == pytest.approx(0.05, abs=1e-4)


# ---------------------------------------------------------------------------
# Outcome truth (2026-08-23): normalizers, snapshot-scanned grade lift,
# first-graded-wins store, authoritative downstream.
# ---------------------------------------------------------------------------


class TestNormalizers:
    def test_pair_normalizer(self):
        from app.evals.models import recognized_grade_pair
        assert recognized_grade_pair("success", "self_reported") == ("success", "self_reported")
        assert recognized_grade_pair("success", None) == (None, None)
        assert recognized_grade_pair("success", "vibes") == (None, None)
        assert recognized_grade_pair("amazing", "self_reported") == (None, None)

    def test_binary_outcome_projection(self):
        from app.evals.models import binary_outcome
        assert binary_outcome("success") == "success"
        assert binary_outcome("failure") == "failure"
        assert binary_outcome("partial") == "unknown"
        assert binary_outcome(None) == "unknown"

    def test_before_validator_normalizes_invalid_literals_without_raising(self):
        """mode='after' would never run: Literal field validation raises
        first. The before-validator normalizes the RAW mapping (verified
        under Pydantic 2.12.5)."""
        from app.evals.models import EvalResult
        m = EvalResult(session_id="s", trigger="manual",
                       task_result="amazing", task_result_source="self_reported")
        assert m.task_result is None and m.task_result_source is None
        m = EvalResult(session_id="s", trigger="manual",
                       task_result="success", task_result_source="vibes")
        assert m.task_result is None and m.task_result_source is None
        m = EvalResult(session_id="s", trigger="manual", task_result="success")
        assert m.task_result is None and m.task_result_source is None
        raw = ('{"session_id": "s", "trigger": "manual", '
               '"task_result": "amazing", "task_result_source": 3}')
        m = EvalResult.model_validate_json(raw)     # junk stored record parses
        assert m.task_result is None

    def test_grade_from_events_reads_both_terminal_channels(self):
        from app.evals.models import grade_from_events
        ok = {"task_result": "success", "task_result_source": "self_reported"}
        assert grade_from_events(
            [{"event_type": "session.completed", "payload": ok}]) == ("success", "self_reported")
        assert grade_from_events(
            [{"event_type": "session.completed",
              "payload": {"task_result": "success"}}]) == (None, None)
        assert grade_from_events(
            [{"event_type": "ctx_update", "payload": ok}]) == (None, None)

    def test_grade_from_events_tolerates_a_non_dict_payload(self):
        """Round-6 finding 6: a non-empty list/string payload must degrade to
        (None, None), not raise into the eval catch-all and DLQ the whole
        computation — `p = payload or {}` keeps a truthy non-dict, so the
        isinstance guard is load-bearing."""
        from app.evals.models import grade_from_events
        assert grade_from_events(
            [{"event_type": "session_end", "payload": ["not", "a", "dict"]}]) == (None, None)
        assert grade_from_events(
            [{"event_type": "session_end", "payload": "junk"}]) == (None, None)


class TestTaskResultLifting:
    @pytest.mark.asyncio
    async def test_hint_wins_and_survives_a_lost_emit(self, monkeypatch):
        result = await _compute(monkeypatch, _make_events([{"type": "memory_read"}]),
                                id_events=[], task_result_hint="failure")
        assert result.task_result == "failure"
        assert result.task_result_source == "self_reported"
        assert result.outcome == "failure"

    @pytest.mark.asyncio
    async def test_lift_finds_the_grade_under_post_completion_noise(self, monkeypatch):
        """D7: the graded session_end sits early, then 250 newer events. The
        snapshot names all of them; hydrating backward in windows finds the
        grade in a later window."""
        grade_ev = {"event_type": "session_end",
                    "payload": {"task_result": "success",
                                "task_result_source": "self_reported"}}
        # oldest→newest: the grade, then 250 noise events (all newer)
        pairs = [("g", grade_ev)] + [
            (f"n{i}", {"event_type": "memory_read", "payload": {}}) for i in range(250)]
        result = await _compute(monkeypatch, _make_events([{"type": "memory_read"}]),
                                id_events=pairs)
        assert result.task_result == "success"

    @pytest.mark.asyncio
    async def test_lift_walks_past_a_hole_of_missing_bodies(self, monkeypatch):
        """Finding 6: some IDs in the newest window have NO body (trimmed /
        expired). Iterating IDs (not hydrated events) must keep walking to the
        grade; a hydrated-count terminator would stop early."""
        grade_ev = {"event_type": "session_end",
                    "payload": {"task_result": "failure",
                                "task_result_source": "self_reported"}}
        # grade oldest; then 50 present noise; then 200 MISSING bodies (newest)
        pairs = ([("g", grade_ev)]
                 + [(f"n{i}", {"event_type": "memory_read", "payload": {}})
                    for i in range(50)]
                 + [(f"m{i}", None) for i in range(200)])
        result = await _compute(monkeypatch, _make_events([{"type": "memory_read"}]),
                                id_events=pairs)
        assert result.task_result == "failure"

    @pytest.mark.asyncio
    async def test_append_after_snapshot_cannot_shift_the_scan(self, monkeypatch, rr):
        """Capture IDs once, then append 300 newer IDs while the first window
        hydrates. The frozen list still reaches the original terminal grade."""
        from app.evals import compute as compute_mod
        from replay import reader as reader_mod

        grade = {"event_type": "session_end", "payload": {
            "task_result": "success", "task_result_source": "self_reported"}}
        frozen = ["grade"] + [f"old-{i}" for i in range(250)]
        bodies = {"grade": grade, **{
            f"old-{i}": {"event_type": "memory_read", "payload": {}}
            for i in range(250)}}
        calls = {"ids": 0, "batches": 0}

        async def snapshot(*args, **kwargs):
            calls["ids"] += 1
            return list(frozen)

        async def hydrate(r, ids):
            calls["batches"] += 1
            if calls["batches"] == 1:
                # Mutate the live backing set after the snapshot. A second
                # rank-relative read would now shift; this implementation has none.
                frozen.extend(f"new-{i}" for i in range(300))
            return [bodies[i] for i in ids if i in bodies]

        monkeypatch.setattr(reader_mod, "get_session_event_ids", snapshot)
        monkeypatch.setattr(reader_mod, "get_event_batch", hydrate)
        assert await compute_mod.find_terminal_grade(rr, "s1") == (
            "success", "self_reported")
        assert calls["ids"] == 1

    @pytest.mark.asyncio
    async def test_ungraded_session_reads_unknown_not_success(self, monkeypatch):
        result = await _compute(monkeypatch,
                                _make_events([{"type": "session_end"}]), id_events=[])
        assert result.task_result is None
        assert result.outcome == "unknown"


class TestMetricsScanCap:
    """Task 4 (outcome truth PR2 D3): the metrics scan (Tier1 metrics, the
    Brier join, failure_event_ids) must see the WHOLE session via the PR1
    snapshot+hydrate primitives, not the oldest-1000 window
    get_session_timeline used to silently truncate to — and the cap is made
    explicit via `metrics_truncated` rather than dropped silently."""

    @pytest.mark.asyncio
    async def test_metrics_include_late_events_beyond_1000(self, monkeypatch):
        filler = [
            (f"e{i}", {"id": f"e{i}", "event_type": "memory_read",
                       "payload": {}, "outcome": "success"})
            for i in range(1200)
        ]
        late_fail = ("late-fail", {"id": "late-fail", "event_type": "tool_call",
                                   "payload": {}, "outcome": "failure"})
        pairs = filler + [late_fail]
        result = await _compute(monkeypatch, [], id_events=pairs)
        assert result is not None
        # Old oldest-1000 window would have dropped this — it sits at index
        # 1200 of 1201 events.
        assert "late-fail" in result.failure_event_ids
        assert result.metrics_truncated is False  # 1201 < 5000

    @pytest.mark.asyncio
    async def test_metrics_truncated_flag_at_cap(self, monkeypatch):
        pairs = [
            (f"e{i}", {"id": f"e{i}", "event_type": "memory_read",
                       "payload": {}, "outcome": "success"})
            for i in range(5001)
        ]
        result = await _compute(monkeypatch, [], id_events=pairs)
        assert result is not None
        assert result.metrics_truncated is True


class TestRaceSafeStore:
    @pytest.mark.asyncio
    async def test_graded_replaces_ungraded_but_nothing_else(self, rr):
        from app.evals.models import EvalResult
        from app.evals.store import store_eval, get_eval
        ungraded = EvalResult(session_id="s1", trigger="session_complete")
        graded = EvalResult(session_id="s1", trigger="manual",
                            task_result="success", task_result_source="self_reported")
        assert await store_eval(rr, ungraded) is True
        assert await store_eval(rr, ungraded) is False           # idempotent
        assert await store_eval(rr, graded) is True              # upgrade
        assert (await get_eval(rr, "s1")).task_result == "success"
        regraded = EvalResult(session_id="s1", trigger="manual",
                              task_result="failure", task_result_source="self_reported")
        assert await store_eval(rr, regraded) is False           # first-graded-wins

    @pytest.mark.asyncio
    async def test_an_ungraded_writer_can_never_clobber_a_grade(self, rr):
        """D9a: the ungraded path writes ONLY via SET NX — under ANY
        interleaving it cannot overwrite."""
        from app.evals.models import EvalResult
        from app.evals.store import store_eval, get_eval
        graded = EvalResult(session_id="s2", trigger="session_complete",
                            task_result="failure", task_result_source="self_reported")
        assert await store_eval(rr, graded) is True
        assert await store_eval(rr, EvalResult(session_id="s2", trigger="manual")) is False
        assert (await get_eval(rr, "s2")).task_result == "failure"

    @pytest.mark.asyncio
    async def test_concurrent_mixed_writers_leave_a_graded_record(self, rr):
        """D9b (round-5): the time-limited claim is GONE — WATCH/MULTI CAS.
        Gather ungraded + graded writers for one session; postconditions hold
        under any scheduling: the final record is graded, and exactly one
        graded writer reports True."""
        import asyncio
        from app.evals.models import EvalResult
        from app.evals.store import store_eval, get_eval

        def _u(): return EvalResult(session_id="sc", trigger="session_complete")
        def _g(tag): return EvalResult(session_id="sc", trigger="manual",
                                       task_result=tag,
                                       task_result_source="self_reported")
        results = await asyncio.gather(
            store_eval(rr, _u()), store_eval(rr, _g("success")),
            store_eval(rr, _u()), store_eval(rr, _g("failure")),
            store_eval(rr, _u()),
        )
        final = await get_eval(rr, "sc")
        assert final.task_result in ("success", "failure")     # a grade won
        assert sum(1 for i, ok in enumerate(results)
                   if ok and i in (1, 3)) == 1                  # one graded True

    @pytest.mark.asyncio
    async def test_a_stale_watcher_retries_and_observes_the_competing_grade(
        self, rr, monkeypatch
    ):
        """D9b: a graded writer whose WATCHed key changed under it must
        EXEC-fail and RETRY (re-read → re-decide), never overwrite from a
        stale read — the successor-lock-deletion class the old fixed-TTL claim
        reintroduced. Wrap rr.pipeline so the FIRST execute() raises
        WatchError; before raising, inject the competing grade. The retry must
        re-read it, lose first-graded-wins, and return False."""
        import redis
        from app.evals.models import EvalResult
        from app.evals.store import store_eval, get_eval

        real_pipeline = rr.pipeline
        state = {"failed": False, "pipelines": 0}
        competitor = EvalResult(
            session_id="sw", trigger="manual", task_result="failure",
            task_result_source="self_reported")

        def flaky_pipeline(*a, **k):
            state["pipelines"] += 1
            pipe = real_pipeline(*a, **k)
            real_execute = pipe.execute

            async def once_failing_execute(*ea, **ek):
                if not state["failed"]:
                    state["failed"] = True
                    # A second client won between this pipeline's WATCH/read
                    # and EXEC. Use the unwrapped client SET to make that fact real.
                    await rr.set("rp:eval:sw", competitor.model_dump_json(), ex=86400)
                    raise redis.WatchError("simulated concurrent change")
                return await real_execute(*ea, **ek)

            pipe.execute = once_failing_execute
            return pipe

        monkeypatch.setattr(rr, "pipeline", flaky_pipeline)
        graded = EvalResult(session_id="sw", trigger="session_complete",
                            task_result="success", task_result_source="self_reported")
        assert await store_eval(rr, graded) is False    # competing grade already won
        assert state["failed"] is True                  # the first EXEC did fail
        assert state["pipelines"] >= 2                  # it re-read on a fresh pipeline
        assert (await get_eval(rr, "sw")).task_result == "failure"


class TestAuthoritativeDownstream:
    @pytest.mark.asyncio
    async def test_a_rejected_write_yields_the_stored_record(self, monkeypatch, rr):
        from app.evals.models import EvalResult
        from app.evals.store import store_eval
        graded = EvalResult(session_id="s1", trigger="session_complete",
                            task_result="success", task_result_source="self_reported")
        assert await store_eval(rr, graded) is True
        result = await _compute(monkeypatch, _make_events([{"type": "memory_read"}]),
                                id_events=[], replay_redis=rr)
        assert result.task_result == "success"                    # the STORED record

    @pytest.mark.asyncio
    async def test_no_persisted_record_aborts_downstream(self, monkeypatch, rr):
        """D9c: store False + reload None (infra failure) must NOT let the
        candidate drive features/webhooks/the response."""
        import app.evals.compute as compute_mod
        extracted: list = []

        async def _no_store(r, result, **kw):
            return False

        async def _no_get(r, sid):
            return None

        async def _spy_extract(*a, **kw):
            extracted.append(1)

        monkeypatch.setattr(compute_mod, "store_eval", _no_store)
        monkeypatch.setattr(compute_mod, "get_eval", _no_get)
        monkeypatch.setattr("app.patterns.extractor.extract_session_features",
                            _spy_extract, raising=False)
        fired = AsyncMock()
        result = await _compute(
            monkeypatch, _make_events([{"type": "memory_read"}]),
            id_events=[], replay_redis=rr, webhook_sink=fired)
        assert result is None
        assert extracted == []
        fired.assert_not_awaited()
        dlq = json.loads(await rr.get("rp:eval_dlq:s1"))
        assert dlq["failure_type"] == "store"

    @pytest.mark.asyncio
    async def test_superseded_candidate_webhook_uses_stored_winner(
        self, monkeypatch, rr
    ):
        """D9f: accepted-ungraded → graded upgrade before the final read.
        Both webhook payloads must carry the complete stored pair."""
        from app.evals import compute as compute_mod
        from app.evals.models import EvalResult
        from app.evals.store import store_eval

        winner = EvalResult(
            session_id="s1", trigger="manual", task_result="success",
            task_result_source="self_reported", outcome="success")
        real_get = compute_mod.get_eval
        reads = {"n": 0}

        async def superseding_get(r, sid):
            reads["n"] += 1
            if reads["n"] == 1:
                assert await store_eval(r, winner) is True
            return await real_get(r, sid)

        fired = AsyncMock()
        monkeypatch.setattr(compute_mod, "get_eval", superseding_get)
        result = await _compute(
            monkeypatch, _make_events([{"type": "memory_read"}]),
            id_events=[], replay_redis=rr, webhook_sink=fired)
        assert result.task_result == "success"
        assert fired.await_count == 2
        for call in fired.await_args_list:
            payload = call.args[2]
            assert (payload["task_result"], payload["task_result_source"]) == (
                "success", "self_reported")

    @pytest.mark.asyncio
    async def test_unreadable_final_authority_suppresses_webhooks(
        self, monkeypatch, rr, caplog
    ):
        from app.evals import compute as compute_mod
        fired = AsyncMock()
        monkeypatch.setattr(compute_mod, "get_eval", AsyncMock(return_value=None))
        await _compute(monkeypatch, _make_events([{"type": "memory_read"}]),
                       id_events=[], replay_redis=rr, webhook_sink=fired)
        fired.assert_not_awaited()
        assert "authoritative eval unreadable" in caplog.text
