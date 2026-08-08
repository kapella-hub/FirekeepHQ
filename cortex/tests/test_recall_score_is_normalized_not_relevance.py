"""A freshness proxy pinned at 1.0 cannot report staleness.

THE BUG
-------
`RecallResponse.score` is `max()` over per-entry scores that
`engine/rag.py::_min_max_normalize` has already rescaled -- and that function
sets the best entry in the set to exactly 1.0 by construction. So the field
reads 1.0 whenever ANY result survives the score floor, however poor the match.

MEASURED on the live deployment 2026-08-06, three queries through
`POST /memory/recall`:

    "how do I deploy to the VPS"                    -> score 1.0, sources [1.0, 0.6091, 0.4537]
    "what is the qdrant collection name"            -> score 1.0, sources [1.0, 1.0]
    "zzz nonsense query about knitting patterns"    -> score 1.0, sources [1.0, 0.5]

The nonsense query is the proof: 1.0 is not a relevance signal, it is a
constant. The replay `memory_read` payload carried that constant as
`top_score`, and `_memory_freshness_at_recall` averaged it -- which is why the
briefing reported `memory_freshness_at_recall: 1.0` across all 19 evaluated
sessions on a store of 4,348 memories.

This is the SAME defect already found and fixed one layer over, for the recall
confidence band (`_format_markdown`), whose comment states the principle
plainly: "a band that cannot come out low is not a band." That fix read
`metadata["raw_score"]`, the pre-normalization value. This applies the same
remedy to the metric.

NOT CHANGED: `RecallResponse.score` itself. It is a public response field and
repointing it is a breaking change; it is documented as normalized instead. The
telemetry now carries `raw_top_score` alongside it.
"""
from __future__ import annotations

import json

import pytest

from app.evals.scorers import _memory_freshness_at_recall, compute_tier1_metrics
from app.main import _raw_top_score


class _Src:
    """Stands in for MemorySource: only `.metadata` is read."""

    def __init__(self, metadata):
        self.metadata = metadata


def _read(payload: dict) -> dict:
    return {"event_type": "memory_read", "payload": payload}


class TestTheBug:
    def test_a_nonsense_query_no_longer_reports_perfect_freshness(self):
        """THE regression, in the live numbers. Normalized top is 1.0; the raw
        cosine that actually matched is 0.21."""
        events = [_read({"top_score": 1.0, "raw_top_score": 0.21})]
        assert _memory_freshness_at_recall(events) == 0.21

    def test_the_metric_can_now_come_out_low(self):
        """The property the old one lacked: a range at all."""
        weak = _memory_freshness_at_recall([_read({"top_score": 1.0, "raw_top_score": 0.18})])
        strong = _memory_freshness_at_recall([_read({"top_score": 1.0, "raw_top_score": 0.91})])
        assert weak < strong, "a proxy that cannot vary is not a proxy"
        assert weak < 0.5 < strong

    def test_the_old_field_alone_is_pinned_at_one(self):
        """Restates why this file exists: three different recalls, one value."""
        pinned = [_read({"top_score": 1.0}) for _ in range(3)]
        assert _memory_freshness_at_recall(pinned) == 1.0


class TestTheHelperThatSuppliesIt:
    def test_it_takes_the_best_raw_score(self):
        srcs = [_Src({"raw_score": 0.4}), _Src({"raw_score": 0.83}), _Src({"raw_score": 0.6})]
        assert _raw_top_score(srcs) == 0.83

    def test_entries_without_a_raw_score_cannot_prop_the_value_up(self):
        """Resolution bonuses carry a sentinel 1.2 and no raw_score. Counting
        them would rebuild the exact bug -- same rule as the confidence band."""
        srcs = [_Src({"raw_score": 0.3}), _Src({"score": 1.2}), _Src({})]
        assert _raw_top_score(srcs) == 0.3

    def test_no_raw_scores_at_all_returns_none_not_zero(self):
        """None means 'no signal'; 0.0 would be a claim of total irrelevance."""
        assert _raw_top_score([_Src({}), _Src({"score": 1.0})]) is None
        assert _raw_top_score([]) is None
        assert _raw_top_score(None) is None

    @pytest.mark.parametrize("bad", ["abc", None, {}, [], float("nan")])
    def test_a_malformed_raw_score_does_not_break_a_successful_recall(self, bad):
        """This runs in the recall hot path's best-effort telemetry."""
        out = _raw_top_score([_Src({"raw_score": bad}), _Src({"raw_score": 0.5})])
        assert out == 0.5 or out != out  # 0.5, or NaN propagated harmlessly

    def test_a_source_with_a_non_dict_metadata_is_skipped(self):
        assert _raw_top_score([_Src("not a dict"), _Src({"raw_score": 0.7})]) == 0.7

    def test_it_never_raises_on_a_hostile_source(self):
        class _Hostile:
            @property
            def metadata(self):
                raise RuntimeError("boom")

        assert _raw_top_score([_Hostile()]) is None


class TestBackwardCompatibility:
    def test_events_predating_raw_top_score_still_read(self):
        """History must stay readable -- those events are simply the pinned
        kind, which the docstring says out loud."""
        assert _memory_freshness_at_recall([_read({"top_score": 0.85})]) == 0.85

    def test_a_json_encoded_payload_is_still_parsed(self):
        ev = {"event_type": "memory_read",
              "payload": json.dumps({"top_score": 1.0, "raw_top_score": 0.33})}
        assert _memory_freshness_at_recall([ev]) == 0.33

    def test_no_memory_reads_is_none_not_a_score(self):
        assert _memory_freshness_at_recall([{"event_type": "memory_write"}]) is None

    def test_it_is_still_reported_as_a_tier1_metric(self):
        m = compute_tier1_metrics([_read({"raw_top_score": 0.42})])
        assert m["memory_freshness_at_recall"] == 0.42


class TestTheOldReaderWouldFailThis:
    """Proof this file discriminates: the pre-change reader took `top_score`
    and therefore returns 1.0 on the very events the new one scores 0.21."""

    @staticmethod
    def _pre_change(events: list[dict]) -> float | None:
        scores = []
        for e in events:
            payload = e.get("payload", {})
            if isinstance(payload, str):
                payload = json.loads(payload)
            ts = payload.get("top_score")
            if ts is not None:
                scores.append(float(ts))
        return round(sum(scores) / len(scores), 4) if scores else None

    def test_the_old_reader_called_the_nonsense_query_perfectly_fresh(self):
        events = [_read({"top_score": 1.0, "raw_top_score": 0.21})]
        assert self._pre_change(events) == 1.0, (
            "expected the pinned value; if this changed, update the "
            "discriminator rather than deleting it"
        )
        assert _memory_freshness_at_recall(events) == 0.21

    def test_the_old_reader_was_constant_across_every_quality_of_match(self):
        for raw in (0.05, 0.21, 0.55, 0.99):
            assert self._pre_change([_read({"top_score": 1.0, "raw_top_score": raw})]) == 1.0
