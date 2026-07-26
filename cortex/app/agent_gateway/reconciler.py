"""Reconciler — compare predictions to outcomes and compute match scores."""

from __future__ import annotations

from app.agent_gateway.models import Outcome, Prediction


def _normalize_path(p: str) -> str:
    return p.replace("\\", "/").rstrip("/")


def _changes_match(predicted: str, actual: list[str]) -> bool:
    pred_norm = _normalize_path(predicted)
    # Directory prediction (ends with /) becomes prefix match
    if predicted.endswith("/"):
        return any(_normalize_path(a).startswith(pred_norm + "/") for a in actual)
    # File prediction: exact or endswith match
    for a in actual:
        a_norm = _normalize_path(a)
        if a_norm == pred_norm or a_norm.endswith("/" + pred_norm) or pred_norm.endswith("/" + a_norm):
            return True
    return False


def compute_prediction_match_score(pred: Prediction, out: Outcome) -> float:
    """Arithmetic mean of criteria_score and changes_score.

    Empty component (no criteria predicted, no changes predicted) contributes 1.0.
    """
    # Criteria: exact-match enum codes; for parameterized like FILE_EXISTS:path,
    # the full string must appear in observed_criteria_met.
    if pred.success_criteria:
        observed = set(out.observed_criteria_met)
        matched = sum(1 for c in pred.success_criteria if c in observed)
        criteria_score = matched / len(pred.success_criteria)
    else:
        criteria_score = 1.0

    # Changes: normalized path match, prefix for directories.
    if pred.expected_changes:
        matched = sum(1 for c in pred.expected_changes if _changes_match(c, out.actual_changes))
        changes_score = matched / len(pred.expected_changes)
    else:
        changes_score = 1.0

    return round((criteria_score + changes_score) / 2.0, 4)
