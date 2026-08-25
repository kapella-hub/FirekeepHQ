"""Tests for the Pattern Engine.

Run with: PYTHONPATH=cortex python -m pytest cortex/tests/test_patterns.py -v --noconftest
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.patterns.models import PatternCard, SessionFeatures, Dataset, Experiment


@pytest_asyncio.fixture
async def rr():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()

# Gate experiment/dataset/statistics tests behind the feature flag
_EXPERIMENTS_ENABLED = os.environ.get("PATTERN_EXPERIMENTS_ENABLED", "false").lower() == "true"
_skip_experiments = pytest.mark.skipif(
    not _EXPERIMENTS_ENABLED,
    reason="PATTERN_EXPERIMENTS_ENABLED=false — experiment framework gated",
)
from app.patterns.analyzer import _confidence, _success_rate, analyze_patterns
from app.patterns.analyzer import (
    _detect_memory_first,
    _detect_file_hotspot,
    _detect_tool_sequence,
    _detect_memory_usage,
    _detect_duration,
    _detect_failure_mode,
)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestSessionFeatures:
    def test_creation_defaults(self):
        f = SessionFeatures(session_id="s1")
        assert f.session_id == "s1"
        assert f.outcome == "unknown"
        assert f.event_count == 0
        assert f.memory_reads == 0
        assert f.memory_writes == 0
        assert f.tool_sequence == []
        assert f.file_paths == []
        assert f.tags == []

    def test_creation_full(self):
        f = SessionFeatures(
            session_id="s2",
            duration_ms=5000,
            outcome="failure",
            event_count=10,
            tool_sequence=["memory_read", "file_edit"],
            tool_type_counts={"memory_read": 1, "file_edit": 1},
            memory_reads=3,
            memory_writes=1,
            file_paths=["/a.py", "/b.py"],
            file_count=2,
            claim_count=1,
            tool_success_rate=0.8,
            failure_rate=0.2,
            tags=["test"],
        )
        assert f.outcome == "failure"
        assert f.memory_reads == 3
        assert f.file_count == 2
        assert f.tool_success_rate == 0.8

    def test_json_round_trip(self):
        f = SessionFeatures(session_id="s3", outcome="success", memory_reads=5)
        data = f.model_dump_json()
        f2 = SessionFeatures.model_validate_json(data)
        assert f2.session_id == "s3"
        assert f2.memory_reads == 5


class TestPatternCard:
    def test_creation_defaults(self):
        p = PatternCard(id="pat_test_123")
        assert p.id == "pat_test_123"
        assert p.confidence == 0.5
        assert p.trending is False
        assert p.pattern_type == "memory_first"

    def test_creation_full(self):
        p = PatternCard(
            id="pat_mf_abc",
            description="Memory first helps",
            pattern_type="memory_first",
            confidence=0.85,
            evidence_count=20,
            baseline_rate=0.6,
            pattern_rate=0.85,
            lift=1.42,
            recommendation="Read memory first",
            tags=["memory"],
            trending=True,
        )
        assert p.confidence == 0.85
        assert p.lift == 1.42
        assert p.trending is True

    def test_json_round_trip(self):
        p = PatternCard(id="pat_x", confidence=0.7, description="test")
        data = p.model_dump_json()
        p2 = PatternCard.model_validate_json(data)
        assert p2.id == "pat_x"
        assert p2.confidence == 0.7


# ---------------------------------------------------------------------------
# Confidence formula tests
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_formula_basic(self):
        # effect_size=0.3, evidence=20 -> 0.3 * 2 * 1.0 = 0.6
        assert _confidence(0.3, 20) == pytest.approx(0.6, abs=0.01)

    def test_formula_low_evidence(self):
        # effect_size=0.5, evidence=5 -> 0.5 * 2 * 0.25 = 0.25
        assert _confidence(0.5, 5) == pytest.approx(0.25, abs=0.01)

    def test_formula_high_effect(self):
        # effect_size=0.8, evidence=40 -> 0.8 * 2 * 1.0 = 1.6 -> clamped to 0.99
        assert _confidence(0.8, 40) == 0.99

    def test_formula_zero_effect(self):
        # effect_size=0, evidence=100 -> 0 -> clamped to 0.05
        assert _confidence(0.0, 100) == 0.05

    def test_formula_negative_effect(self):
        # Uses abs, so -0.3 with 20 evidence -> 0.6
        assert _confidence(-0.3, 20) == pytest.approx(0.6, abs=0.01)

    def test_formula_sample_factor_scales(self):
        # Same effect size, different evidence
        c10 = _confidence(0.4, 10)  # 0.4 * 2 * 0.5 = 0.4
        c20 = _confidence(0.4, 20)  # 0.4 * 2 * 1.0 = 0.8
        assert c20 > c10


# ---------------------------------------------------------------------------
# Detector tests with mock features
# ---------------------------------------------------------------------------


def _make_features(
    n_success: int = 5,
    n_failure: int = 5,
    memory_first_success: bool = True,
    files: list[str] | None = None,
    outcome_source: str = "task_result",
) -> list[SessionFeatures]:
    """Create a list of mock SessionFeatures for testing.

    Defaults to outcome_source="task_result" -- these fixtures stand in for
    graded experimental evidence in the detector/analyzer tests below.
    """
    features = []

    for i in range(n_success):
        seq = ["memory_read", "file_edit", "memory_write"] if memory_first_success else ["file_edit", "memory_read"]
        features.append(SessionFeatures(
            session_id=f"success_{i}",
            outcome="success",
            outcome_source=outcome_source,
            event_count=10,
            duration_ms=5000 + i * 1000,
            tool_sequence=seq,
            tool_type_counts={"memory_read": 1, "file_edit": 1, "memory_write": 1} if memory_first_success else {"file_edit": 1, "memory_read": 1},
            memory_reads=2 if memory_first_success else 1,
            memory_writes=1,
            file_paths=files or ["/src/main.py"],
            file_count=len(files) if files else 1,
            claim_count=1,
            tool_success_rate=0.9,
            failure_rate=0.1,
        ))

    for i in range(n_failure):
        seq = ["file_edit", "error_handler"] if memory_first_success else ["memory_read", "file_edit", "error_handler"]
        features.append(SessionFeatures(
            session_id=f"failure_{i}",
            outcome="failure",
            outcome_source=outcome_source,
            event_count=8,
            duration_ms=15000 + i * 1000,
            tool_sequence=seq,
            tool_type_counts={"file_edit": 1, "error_handler": 1},
            memory_reads=0,
            memory_writes=0,
            file_paths=["/src/buggy.py"],
            file_count=1,
            claim_count=1,
            tool_success_rate=0.3,
            failure_rate=0.7,
        ))

    return features


# ---------------------------------------------------------------------------
# Outcome truth: provenance + graded_only (2026-08-23)
# ---------------------------------------------------------------------------


class TestGradedProvenance:
    def test_defaults_mean_legacy_and_unknown(self):
        from app.patterns.models import SessionFeatures
        f = SessionFeatures(session_id="s")
        assert f.outcome == "unknown" and f.outcome_source == "legacy"

    def test_old_cached_json_parses_as_legacy(self):
        from app.patterns.models import SessionFeatures, graded_only
        old = SessionFeatures.model_validate_json(
            '{"session_id": "s", "outcome": "success"}')
        assert old.outcome_source == "legacy"
        assert graded_only([old]) == []

    def test_graded_only_keeps_real_grades(self):
        from app.patterns.models import SessionFeatures, graded_only
        real = SessionFeatures(session_id="a", outcome="failure",
                               outcome_source="task_result")
        fab = SessionFeatures(session_id="b", outcome="success")  # legacy default
        unk = SessionFeatures(session_id="c", outcome="unknown",
                              outcome_source="task_result")  # graded "partial"
        assert graded_only([real, fab, unk]) == [real]


@pytest.mark.asyncio
async def test_extractor_stamps_grade_and_provenance(monkeypatch):
    """A REAL extractor call; reader import is function-local — patch
    replay.reader, not the extractor module. Task 4b: the extractor reads
    through get_session_event_ids/get_event_batch now (no get_session_timeline
    fetch to fake)."""
    import replay.reader as reader_mod
    from app.evals.models import EvalResult
    from app.patterns.extractor import extract_session_features

    async def fake_summary(*a, **k):
        return {"event_count": 2, "duration_ms": 10}

    _events = {
        "e0": {"event_type": "memory_read", "outcome": None, "payload": {}, "tags": []},
        "e1": {"event_type": "session_end", "outcome": "success", "payload": {}, "tags": []},
    }

    async def fake_ids(r, sid, *, limit=5000):
        return list(_events.keys())

    async def fake_batch(r, ids):
        return [_events[i] for i in ids if i in _events]

    monkeypatch.setattr(reader_mod, "get_session_summary", fake_summary)
    monkeypatch.setattr(reader_mod, "get_session_event_ids", fake_ids)
    monkeypatch.setattr(reader_mod, "get_event_batch", fake_batch)

    graded = await extract_session_features(
        None, "s1",
        eval_result=EvalResult(session_id="s1", trigger="session_complete",
                               task_result="success",
                               task_result_source="self_reported"))
    assert graded.outcome == "success" and graded.outcome_source == "task_result"

    ungraded = await extract_session_features(None, "s1", eval_result=None)
    assert ungraded.outcome == "unknown" and ungraded.outcome_source == "legacy"


@pytest.mark.asyncio
async def test_features_include_late_events_beyond_1000(monkeypatch):
    """D3, third site (task 4b): extract_session_features must read the whole
    session via get_session_event_ids/get_event_batch, not the oldest-1000
    window get_session_timeline used to silently truncate to. Mirrors
    test_evals.py's TestMetricsScanCap for the same fix in compute.py."""
    import replay.reader as reader_mod
    from app.patterns.extractor import extract_session_features

    filler = [
        (f"e{i}", {"event_type": "memory_read", "payload": {}, "outcome": "success"})
        for i in range(1100)
    ]
    late_fail = ("late-fail", {"event_type": "tool_call", "payload": {}, "outcome": "failure"})
    pairs = filler + [late_fail]
    id_to_ev = dict(pairs)

    async def fake_summary(*a, **k):
        return {"event_count": len(pairs), "duration_ms": 1000}

    async def fake_ids(r, sid, *, limit=5000):
        return [i for i, _ in pairs][-limit:]

    async def fake_batch(r, ids):
        return [id_to_ev[i] for i in ids if i in id_to_ev]

    monkeypatch.setattr(reader_mod, "get_session_summary", fake_summary)
    monkeypatch.setattr(reader_mod, "get_session_event_ids", fake_ids)
    monkeypatch.setattr(reader_mod, "get_event_batch", fake_batch)

    feats = await extract_session_features(None, "sess-long-features")
    assert feats is not None
    # Old oldest-1000 window would have dropped this — it sits at index 1100
    # of 1101 events.
    assert feats.tool_type_counts.get("tool_call") == 1
    assert feats.failure_rate > 0.0


def test_success_rate_is_none_when_nothing_is_graded():
    from app.patterns.analyzer import _success_rate
    from app.patterns.models import SessionFeatures
    assert _success_rate([SessionFeatures(session_id=str(i)) for i in range(6)]) is None


@pytest.mark.asyncio
async def test_effectiveness_does_not_extend_card_ttl(rr):
    """D11: the dashboard calls GET /patterns/effectiveness on load; before
    this change the persist used ex=_DEFAULT_TTL, giving fabricated-era cards
    a fresh 30 days per visit. KEEPTTL preserves the remaining TTL."""
    from app.patterns.models import PatternCard, SessionFeatures
    from app.patterns.store import (
        _PATTERN_PREFIX, compute_tip_effectiveness, record_tip_shown,
        store_features, store_patterns,
    )
    await store_patterns(rr, [PatternCard(id="p1")])
    for i in range(6):
        await store_features(rr, SessionFeatures(
            session_id=f"s{i}", outcome="success",
            outcome_source="task_result"))
    await record_tip_shown(rr, "s0", ["p1"], group="treatment")
    key = f"{_PATTERN_PREFIX}p1"
    await rr.expire(key, 1000)
    before = await rr.pttl(key)
    await compute_tip_effectiveness(rr)
    after = await rr.pttl(key)
    assert 0 < after <= before


@pytest.mark.asyncio
async def test_outcome_filtered_datasets_exclude_legacy(rr):
    """'Success only' membership must not include fabricated legacy successes."""
    from app.patterns.models import SessionFeatures, Dataset
    from app.patterns.store import store_features, materialize_dataset
    await store_features(rr, SessionFeatures(session_id="leg", outcome="success"))  # legacy
    await store_features(rr, SessionFeatures(session_id="grd", outcome="success",
                                             outcome_source="task_result"))
    ds = Dataset(id="d1", name="n", outcome_filter="success")
    out = await materialize_dataset(rr, ds)
    assert out.session_ids == ["grd"]
    assert out.metrics_summary["success_count"] == 1
    assert out.metrics_summary.get("unknown_count", 0) == 0     # legacy filtered, not counted


@pytest.mark.asyncio
async def test_unfiltered_dataset_reports_unknown_instead_of_fabricated_success(rr):
    from app.patterns.models import Dataset, SessionFeatures
    from app.patterns.store import materialize_dataset, store_features
    await store_features(rr, SessionFeatures(session_id="u1", outcome="success"))
    await store_features(rr, SessionFeatures(session_id="u2", outcome="unknown"))
    out = await materialize_dataset(rr, Dataset(id="d2", name="all"))
    assert set(out.session_ids) == {"u1", "u2"}
    assert out.metrics_summary == {
        "success_count": 0,
        "failure_count": 0,
        "unknown_count": 2,
        "success_rate": None,
        "avg_duration_ms": 0,
    }


@pytest.mark.asyncio
async def test_ungraded_features_never_overwrite_graded(rr):
    """D9e: store_features is dominance-guarded — a stalled ungraded writer
    (or any later legacy re-extract) must not regress a graded record."""
    from app.patterns.models import SessionFeatures
    from app.patterns.store import store_features
    from app.patterns.store import get_all_features
    assert await store_features(rr, SessionFeatures(
        session_id="s", outcome="failure", outcome_source="task_result")) is True
    assert await store_features(rr, SessionFeatures(session_id="s")) is False  # legacy refused
    # re-read: still the graded record
    feats = {f.session_id: f for f in await get_all_features(rr)}
    assert feats["s"].outcome == "failure" and feats["s"].outcome_source == "task_result"


@pytest.mark.asyncio
async def test_graded_feature_upgrades_legacy(rr):
    """D9e, upgrade direction: a graded write for a session that already has a
    legacy record must win — grade-dominance blocks legacy-over-graded (above),
    but graded-over-legacy is exactly the upgrade it exists to allow."""
    from app.patterns.models import SessionFeatures
    from app.patterns.store import store_features
    from app.patterns.store import get_all_features
    assert await store_features(rr, SessionFeatures(session_id="s")) is True  # legacy
    assert await store_features(rr, SessionFeatures(
        session_id="s", outcome="failure", outcome_source="task_result")) is True
    feats = {f.session_id: f for f in await get_all_features(rr)}
    assert feats["s"].outcome == "failure" and feats["s"].outcome_source == "task_result"


@pytest.mark.asyncio
async def test_stale_legacy_feature_writer_retries_then_loses(
    rr, monkeypatch
):
    """Force graded state to appear after the legacy writer's WATCH/read and
    before EXEC. Its retry must observe provenance and refuse the regression."""
    import redis
    from app.patterns.models import SessionFeatures
    from app.patterns.store import (
        _FEATURE_INDEX, _FEATURE_PREFIX, get_all_features, store_features)

    graded = SessionFeatures(
        session_id="race", outcome="success", outcome_source="task_result")
    legacy = SessionFeatures(session_id="race")
    key = f"{_FEATURE_PREFIX}race"
    real_pipeline = rr.pipeline
    state = {"raced": False}

    def racing_pipeline(*args, **kwargs):
        pipe = real_pipeline(*args, **kwargs)
        real_execute = pipe.execute

        async def execute(*ea, **ek):
            if not state["raced"]:
                state["raced"] = True
                await rr.set(key, graded.model_dump_json(), ex=86400)
                await rr.zadd(
                    _FEATURE_INDEX, {"race": graded.created_at.timestamp()})
                raise redis.WatchError("graded writer won")
            return await real_execute(*ea, **ek)

        pipe.execute = execute
        return pipe

    monkeypatch.setattr(rr, "pipeline", racing_pipeline)
    assert await store_features(rr, legacy) is False
    stored = {f.session_id: f for f in await get_all_features(rr)}["race"]
    assert (stored.outcome, stored.outcome_source) == (
        "success", "task_result")


class TestDetectors:
    def test_detect_memory_first(self):
        features = _make_features(n_success=8, n_failure=8)
        baseline = _success_rate(features)
        pattern = _detect_memory_first(features, baseline)
        assert pattern is not None
        assert pattern.pattern_type == "memory_first"
        assert pattern.confidence > 0.05
        assert "memory" in pattern.description.lower()

    def test_detect_file_hotspot(self):
        features = _make_features(n_success=5, n_failure=5, files=["/src/main.py"])
        baseline = _success_rate(features)
        patterns = _detect_file_hotspot(features, baseline)
        # Should find at least /src/main.py (success) or /src/buggy.py (failure)
        assert len(patterns) >= 1
        assert all(p.pattern_type == "file_hotspot" for p in patterns)

    def test_detect_tool_sequence(self):
        features = _make_features(n_success=5, n_failure=5)
        baseline = _success_rate(features)
        patterns = _detect_tool_sequence(features, baseline)
        # memory_read->file_edit appears in successes, file_edit->error_handler in failures
        assert len(patterns) >= 1
        assert all(p.pattern_type == "tool_sequence" for p in patterns)

    def test_detect_memory_usage(self):
        features = _make_features(n_success=6, n_failure=6)
        # Give successes varying memory usage so median split works
        for i, f in enumerate(features):
            if f.outcome == "success":
                f.memory_reads = 3 + i  # Above median
                f.memory_writes = 2
        baseline = _success_rate(features)
        pattern = _detect_memory_usage(features, baseline)
        # Successes have more memory ops than failures
        assert pattern is not None
        assert pattern.pattern_type == "memory_usage"

    def test_detect_duration(self):
        features = _make_features(n_success=6, n_failure=6)
        baseline = _success_rate(features)
        patterns = _detect_duration(features, baseline)
        # Short sessions (successes have lower duration) should differ from long ones
        assert isinstance(patterns, list)

    def test_detect_failure_mode(self):
        features = _make_features(n_success=5, n_failure=5)
        baseline = _success_rate(features)
        patterns = _detect_failure_mode(features, baseline)
        # error_handler appears only in failures
        assert len(patterns) >= 1
        found_error = any("error_handler" in p.description for p in patterns)
        assert found_error

    def test_not_enough_data(self):
        features = _make_features(n_success=1, n_failure=0)
        baseline = _success_rate(features)
        assert _detect_memory_first(features, baseline) is None
        assert _detect_memory_usage(features, baseline) is None


# ---------------------------------------------------------------------------
# Analyzer integration test (mocked Redis)
# ---------------------------------------------------------------------------


class TestAnalyzer:
    def test_analyze_min_sessions_gate(self):
        """Analyzer returns empty when not enough sessions."""
        mock_redis = AsyncMock()

        async def run():
            with patch("app.patterns.analyzer.get_all_features", return_value=[]):
                result = await analyze_patterns(mock_redis, min_sessions=5)
                assert result == []

        asyncio.run(run())

    def test_analyze_finds_patterns(self):
        """Analyzer discovers patterns from sufficient data."""
        features = _make_features(n_success=8, n_failure=8)
        mock_redis = AsyncMock()

        async def run():
            with patch("app.patterns.analyzer.get_all_features", return_value=features):
                patterns = await analyze_patterns(mock_redis, min_sessions=5)
                assert len(patterns) > 0
                # Should be sorted by confidence desc
                for i in range(len(patterns) - 1):
                    assert patterns[i].confidence >= patterns[i + 1].confidence

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Relevant patterns keyword matching test
# ---------------------------------------------------------------------------


class TestAnalyzerRealisticData:
    """Verify pattern analysis with realistic session feature distributions."""

    def test_realistic_mixed_sessions_produce_patterns(self):
        """Analyzer discovers patterns from a realistic mix of session features."""
        features = []
        # Simulate 12 realistic sessions with varied characteristics
        for i in range(6):
            # Successful sessions: memory-first, moderate duration, some file edits
            features.append(SessionFeatures(
                session_id=f"real_success_{i}",
                outcome="success",
                outcome_source="task_result",
                event_count=15 + i * 3,
                duration_ms=30000 + i * 5000,
                tool_sequence=["memory_read", "memory_read", "file_edit", "memory_write", "file_edit"],
                tool_type_counts={"memory_read": 2, "file_edit": 2, "memory_write": 1},
                memory_reads=2 + i,
                memory_writes=1,
                file_paths=["/src/service.py", "/tests/test_service.py"],
                file_count=2,
                claim_count=1,
                tool_success_rate=0.85 + i * 0.02,
                failure_rate=0.05,
                tags=["implementation"],
            ))
        for i in range(6):
            # Failed sessions: no memory reads, jump straight to edits, longer duration
            features.append(SessionFeatures(
                session_id=f"real_failure_{i}",
                outcome="failure",
                outcome_source="task_result",
                event_count=20 + i * 2,
                duration_ms=120000 + i * 10000,
                tool_sequence=["file_edit", "file_edit", "error_handler", "file_edit", "error_handler"],
                tool_type_counts={"file_edit": 3, "error_handler": 2},
                memory_reads=0,
                memory_writes=0,
                file_paths=["/src/config.py"],
                file_count=1,
                claim_count=2,
                tool_success_rate=0.3,
                failure_rate=0.7,
                tags=["bugfix"],
            ))

        mock_redis = AsyncMock()

        async def run():
            with patch("app.patterns.analyzer.get_all_features", return_value=features):
                patterns = await analyze_patterns(mock_redis, min_sessions=5)
                assert len(patterns) >= 2, f"Expected multiple patterns, got {len(patterns)}"
                # Sorted by confidence descending
                for i in range(len(patterns) - 1):
                    assert patterns[i].confidence >= patterns[i + 1].confidence
                # Should detect memory_first (successes use memory first)
                types_found = {p.pattern_type for p in patterns}
                assert "memory_first" in types_found or "failure_mode" in types_found, (
                    f"Expected memory_first or failure_mode pattern, got {types_found}"
                )
                # All patterns should have valid fields
                for p in patterns:
                    assert p.id.startswith("pat_")
                    assert 0.05 <= p.confidence <= 0.99
                    assert p.evidence_count > 0
                    assert p.recommendation != ""

        asyncio.run(run())

    def test_min_sessions_threshold_is_5(self):
        """The default min_sessions threshold is 5."""
        features = _make_features(n_success=2, n_failure=2)  # Only 4 sessions
        mock_redis = AsyncMock()

        async def run():
            with patch("app.patterns.analyzer.get_all_features", return_value=features):
                # Default min_sessions=5, we only have 4
                patterns = await analyze_patterns(mock_redis)
                assert patterns == []
                # With lowered threshold it should work
                patterns = await analyze_patterns(mock_redis, min_sessions=4)
                # May or may not find patterns with 4 sessions, but should not crash
                assert isinstance(patterns, list)

        asyncio.run(run())


class TestRelevantPatterns:
    def test_filters_by_stage_and_category(self):
        """Only trial+ procedural/risk patterns should appear in relevant results."""
        from app.patterns.store import get_relevant_patterns

        p_candidate = PatternCard(id="p1", confidence=0.9, stage="candidate", category="procedural")
        p_behavioral = PatternCard(id="p2", confidence=0.9, stage="trial", category="behavioral")
        p_stale = PatternCard(id="p3", confidence=0.9, stage="stale", category="procedural")
        p_trial = PatternCard(id="p4", confidence=0.7, stage="trial", category="risk")
        p_validated = PatternCard(id="p5", confidence=0.8, stage="validated", category="procedural")

        mock_redis = AsyncMock()

        async def run():
            with patch("app.patterns.store.get_patterns", return_value=[p_candidate, p_behavioral, p_stale, p_trial, p_validated]):
                result = await get_relevant_patterns(mock_redis, limit=10)
                ids = {p.id for p in result}
                assert "p4" in ids  # trial + risk
                assert "p5" in ids  # validated + procedural
                assert "p1" not in ids  # candidate excluded
                assert "p2" not in ids  # behavioral excluded
                assert "p3" not in ids  # stale excluded

        asyncio.run(run())

    def test_keyword_boost(self):
        """Patterns matching goal keywords should rank higher."""
        from app.patterns.store import get_relevant_patterns

        p1 = PatternCard(
            id="p1", confidence=0.5, description="Memory first helps with database",
            tags=["memory"], category="procedural", stage="trial",
        )
        p2 = PatternCard(
            id="p2", confidence=0.8, description="File edits succeed in tests",
            tags=["file"], category="procedural", stage="trial",
        )
        p3 = PatternCard(
            id="p3", confidence=0.4, description="Database operations need memory",
            tags=["database", "memory"], category="procedural", stage="trial",
        )

        mock_redis = AsyncMock()

        async def run():
            with patch("app.patterns.store.get_patterns", return_value=[p2, p1, p3]):
                # Goal mentions "database" — p1 and p3 should get boosted
                result = await get_relevant_patterns(mock_redis, goal="fix database issue", limit=3)
                assert len(result) == 3
                # p3 has "database" in both tags and description, should rank well despite low base confidence
                ids = [p.id for p in result]
                # p2 has highest base confidence (0.8) but no keyword match
                # p3 has "database" in tags (+0.1) and description (+0.1 for "database")
                # p1 has "database" in description (+0.1)
                assert ids[0] == "p2"  # 0.8, no boost still highest

        asyncio.run(run())

    def test_exclude_agent_filter(self):
        """Patterns from excluded agent should not appear."""
        from app.patterns.store import get_relevant_patterns

        p1 = PatternCard(id="p1", confidence=0.8, source_agent="agent-a", category="procedural", stage="trial")
        p2 = PatternCard(id="p2", confidence=0.7, source_agent="agent-b", category="procedural", stage="trial")

        mock_redis = AsyncMock()

        async def run():
            with patch("app.patterns.store.get_patterns", return_value=[p1, p2]):
                result = await get_relevant_patterns(
                    mock_redis, exclude_agent="agent-a", limit=5,
                )
                assert len(result) == 1
                assert result[0].id == "p2"

        asyncio.run(run())

    def test_file_boost(self):
        """Patterns matching file paths should rank higher."""
        from app.patterns.store import get_relevant_patterns

        p1 = PatternCard(id="p1", confidence=0.5, tags=["/src/main.py", "file"], category="risk", stage="trial")
        p2 = PatternCard(id="p2", confidence=0.6, tags=["memory"], category="procedural", stage="validated")

        mock_redis = AsyncMock()

        async def run():
            with patch("app.patterns.store.get_patterns", return_value=[p2, p1]):
                result = await get_relevant_patterns(
                    mock_redis, files=["/src/main.py"], limit=2,
                )
                assert len(result) == 2
                # p1 gets +0.2 file boost: 0.5 + 0.2 = 0.7 > p2's 0.6
                assert result[0].id == "p1"

        asyncio.run(run())


# ---------------------------------------------------------------------------
# PatternCard new fields (category, stage, scope, quarantine)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Promotion lifecycle tests
# ---------------------------------------------------------------------------


class TestPromotionLifecycle:
    # These verify the promotion-ladder MATH, which N=1 Task 1 KEEPS (only gates
    # whether it runs behind PATTERN_VALIDATION_ENABLED). Enable the flag so the
    # math is exercised; the freeze itself is covered in test_patterns_lifecycle.py.
    @pytest.fixture(autouse=True)
    def _enable_validation(self, monkeypatch):
        from types import SimpleNamespace
        from app.patterns import lifecycle
        monkeypatch.setattr(
            lifecycle, "get_settings",
            lambda: SimpleNamespace(PATTERN_VALIDATION_ENABLED=True),
        )

    def test_candidate_to_observed(self):
        from app.patterns.lifecycle import evaluate_promotion
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.4, evidence_count=12, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="candidate", category="procedural",
        )
        result = evaluate_promotion(p)
        assert result.stage == "observed"
        assert result.promoted_at is not None

    def test_candidate_stays_if_low_evidence(self):
        from app.patterns.lifecycle import evaluate_promotion
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.4, evidence_count=5, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="candidate", category="procedural",
        )
        result = evaluate_promotion(p)
        assert result.stage == "candidate"

    def test_observed_to_trial(self):
        from app.patterns.lifecycle import evaluate_promotion
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.6, evidence_count=18, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="observed", category="procedural",
        )
        result = evaluate_promotion(p)
        assert result.stage == "trial"

    def test_trial_to_validated_with_tip_lift(self):
        from app.patterns.lifecycle import evaluate_promotion
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.7, evidence_count=30, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="trial", category="procedural",
            tip_lift=0.1,
        )
        result = evaluate_promotion(p)
        assert result.stage == "validated"

    def test_trial_blocked_without_tip_lift(self):
        """tip_lift=None intentionally blocks trial->validated."""
        from app.patterns.lifecycle import evaluate_promotion
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.7, evidence_count=30, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="trial", category="procedural",
            tip_lift=None,
        )
        result = evaluate_promotion(p)
        assert result.stage == "trial"  # Not promoted

    def test_stale_after_30_days(self):
        from app.patterns.lifecycle import evaluate_promotion
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.7, evidence_count=20, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="observed", category="procedural",
            last_matched_at=now - timedelta(days=35),
        )
        result = evaluate_promotion(p, now=now)
        assert result.stage == "stale"

    def test_retire_low_confidence(self):
        from app.patterns.lifecycle import evaluate_promotion
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.15, evidence_count=20, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="observed", category="procedural",
        )
        result = evaluate_promotion(p)
        assert result.stage == "retired"

    def test_quarantine_blocks_promotion(self):
        from app.patterns.lifecycle import evaluate_promotion
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.9, evidence_count=50, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="quarantined", category="procedural",
            quarantine_reason="Bad pattern",
        )
        result = evaluate_promotion(p)
        assert result.stage == "quarantined"

    def test_retired_stays_retired(self):
        from app.patterns.lifecycle import evaluate_promotion
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.9, evidence_count=50, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="retired", category="procedural",
        )
        result = evaluate_promotion(p)
        assert result.stage == "retired"

    def test_behavioral_never_reaches_trial(self):
        from app.patterns.lifecycle import evaluate_promotion
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_usage",
            confidence=0.8, evidence_count=50, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="observed", category="behavioral",
        )
        result = evaluate_promotion(p)
        assert result.stage == "observed"  # Capped

    def test_behavioral_candidate_promotes_to_observed(self):
        from app.patterns.lifecycle import evaluate_promotion
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_usage",
            confidence=0.4, evidence_count=12, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="candidate", category="behavioral",
        )
        result = evaluate_promotion(p)
        assert result.stage == "observed"

    def test_confidence_decay(self):
        from app.patterns.lifecycle import evaluate_promotion
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.5, evidence_count=20, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="observed", category="procedural",
            last_matched_at=now - timedelta(days=21),  # 3 weeks
        )
        result = evaluate_promotion(p, now=now)
        # 3 weeks * 0.02 = 0.06 decay: 0.5 - 0.06 = 0.44
        assert result.confidence < 0.5
        assert result.confidence > 0.4


class TestApplyLifecycle:
    # apply_lifecycle -> evaluate_promotion runs the promotion MATH per pattern,
    # which Task 1 gates behind PATTERN_VALIDATION_ENABLED. Enable it so these
    # math assertions hold; the frozen path is covered in test_patterns_lifecycle.py.
    @pytest.fixture(autouse=True)
    def _enable_validation(self, monkeypatch):
        from types import SimpleNamespace
        from app.patterns import lifecycle
        monkeypatch.setattr(
            lifecycle, "get_settings",
            lambda: SimpleNamespace(PATTERN_VALIDATION_ENABLED=True),
        )

    def test_basic_lifecycle_run(self):
        from app.patterns.lifecycle import apply_lifecycle
        patterns = [
            PatternCard(
                id=f"pat-{i}", description=f"test {i}", pattern_type="memory_first",
                confidence=0.4, evidence_count=12, baseline_rate=0.6,
                pattern_rate=0.8, lift=1.33, recommendation="test",
                stage="candidate", category="procedural",
            )
            for i in range(5)
        ]
        result = apply_lifecycle(patterns)
        # All should be promoted to observed
        for p in result:
            assert p.stage == "observed"

    def test_hard_limit_60_to_50(self):
        """60 non-candidate patterns should be trimmed to 50."""
        from app.patterns.lifecycle import apply_lifecycle
        patterns = [
            PatternCard(
                id=f"pat-{i}", description=f"test {i}", pattern_type="memory_first",
                confidence=0.3 + (i * 0.01), evidence_count=20,
                baseline_rate=0.6, pattern_rate=0.8, lift=1.33,
                recommendation="test",
                stage="observed", category="procedural",
            )
            for i in range(60)
        ]
        result = apply_lifecycle(patterns)
        non_retired = [p for p in result if p.stage != "retired"]
        assert len(non_retired) <= 50

    def test_retired_patterns_removed(self):
        from app.patterns.lifecycle import apply_lifecycle
        patterns = [
            PatternCard(
                id="pat-low", description="low conf", pattern_type="memory_first",
                confidence=0.1, evidence_count=20, baseline_rate=0.6,
                pattern_rate=0.8, lift=1.33, recommendation="test",
                stage="observed", category="procedural",
            ),
            PatternCard(
                id="pat-high", description="high conf", pattern_type="memory_first",
                confidence=0.7, evidence_count=20, baseline_rate=0.6,
                pattern_rate=0.8, lift=1.33, recommendation="test",
                stage="observed", category="procedural",
            ),
        ]
        result = apply_lifecycle(patterns)
        ids = [p.id for p in result]
        assert "pat-high" in ids
        # pat-low should be retired (confidence 0.1 < 0.2) and removed
        assert "pat-low" not in ids


class TestQuarantine:
    def test_quarantine_pattern(self):
        from app.patterns.lifecycle import quarantine_pattern
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.7, evidence_count=20, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="trial", category="procedural",
        )
        result = quarantine_pattern(p, "Suspicious results")
        assert result.stage == "quarantined"
        assert result.quarantine_reason == "Suspicious results"
        assert result.quarantined_at is not None

    def test_unquarantine_pattern(self):
        from app.patterns.lifecycle import unquarantine_pattern
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.7, evidence_count=20, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="quarantined", category="procedural",
            quarantine_reason="test reason",
            quarantined_at=datetime.now(timezone.utc),
        )
        result = unquarantine_pattern(p)
        assert result.stage == "candidate"
        assert result.quarantine_reason == ""
        assert result.quarantined_at is None


class TestPatternCategoryAssignment:
    def test_memory_first_is_procedural(self):
        features = _make_features(n_success=8, n_failure=8)
        baseline = _success_rate(features)
        pattern = _detect_memory_first(features, baseline)
        assert pattern is not None
        assert pattern.category == "procedural"

    def test_file_hotspot_is_risk(self):
        features = _make_features(n_success=5, n_failure=5, files=["/src/main.py"])
        baseline = _success_rate(features)
        patterns = _detect_file_hotspot(features, baseline)
        assert len(patterns) >= 1
        for p in patterns:
            assert p.category == "risk"

    def test_tool_sequence_is_procedural(self):
        features = _make_features(n_success=5, n_failure=5)
        baseline = _success_rate(features)
        patterns = _detect_tool_sequence(features, baseline)
        for p in patterns:
            assert p.category == "procedural"

    def test_memory_usage_is_behavioral(self):
        features = _make_features(n_success=6, n_failure=6)
        for i, f in enumerate(features):
            if f.outcome == "success":
                f.memory_reads = 3 + i
                f.memory_writes = 2
        baseline = _success_rate(features)
        pattern = _detect_memory_usage(features, baseline)
        assert pattern is not None
        assert pattern.category == "behavioral"

    def test_duration_is_behavioral(self):
        features = _make_features(n_success=6, n_failure=6)
        baseline = _success_rate(features)
        patterns = _detect_duration(features, baseline)
        for p in patterns:
            assert p.category == "behavioral"

    def test_failure_mode_is_risk(self):
        features = _make_features(n_success=5, n_failure=5)
        baseline = _success_rate(features)
        patterns = _detect_failure_mode(features, baseline)
        for p in patterns:
            assert p.category == "risk"

    def test_file_hotspot_has_scope_module(self):
        features = _make_features(n_success=5, n_failure=5, files=["/src/main.py"])
        baseline = _success_rate(features)
        patterns = _detect_file_hotspot(features, baseline)
        # The file paths include /src/main.py and /src/buggy.py
        for p in patterns:
            assert p.scope_module != ""


class TestPatternCardFields:
    def test_default_stage_is_candidate(self):
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.5, evidence_count=5, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
        )
        assert p.stage == "candidate"
        assert p.category == "procedural"

    def test_category_field(self):
        p = PatternCard(
            id="pat-test", description="test", pattern_type="file_hotspot",
            confidence=0.5, evidence_count=5, baseline_rate=0.6,
            pattern_rate=0.4, lift=0.67, recommendation="test",
            category="risk",
        )
        assert p.category == "risk"

    def test_scope_tags(self):
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.5, evidence_count=5, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            scope_goal_type="debugging", scope_module="auth",
            scope_service="cortex",
        )
        assert p.scope_goal_type == "debugging"
        assert p.scope_module == "auth"
        assert p.scope_service == "cortex"

    def test_quarantined_stage(self):
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.5, evidence_count=5, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            stage="quarantined", quarantine_reason="Manual review",
        )
        assert p.stage == "quarantined"
        assert p.quarantine_reason == "Manual review"

    def test_promotion_tracking_fields(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        p = PatternCard(
            id="pat-test", description="test", pattern_type="memory_first",
            confidence=0.5, evidence_count=5, baseline_rate=0.6,
            pattern_rate=0.8, lift=1.33, recommendation="test",
            last_matched_at=now, promoted_at=now,
        )
        assert p.last_matched_at == now
        assert p.promoted_at == now

    def test_new_fields_json_round_trip(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        p = PatternCard(
            id="pat-test", description="test", pattern_type="file_hotspot",
            confidence=0.7, evidence_count=15, baseline_rate=0.5,
            pattern_rate=0.3, lift=0.6, recommendation="careful",
            category="risk", stage="observed",
            scope_goal_type="debugging", scope_module="auth",
            quarantine_reason="", last_matched_at=now, promoted_at=now,
        )
        data = p.model_dump_json()
        p2 = PatternCard.model_validate_json(data)
        assert p2.category == "risk"
        assert p2.stage == "observed"
        assert p2.scope_goal_type == "debugging"
        assert p2.last_matched_at is not None


# ---------------------------------------------------------------------------
# Dataset model tests (gated behind PATTERN_EXPERIMENTS_ENABLED)
# ---------------------------------------------------------------------------


@_skip_experiments
class TestDataset:
    def test_creation_defaults(self):
        d = Dataset(id="dset_abc12345", name="Test dataset")
        assert d.id == "dset_abc12345"
        assert d.name == "Test dataset"
        assert d.description == ""
        assert d.date_min is None
        assert d.date_max is None
        assert d.agent_ids == []
        assert d.goal_pattern == ""
        assert d.outcome_filter == ""
        assert d.session_ids == []
        assert d.session_count == 0
        assert d.metrics_summary == {}

    def test_creation_full(self):
        now = datetime.now(timezone.utc)
        d = Dataset(
            id="dset_full1234",
            name="March debugging",
            description="All debugging sessions in March",
            created_at=now,
            date_min=now,
            date_max=now,
            agent_ids=["agent-a", "agent-b"],
            goal_pattern="debug.*",
            outcome_filter="success",
            session_ids=["s1", "s2", "s3"],
            session_count=3,
            metrics_summary={"avg_duration": 5000},
        )
        assert d.agent_ids == ["agent-a", "agent-b"]
        assert d.session_count == 3
        assert d.outcome_filter == "success"

    def test_json_round_trip(self):
        d = Dataset(
            id="dset_rt123456",
            name="Round trip test",
            agent_ids=["a1"],
            session_ids=["s1", "s2"],
            session_count=2,
        )
        data = d.model_dump_json()
        d2 = Dataset.model_validate_json(data)
        assert d2.id == "dset_rt123456"
        assert d2.session_ids == ["s1", "s2"]
        assert d2.session_count == 2


# ---------------------------------------------------------------------------
# Experiment model tests (gated behind PATTERN_EXPERIMENTS_ENABLED)
# ---------------------------------------------------------------------------


@_skip_experiments
class TestExperiment:
    def test_creation_defaults(self):
        e = Experiment(
            id="exp_abc12345",
            name="Memory-first test",
            hypothesis="Memory first improves success",
            pattern_id="pat_mf_123",
            dataset_id="dset_abc12345",
        )
        assert e.id == "exp_abc12345"
        assert e.status == "running"
        assert e.treatment_count == 0
        assert e.control_count == 0
        assert e.effect_size is None
        assert e.p_value is None
        assert e.confidence_interval is None
        assert e.verdict == ""

    def test_creation_full(self):
        e = Experiment(
            id="exp_full1234",
            name="Full experiment",
            hypothesis="Test hypothesis",
            pattern_id="pat_x",
            dataset_id="dset_y",
            status="concluded",
            treatment_count=50,
            control_count=45,
            effect_size=0.25,
            p_value=0.03,
            confidence_interval=(0.05, 0.45),
            verdict="significant",
        )
        assert e.status == "concluded"
        assert e.effect_size == 0.25
        assert e.p_value == 0.03
        assert e.confidence_interval == (0.05, 0.45)
        assert e.verdict == "significant"

    def test_json_round_trip(self):
        e = Experiment(
            id="exp_rt123456",
            name="RT test",
            hypothesis="test",
            pattern_id="pat_1",
            dataset_id="dset_1",
            effect_size=0.15,
            confidence_interval=(0.01, 0.29),
        )
        data = e.model_dump_json()
        e2 = Experiment.model_validate_json(data)
        assert e2.id == "exp_rt123456"
        assert e2.effect_size == 0.15
        assert e2.confidence_interval == (0.01, 0.29)


# ---------------------------------------------------------------------------
# Dataset storage tests — mocked Redis (gated behind PATTERN_EXPERIMENTS_ENABLED)
# ---------------------------------------------------------------------------


@_skip_experiments
class TestDatasetStorage:
    def test_store_and_get_dataset(self):
        from app.patterns.store import store_dataset

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis.zadd = AsyncMock(return_value=1)

        d = Dataset(id="dset_test1234", name="Test dataset")

        async def run():
            result = await store_dataset(mock_redis, d)
            assert result is True
            mock_redis.set.assert_called_once()
            mock_redis.zadd.assert_called_once()

        asyncio.run(run())

    def test_get_dataset_returns_none_when_missing(self):
        from app.patterns.store import get_dataset

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)

        async def run():
            result = await get_dataset(mock_redis, "dset_missing")
            assert result is None

        asyncio.run(run())

    def test_list_datasets(self):
        from app.patterns.store import list_datasets

        d1 = Dataset(id="dset_a", name="Dataset A")
        d2 = Dataset(id="dset_b", name="Dataset B")

        mock_redis = AsyncMock()
        mock_redis.zrevrange = AsyncMock(return_value=["dset_b", "dset_a"])

        def get_side_effect(key):
            if "dset_a" in key:
                return d1.model_dump_json()
            if "dset_b" in key:
                return d2.model_dump_json()
            return None

        mock_redis.get = AsyncMock(side_effect=get_side_effect)

        async def run():
            result = await list_datasets(mock_redis)
            assert len(result) == 2
            assert result[0].id == "dset_b"

        asyncio.run(run())

    def test_delete_dataset(self):
        from app.patterns.store import delete_dataset

        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock(return_value=1)
        mock_redis.zrem = AsyncMock(return_value=1)

        async def run():
            result = await delete_dataset(mock_redis, "dset_gone")
            assert result is True

        asyncio.run(run())

    def test_materialize_dataset(self):
        from app.patterns.store import materialize_dataset

        features = [
            SessionFeatures(session_id="s1", outcome="success", outcome_source="task_result", tags=["debug"]),
            SessionFeatures(session_id="s2", outcome="failure", outcome_source="task_result", tags=["debug"]),
            SessionFeatures(session_id="s3", outcome="success", outcome_source="task_result", tags=["feature"]),
        ]

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis.zadd = AsyncMock(return_value=1)

        d = Dataset(id="dset_mat12345", name="Debugging", outcome_filter="success")

        async def run():
            with patch("app.patterns.store.get_all_features", return_value=features):
                result = await materialize_dataset(mock_redis, d)
                assert result.session_count == 2  # s1 and s3 are successes
                assert "s1" in result.session_ids
                assert "s3" in result.session_ids
                assert "s2" not in result.session_ids

        asyncio.run(run())

    def test_materialize_dataset_with_goal_pattern(self):
        from app.patterns.store import materialize_dataset

        features = [
            SessionFeatures(session_id="s1", outcome="success", tags=["debugging", "auth"]),
            SessionFeatures(session_id="s2", outcome="success", tags=["feature", "api"]),
            SessionFeatures(session_id="s3", outcome="failure", tags=["debugging", "relay"]),
        ]

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis.zadd = AsyncMock(return_value=1)

        d = Dataset(id="dset_goal1234", name="Debug sessions", goal_pattern="debug")

        async def run():
            with patch("app.patterns.store.get_all_features", return_value=features):
                result = await materialize_dataset(mock_redis, d)
                assert result.session_count == 2  # s1 and s3 match "debug"
                assert "s2" not in result.session_ids

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Experiment storage tests — mocked Redis (gated behind PATTERN_EXPERIMENTS_ENABLED)
# ---------------------------------------------------------------------------


@_skip_experiments
class TestExperimentStorage:
    def test_store_and_list_experiments(self):
        from app.patterns.store import store_experiment

        e = Experiment(
            id="exp_test1234", name="Test", hypothesis="h",
            pattern_id="pat_1", dataset_id="dset_1",
        )

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis.zadd = AsyncMock(return_value=1)

        async def run():
            result = await store_experiment(mock_redis, e)
            assert result is True

        asyncio.run(run())

    def test_get_experiment_returns_none_when_missing(self):
        from app.patterns.store import get_experiment

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)

        async def run():
            result = await get_experiment(mock_redis, "exp_missing")
            assert result is None

        asyncio.run(run())

    def test_delete_experiment(self):
        from app.patterns.store import delete_experiment

        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock(return_value=1)
        mock_redis.zrem = AsyncMock(return_value=1)

        async def run():
            result = await delete_experiment(mock_redis, "exp_gone")
            assert result is True

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Statistics tests (gated behind PATTERN_EXPERIMENTS_ENABLED)
# ---------------------------------------------------------------------------


@_skip_experiments
class TestStatistics:
    def test_cohens_h_identical_rates(self):
        from app.patterns.statistics import _cohens_h
        assert _cohens_h(0.5, 0.5) == pytest.approx(0.0, abs=0.001)

    def test_cohens_h_different_rates(self):
        from app.patterns.statistics import _cohens_h
        h = _cohens_h(0.8, 0.5)
        assert h > 0.1  # Should be a meaningful effect

    def test_chi_square_significant(self):
        from app.patterns.statistics import _chi_square_2x2
        # Strong difference: 80% vs 40% success
        chi2, p = _chi_square_2x2(40, 10, 20, 30)
        assert chi2 > 0
        assert p < 0.05

    def test_chi_square_not_significant(self):
        from app.patterns.statistics import _chi_square_2x2
        # Similar rates: 50% vs 48%
        chi2, p = _chi_square_2x2(25, 25, 24, 26)
        assert p > 0.05

    def test_chi_square_empty_table(self):
        from app.patterns.statistics import _chi_square_2x2
        chi2, p = _chi_square_2x2(0, 0, 0, 0)
        assert chi2 == 0.0
        assert p == 1.0

    def test_confidence_interval(self):
        from app.patterns.statistics import _confidence_interval_diff
        ci = _confidence_interval_diff(0.8, 50, 0.5, 50)
        # Difference is 0.3, CI should contain 0.3 and be positive
        assert ci[0] > 0  # Lower bound positive
        assert ci[1] > ci[0]  # Upper > lower
        assert ci[0] < 0.3 < ci[1]  # Contains true difference

    def test_minimum_sample_size(self):
        from app.patterns.statistics import minimum_sample_size
        n = minimum_sample_size(baseline_rate=0.5, min_effect=0.1)
        assert n >= 5  # At least MIN_GROUP_SIZE
        assert n < 1000  # Reasonable for this effect size

    def test_compute_experiment_results_significant(self):
        from app.patterns.statistics import compute_experiment_results

        # Create features: 80% success with tip, 40% without
        features = []
        for i in range(50):
            features.append(SessionFeatures(
                session_id=f"trt_{i}",
                outcome="success" if i < 40 else "failure",
                outcome_source="task_result",
            ))
        for i in range(50):
            features.append(SessionFeatures(
                session_id=f"ctl_{i}",
                outcome="success" if i < 20 else "failure",
                outcome_source="task_result",
            ))

        tip_groups = {}
        for i in range(50):
            tip_groups[f"trt_{i}"] = {"pattern_ids": ["pat_test"], "group": "treatment"}
        for i in range(50):
            tip_groups[f"ctl_{i}"] = {"pattern_ids": ["pat_test"], "group": "control"}

        exp = Experiment(
            id="exp_sig12345",
            name="Significant test",
            hypothesis="Treatment works",
            pattern_id="pat_test",
            dataset_id="dset_1",
        )

        result = compute_experiment_results(exp, features, "pat_test", tip_groups)
        assert result.treatment_count == 50
        assert result.control_count == 50
        assert result.p_value < 0.05
        assert result.effect_size > 0.1
        assert result.verdict == "significant"

    def test_compute_experiment_results_insufficient_data(self):
        from app.patterns.statistics import compute_experiment_results

        features = [
            SessionFeatures(session_id="s1", outcome="success", outcome_source="task_result"),
            SessionFeatures(session_id="s2", outcome="failure", outcome_source="task_result"),
        ]
        tip_groups = {
            "s1": {"pattern_ids": ["pat_x"], "group": "treatment"},
        }

        exp = Experiment(
            id="exp_insuf123",
            name="Insufficient",
            hypothesis="h",
            pattern_id="pat_x",
            dataset_id="dset_1",
        )

        result = compute_experiment_results(exp, features, "pat_x", tip_groups)
        assert result.verdict == "insufficient data"

    def test_compute_experiment_results_not_significant(self):
        from app.patterns.statistics import compute_experiment_results

        # Create features: ~50% success in both groups
        features = []
        for i in range(40):
            features.append(SessionFeatures(
                session_id=f"trt_{i}",
                outcome="success" if i < 20 else "failure",
                outcome_source="task_result",
            ))
        for i in range(40):
            features.append(SessionFeatures(
                session_id=f"ctl_{i}",
                outcome="success" if i < 19 else "failure",
                outcome_source="task_result",
            ))

        tip_groups = {}
        for i in range(40):
            tip_groups[f"trt_{i}"] = {"pattern_ids": ["pat_ns"], "group": "treatment"}
        for i in range(40):
            tip_groups[f"ctl_{i}"] = {"pattern_ids": ["pat_ns"], "group": "control"}

        exp = Experiment(
            id="exp_ns1234567",
            name="Not significant",
            hypothesis="h",
            pattern_id="pat_ns",
            dataset_id="dset_1",
        )

        result = compute_experiment_results(exp, features, "pat_ns", tip_groups)
        assert result.verdict == "not significant"
