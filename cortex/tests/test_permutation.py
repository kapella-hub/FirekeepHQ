"""PR5 D8: the member-level primary analysis. The 4-vs-4 case is fully
hand-worked; the 3-vs-3 floor case proves why D8 requires >= 5 members/arm
(C(6,3)=20 -> the smallest attainable two-sided p is exactly 2/20 = 0.1,
which can never satisfy p < 0.05)."""
from app.autopilot.permutation import permutation_test_member_means


def test_four_vs_four_hand_worked():
    # Members' graded fractions. Observed diff = 0.75 - 0.25 = 0.5.
    a = [1.0, 0.8, 0.6, 0.6]   # mean 0.75
    b = [0.4, 0.3, 0.2, 0.1]   # mean 0.25
    r = permutation_test_member_means(a, b)
    assert r["method"] == "exact"
    assert r["reassignments"] == 70          # C(8,4)
    assert abs(r["diff"] - 0.5) < 1e-12
    # Hand count: pooled = [1.0,.8,.6,.6,.4,.3,.2,.1]. |mean_A - mean_B|
    # >= 0.5 holds only for the observed split and its mirror -> p = 2/70.
    assert abs(r["p_value"] - 2 / 70) < 1e-12


def test_three_vs_three_floor_is_two_twentieths():
    a, b = [1.0, 1.0, 1.0], [0.0, 0.0, 0.0]
    r = permutation_test_member_means(a, b)
    assert r["reassignments"] == 20          # C(6,3)
    assert abs(r["p_value"] - 2 / 20) < 1e-12  # observed + mirror


def test_null_data_is_not_significant():
    a = [0.5, 0.5, 0.5, 0.5, 0.5]
    b = [0.5, 0.5, 0.5, 0.5, 0.5]
    r = permutation_test_member_means(a, b)
    assert r["p_value"] == 1.0
    assert r["diff"] == 0.0


def test_monte_carlo_kicks_in_and_is_deterministic():
    a = [i / 20 for i in range(10)]
    b = [(i + 5) / 20 for i in range(10)]    # C(20,10) = 184756 > 10000
    r1 = permutation_test_member_means(a, b)
    r2 = permutation_test_member_means(a, b)
    assert r1["method"] == "monte_carlo"
    assert r1["p_value"] == r2["p_value"]    # fixed seed -> reproducible
