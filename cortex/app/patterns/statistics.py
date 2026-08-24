"""Statistical analysis for pattern experiments.

Computes chi-square tests, Cohen's h effect size, and confidence intervals
for comparing treatment vs control group success rates.
"""

from __future__ import annotations

import math
import logging

from app.patterns.models import Experiment, SessionFeatures, graded_only

logger = logging.getLogger(__name__)

# Minimum sessions per group for meaningful analysis
MIN_GROUP_SIZE = 5


def _cohens_h(p1: float, p2: float) -> float:
    """Compute Cohen's h effect size for two proportions.

    h = 2 * arcsin(sqrt(p1)) - 2 * arcsin(sqrt(p2))
    Returns absolute value.
    """
    return abs(2 * math.asin(math.sqrt(p1)) - 2 * math.asin(math.sqrt(p2)))


def _chi_square_2x2(a: int, b: int, c: int, d: int) -> tuple[float, float]:
    """Chi-square test for a 2x2 contingency table.

    Layout:
        | success | failure |
    trt |    a    |    b    |
    ctl |    c    |    d    |

    Returns (chi2, p_value). Uses scipy if available, otherwise
    falls back to a manual computation with survival function approximation.
    """
    n = a + b + c + d
    if n == 0:
        return 0.0, 1.0

    try:
        from scipy.stats import chi2_contingency
        import numpy as np

        table = np.array([[a, b], [c, d]])
        # Use correction=False for consistency (Pearson chi-square)
        chi2, p, _, _ = chi2_contingency(table, correction=False)
        return float(chi2), float(p)
    except ImportError:
        logger.debug("scipy not available, using manual chi-square")

    # Manual Pearson chi-square with continuity correction
    row1 = a + b
    row2 = c + d
    col1 = a + c
    col2 = b + d

    if row1 == 0 or row2 == 0 or col1 == 0 or col2 == 0:
        return 0.0, 1.0

    expected_a = row1 * col1 / n
    expected_b = row1 * col2 / n
    expected_c = row2 * col1 / n
    expected_d = row2 * col2 / n

    chi2 = (
        (a - expected_a) ** 2 / expected_a
        + (b - expected_b) ** 2 / expected_b
        + (c - expected_c) ** 2 / expected_c
        + (d - expected_d) ** 2 / expected_d
    )

    # Approximate p-value for 1 df chi-square using survival function
    p = _chi2_survival(chi2, df=1)
    return chi2, p


def _chi2_survival(x: float, df: int = 1) -> float:
    """Approximate upper-tail p-value for chi-square distribution (1 df).

    Uses the normal approximation: for df=1, chi2 ~ Z^2 where Z ~ N(0,1).
    P(chi2 > x) = 2 * P(Z > sqrt(x)) = erfc(sqrt(x/2)).
    """
    if x <= 0:
        return 1.0
    return math.erfc(math.sqrt(x / 2))


def _confidence_interval_diff(
    p1: float, n1: int, p2: float, n2: int, z: float = 1.96
) -> tuple[float, float]:
    """95% Wald confidence interval for the difference p1 - p2."""
    diff = p1 - p2
    if n1 == 0 or n2 == 0:
        return (diff, diff)
    se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    return (round(diff - z * se, 4), round(diff + z * se, 4))


def minimum_sample_size(
    baseline_rate: float = 0.5,
    min_effect: float = 0.1,
    alpha: float = 0.05,
    power: float = 0.8,
) -> int:
    """Estimate minimum sample size per group for detecting a given effect.

    Uses the formula for comparing two proportions:
    n = (Z_alpha + Z_beta)^2 * (p1(1-p1) + p2(1-p2)) / (p1 - p2)^2
    """
    from scipy.stats import norm

    p1 = baseline_rate
    p2 = baseline_rate + min_effect

    # Clamp to valid range
    p2 = max(0.01, min(0.99, p2))

    z_alpha = norm.ppf(1 - alpha / 2)
    z_beta = norm.ppf(power)

    diff = p2 - p1
    if abs(diff) < 1e-10:
        return 9999

    n = (z_alpha + z_beta) ** 2 * (p1 * (1 - p1) + p2 * (1 - p2)) / diff**2
    return max(MIN_GROUP_SIZE, math.ceil(n))


def compute_experiment_results(
    experiment: Experiment,
    features: list[SessionFeatures],
    tip_pattern_id: str,
    tip_groups: dict[str, dict],
) -> Experiment:
    """Compute statistical results for an experiment.

    Splits features into treatment (tip shown) and control (tip withheld or not shown),
    computes success rates, chi-square test, Cohen's h, and CI.

    Args:
        experiment: The experiment to update with results.
        features: SessionFeatures for the dataset.
        tip_pattern_id: The pattern ID being tested.
        tip_groups: Dict mapping session_id -> {"pattern_ids": [...], "group": "treatment"|"control"}.

    Returns:
        Updated experiment with statistical results.
    """
    features = graded_only(features)

    treatment_success = 0
    treatment_total = 0
    control_success = 0
    control_total = 0

    for f in features:
        log = tip_groups.get(f.session_id)
        if log and tip_pattern_id in log.get("pattern_ids", []):
            if log.get("group") == "control":
                control_total += 1
                if f.outcome == "success":
                    control_success += 1
            else:
                treatment_total += 1
                if f.outcome == "success":
                    treatment_success += 1
        else:
            # Sessions without tip log go to control
            control_total += 1
            if f.outcome == "success":
                control_success += 1

    experiment.treatment_count = treatment_total
    experiment.control_count = control_total

    if treatment_total < MIN_GROUP_SIZE or control_total < MIN_GROUP_SIZE:
        experiment.verdict = "insufficient data"
        return experiment

    p_treatment = treatment_success / treatment_total
    p_control = control_success / control_total

    # Chi-square test
    a = treatment_success
    b = treatment_total - treatment_success
    c = control_success
    d = control_total - control_success
    _, p_value = _chi_square_2x2(a, b, c, d)

    # Effect size (Cohen's h)
    effect = _cohens_h(p_treatment, p_control)

    # Confidence interval on rate difference
    ci = _confidence_interval_diff(p_treatment, treatment_total, p_control, control_total)

    experiment.effect_size = round(effect, 4)
    experiment.p_value = round(p_value, 6)
    experiment.confidence_interval = ci

    # Verdict
    if p_value < 0.05 and effect > 0.1:
        experiment.verdict = "significant"
    elif p_value < 0.05:
        experiment.verdict = "statistically significant but small effect"
    elif treatment_total + control_total < 30:
        experiment.verdict = "insufficient data"
    else:
        experiment.verdict = "not significant"

    return experiment
