"""Tests for the confidence scoring module."""


from app.confidence import compute_confidence, confidence_for_recall_boost


class TestComputeConfidence:
    def test_default_agent_confidence(self):
        assert compute_confidence() == 0.7

    def test_user_created_higher(self):
        assert compute_confidence(created_by="user") == 0.9

    def test_sleep_cycle_lower(self):
        assert compute_confidence(created_by="sleep_cycle") == 0.5

    def test_unknown_source_uses_default(self):
        assert compute_confidence(created_by="some_new_source") == 0.7

    def test_confirmations_boost(self):
        base = compute_confidence(confirmed_count=0)
        boosted = compute_confidence(confirmed_count=3)
        assert boosted > base

    def test_confirmation_bonus_caps(self):
        # 10 confirmations should not exceed cap
        c10 = compute_confidence(confirmed_count=10)
        c20 = compute_confidence(confirmed_count=20)
        assert c10 == c20  # Both at cap

    def test_contradictions_penalize(self):
        base = compute_confidence(contradicted_count=0)
        penalized = compute_confidence(contradicted_count=2)
        assert penalized < base

    def test_contradiction_penalty_caps(self):
        # Even with many contradictions, confidence doesn't go below 0.1
        c = compute_confidence(contradicted_count=100)
        assert c >= 0.1

    def test_max_confidence_is_one(self):
        c = compute_confidence(created_by="user", confirmed_count=100)
        assert c <= 1.0

    def test_combined_signals(self):
        # Agent with 2 confirmations and 1 contradiction
        c = compute_confidence(
            created_by="agent",
            confirmed_count=2,
            contradicted_count=1,
        )
        # 0.7 + 0.10 - 0.15 = 0.65
        assert c == 0.65

    def test_heavily_contradicted_user_memory(self):
        c = compute_confidence(created_by="user", contradicted_count=3)
        # 0.9 - 0.45 = 0.45
        assert c == 0.45


class TestRecallBoost:
    def test_high_confidence_boosts(self):
        boost = confidence_for_recall_boost(1.0)
        assert boost > 1.0

    def test_low_confidence_penalizes(self):
        boost = confidence_for_recall_boost(0.1)
        assert boost < 1.0

    def test_mid_confidence_near_neutral(self):
        boost = confidence_for_recall_boost(0.7)
        assert 0.9 < boost < 1.1

    def test_boost_range(self):
        low = confidence_for_recall_boost(0.1)
        high = confidence_for_recall_boost(1.0)
        assert low >= 0.5
        assert high <= 1.2
