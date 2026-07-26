from app.agent_gateway.models import Outcome, Prediction
from app.agent_gateway.reconciler import compute_prediction_match_score


def test_perfect_match_scores_one():
    pred = Prediction(
        intent="x",
        expected_changes=["src/foo.py"],
        success_criteria=["TESTS_PASS"],
        confidence=0.9,
    )
    out = Outcome(success=True, actual_changes=["src/foo.py"], observed_criteria_met=["TESTS_PASS"])
    assert compute_prediction_match_score(pred, out) == 1.0


def test_partial_criteria_match():
    pred = Prediction(
        intent="x",
        expected_changes=["src/foo.py"],
        success_criteria=["TESTS_PASS", "BUILD_OK"],
        confidence=0.9,
    )
    out = Outcome(success=True, actual_changes=["src/foo.py"], observed_criteria_met=["TESTS_PASS"])
    # criteria_score = 0.5, changes_score = 1.0, mean = 0.75
    assert compute_prediction_match_score(pred, out) == 0.75


def test_changes_prefix_match_for_directories():
    pred = Prediction(
        intent="x",
        expected_changes=["cortex/app/"],
        success_criteria=[],
        confidence=0.9,
    )
    out = Outcome(success=True, actual_changes=["cortex/app/foo.py"], observed_criteria_met=[])
    # criteria_score is 0 over 0 → treat as 1.0; changes_score = 1.0; mean = 1.0
    assert compute_prediction_match_score(pred, out) == 1.0


def test_empty_prediction_components_gives_one():
    pred = Prediction(intent="x", expected_changes=[], success_criteria=[], confidence=0.9)
    out = Outcome(success=True)
    assert compute_prediction_match_score(pred, out) == 1.0


def test_total_miss_scores_zero():
    pred = Prediction(
        intent="x",
        expected_changes=["src/foo.py"],
        success_criteria=["TESTS_PASS"],
        confidence=0.9,
    )
    out = Outcome(success=False, actual_changes=["src/bar.py"], observed_criteria_met=[])
    assert compute_prediction_match_score(pred, out) == 0.0


def test_normalized_path_match_handles_relative_and_absolute():
    pred = Prediction(intent="x", expected_changes=["src/foo.py"], confidence=0.9)
    out = Outcome(success=True, actual_changes=["/repo/src/foo.py"])
    # endswith match — score 1.0 on changes
    score = compute_prediction_match_score(pred, out)
    assert score == 1.0
