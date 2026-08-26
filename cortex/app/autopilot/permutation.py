"""Exact/Monte-Carlo permutation test on member-level proportions (PR5 D8).

The randomization unit is the member, so the confirmatory test permutes
MEMBERS across arms — never sessions. Pure stdlib, deterministic: the exact
path enumerates every reassignment; the Monte-Carlo path uses a fixed seed
(a pre-registered analysis must give the same p on every run over the same
snapshot).
"""
import math
import random
from itertools import combinations


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def permutation_test_member_means(
    arm_a: list[float], arm_b: list[float], *,
    max_exact: int = 10000, mc_draws: int = 10000,
) -> dict:
    if not arm_a or not arm_b:
        raise ValueError("both arms need at least one member")
    pooled = list(arm_a) + list(arm_b)
    n_a = len(arm_a)
    observed = _mean(arm_a) - _mean(arm_b)
    total = math.comb(len(pooled), n_a)
    threshold = abs(observed) - 1e-12  # float-tolerant >=

    def diff_of(indices: tuple[int, ...]) -> float:
        chosen = set(indices)
        a = [pooled[i] for i in chosen]
        b = [pooled[i] for i in range(len(pooled)) if i not in chosen]
        return _mean(a) - _mean(b)

    if total <= max_exact:
        hits = sum(
            1 for idx in combinations(range(len(pooled)), n_a)
            if abs(diff_of(idx)) >= threshold
        )
        return {"p_value": hits / total, "diff": observed,
                "mean_a": _mean(arm_a), "mean_b": _mean(arm_b),
                "method": "exact", "reassignments": total}

    rng = random.Random(0)
    indices = list(range(len(pooled)))
    hits = 0
    for _ in range(mc_draws):
        sample = tuple(rng.sample(indices, n_a))
        if abs(diff_of(sample)) >= threshold:
            hits += 1
    # +1/+1: the observed assignment is always a member of the null set —
    # keeps Monte-Carlo p strictly positive and slightly conservative.
    return {"p_value": (hits + 1) / (mc_draws + 1), "diff": observed,
            "mean_a": _mean(arm_a), "mean_b": _mean(arm_b),
            "method": "monte_carlo", "reassignments": mc_draws}
