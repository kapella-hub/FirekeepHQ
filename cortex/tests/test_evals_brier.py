from app.evals.scorers import brier_score


def test_brier_perfect_calibration():
    actions = [
        {"prediction_confidence": 1.0, "prediction_match_score": 1.0},
        {"prediction_confidence": 0.0, "prediction_match_score": 0.0},
    ]
    assert brier_score(actions) == 0.0


def test_brier_perfect_miscalibration():
    actions = [
        {"prediction_confidence": 1.0, "prediction_match_score": 0.0},
        {"prediction_confidence": 0.0, "prediction_match_score": 1.0},
    ]
    assert brier_score(actions) == 1.0


def test_brier_empty_returns_none():
    assert brier_score([]) is None


def test_brier_excludes_actions_without_score():
    actions = [
        {"prediction_confidence": 0.5, "prediction_match_score": None},
        {"prediction_confidence": 1.0, "prediction_match_score": 1.0},
    ]
    assert brier_score(actions) == 0.0


def test_brier_excludes_actions_without_confidence():
    actions = [
        {"prediction_confidence": None, "prediction_match_score": 1.0},
        {"prediction_confidence": 1.0, "prediction_match_score": 1.0},
    ]
    assert brier_score(actions) == 0.0


def test_brier_partial_score():
    actions = [
        {"prediction_confidence": 0.8, "prediction_match_score": 0.6},  # diff^2 = 0.04
        {"prediction_confidence": 0.5, "prediction_match_score": 0.5},  # diff^2 = 0.0
    ]
    # Mean = 0.02
    assert brier_score(actions) == 0.02
