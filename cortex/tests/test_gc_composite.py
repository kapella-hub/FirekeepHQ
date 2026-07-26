from app.workers.gc import compute_eviction_score

HALF_LIVES = {"reference": float("inf"), "procedural": 180, "episodic": 90, "transient": 14}

def test_never_accessed_old_memory_evicts():
    score = compute_eviction_score(
        age_days=200, memory_type="episodic",
        access_count=0, confidence=0.3
    )
    assert score > 1.5  # should be pruned at default threshold

def test_frequently_accessed_memory_survives():
    score = compute_eviction_score(
        age_days=200, memory_type="episodic",
        access_count=20, confidence=0.7
    )
    assert score < 1.5  # should survive

def test_confirmed_memory_not_evaluated_for_eviction():
    score = compute_eviction_score(
        age_days=9999, memory_type="transient",
        access_count=0, confidence=0.1
    )
    assert score > 0  # formula returns a number; caller checks confirmed_count

def test_reference_memory_never_evicts():
    score = compute_eviction_score(
        age_days=99999, memory_type="reference",
        access_count=0, confidence=0.0
    )
    assert score == 0.0  # infinite half-life → age_ratio = 0

def test_high_confidence_reduces_score():
    low_conf = compute_eviction_score(100, "episodic", 0, 0.1)
    high_conf = compute_eviction_score(100, "episodic", 0, 0.9)
    assert low_conf > high_conf


def test_efficacy_factor_neutral_at_half():
    """OWM: neutral efficacy (0.5, incl. never-scored memories) must leave the
    eviction score bit-identical to the pre-OWM formula."""
    base = compute_eviction_score(age_days=90, memory_type="episodic",
                                  access_count=0, confidence=0.0)
    with_neutral = compute_eviction_score(age_days=90, memory_type="episodic",
                                          access_count=0, confidence=0.0,
                                          efficacy=0.5)
    assert base == with_neutral


def test_low_efficacy_raises_eviction_score_high_lowers_it():
    lo = compute_eviction_score(age_days=90, memory_type="episodic",
                                access_count=0, confidence=0.0, efficacy=0.0)
    hi = compute_eviction_score(age_days=90, memory_type="episodic",
                                access_count=0, confidence=0.0, efficacy=1.0)
    mid = compute_eviction_score(age_days=90, memory_type="episodic",
                                 access_count=0, confidence=0.0, efficacy=0.5)
    assert lo > mid > hi


def test_owm_efficacy_for_eviction_kill_switch_and_falsy_zero():
    from app.workers.gc import owm_efficacy_for_eviction
    # disabled -> neutral even with a stale stored penalty
    assert owm_efficacy_for_eviction({"owm_efficacy": 0.1}, enabled=False) == 0.5
    # enabled -> a stored 0.0 is the MAXIMUM penalty, not falsy-neutral
    assert owm_efficacy_for_eviction({"owm_efficacy": 0.0}, enabled=True) == 0.0
    # absent field -> neutral
    assert owm_efficacy_for_eviction({}, enabled=True) == 0.5
