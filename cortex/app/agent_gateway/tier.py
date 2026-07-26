"""Tier classifier: maps an action shape + signals to auto | lightweight | full."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.agent_gateway.models import Action, Prediction, Tier

# Path patterns that indicate elevated risk (config, secrets, infra)
DENY_ADJACENT_PATTERNS = (
    r"\.env(\.|$)",
    r"\.(key|pem|crt|secret)$",
    r"(^|/)(docker-compose|Dockerfile)(\.|$)",
    r"(^|/)\.github/",
    r"(^|/)(terraform|infra)/",
)

# Command prefixes considered destructive
DESTRUCTIVE_COMMAND_PATTERNS = (
    r"\brm\s+-[rf]",
    r"(?i)\bdrop\s+(table|database)\b",
    r"(?i)\bdelete\s+from\b",
    r"\bgit\s+(reset\s+--hard|push\s+(.+\s+)?-f|push\s+(.+\s+)?--force)",
    r"(?i)\btruncate\b",
    r"\bmkfs\b",
)

# Safe command prefixes (formatters, linters that mutate but rarely break things)
SAFE_COMMAND_PATTERNS = (
    r"^\s*black\b",
    r"^\s*isort\b",
    r"^\s*prettier\b",
    r"^\s*gofmt\b",
    r"^\s*ruff\s+format\b",
)


@dataclass
class TierContext:
    action: Action
    prediction: Optional[Prediction] = None
    recent_failure_hit: bool = False
    fastpath_hit: bool = False
    session_clean_touch: bool = False


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(p, value) for p in patterns)


def _has_shell_chain(target: str) -> bool:
    """Return True if the target contains shell metacharacters that could chain commands."""
    return any(c in target for c in ("&&", "||", ";", "|", "`", "$("))


def classify_tier(ctx: TierContext) -> Tier:
    """Decide the tier for an action.

    Elevation beats demotion. Order:
      1. Start at lightweight.
      2. Elevate to full if any elevation signal fires (action shape alone).
      3. Otherwise, demote to auto if any demotion signal fires.
      4. If prediction is present, re-elevate to full when prediction shape suggests it.
    """
    elevation_reasons: list[str] = []

    if ctx.action.type == "delete":
        elevation_reasons.append("delete_action")
    if ctx.action.type == "run_command" and _matches_any(ctx.action.target, DESTRUCTIVE_COMMAND_PATTERNS):
        elevation_reasons.append("destructive_command")
    if _matches_any(ctx.action.target, DENY_ADJACENT_PATTERNS):
        elevation_reasons.append("deny_adjacent_path")
    if ctx.recent_failure_hit:
        elevation_reasons.append("recent_failure")

    if elevation_reasons:
        # Allow prediction to further confirm full (no demotion possible)
        return "full"

    # Try demotion to auto
    demotion_reasons: list[str] = []
    if (
        ctx.action.type == "run_command"
        and _matches_any(ctx.action.target, SAFE_COMMAND_PATTERNS)
        and not _has_shell_chain(ctx.action.target)
    ):
        demotion_reasons.append("safe_command")
    if ctx.fastpath_hit:
        demotion_reasons.append("fastpath")
    if ctx.session_clean_touch:
        demotion_reasons.append("session_clean_touch")

    if demotion_reasons:
        return "auto"

    tier: Tier = "lightweight"

    # Prediction-based re-elevation
    if ctx.prediction is not None and len(ctx.prediction.expected_changes) > 3:
        tier = "full"
    if ctx.prediction is not None and any(
        _matches_any(c, DENY_ADJACENT_PATTERNS) for c in ctx.prediction.expected_changes
    ):
        tier = "full"

    return tier
