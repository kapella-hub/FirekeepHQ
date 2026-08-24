"""Policy rules — individual evaluators for the policy engine.

Each rule checks one signal and returns (action, risk_contribution, reason).
Rules are designed to be self-contained and testable in isolation.
"""

from __future__ import annotations

import fnmatch
import logging

import redis.asyncio as aioredis

from app.policy.engine import PolicyContext, PolicyRule

logger = logging.getLogger(__name__)


class LeaseRule(PolicyRule):
    """Lease conflicts are checked by the precheck hook before this rule runs.

    This rule is a pass-through that exists in the rules list so the lease
    check shows up in the policy decision pipeline; the actual lease
    verification happens upstream in the bash hook calling Relay.
    """

    name = "lease"

    async def evaluate(self, context: PolicyContext) -> tuple[str, float, str]:
        return ("allow", 0.0, "")


class FileRiskRule(PolicyRule):
    """Check the pattern engine for file_hotspot patterns matching the file path.

    Files that appear frequently in failure sessions are flagged as risky.
    """

    name = "file_risk"

    def __init__(self, get_replay_redis=None):
        super().__init__()
        self._get_redis = get_replay_redis

    async def evaluate(self, context: PolicyContext) -> tuple[str, float, str]:
        if not self._get_redis:
            return ("allow", 0.0, "")

        try:
            r: aioredis.Redis = self._get_redis()
            from app.patterns.store import get_patterns

            patterns = await get_patterns(r, limit=100)
            for p in patterns:
                if p.pattern_type != "file_hotspot":
                    continue
                # File hotspot patterns store file paths in tags
                for tag in p.tags:
                    if tag in context.file_path or context.file_path.endswith(tag):
                        risk = min(p.confidence * 0.5, 0.5)
                        if p.confidence >= 0.8:
                            return (
                                "warn",
                                risk,
                                f"High-risk file (hotspot confidence {p.confidence:.2f}): {p.description}",
                            )
                        return (
                            "allow",
                            risk,
                            "",
                        )
            return ("allow", 0.0, "")
        except Exception as exc:
            logger.debug("FileRiskRule error: %s", exc)
            return ("allow", 0.0, "")


class SessionHealthRule(PolicyRule):
    """Check current session's eval metrics for high failure rate.

    If the session has a high failure rate, warn before allowing more edits.
    """

    name = "session_health"

    def __init__(self, get_replay_redis=None):
        super().__init__()
        self._get_redis = get_replay_redis

    async def evaluate(self, context: PolicyContext) -> tuple[str, float, str]:
        if not self._get_redis or not context.session_id:
            return ("allow", 0.0, "")

        try:
            r: aioredis.Redis = self._get_redis()
            from app.evals.store import get_eval

            ev = await get_eval(r, context.session_id)
            if not ev:
                return ("allow", 0.0, "")

            failure_rate = ev.metrics.get("failure_rate", 0.0)
            if failure_rate >= 0.5:
                return (
                    "warn",
                    0.3,
                    f"Session has high failure rate ({failure_rate:.0%}). Consider reviewing before more edits.",
                )
            return ("allow", failure_rate * 0.2, "")
        except Exception as exc:
            logger.debug("SessionHealthRule error: %s", exc)
            return ("allow", 0.0, "")


class PathDenyRule(PolicyRule):
    """Block edits to files matching configurable deny glob patterns.

    Deny patterns are comma-separated globs like ".env,*.key,*.pem".
    """

    name = "path_deny"

    def __init__(self, deny_patterns: list[str] | None = None):
        super().__init__()
        self.deny_patterns: list[str] = deny_patterns or []

    async def evaluate(self, context: PolicyContext) -> tuple[str, float, str]:
        if not self.deny_patterns:
            return ("allow", 0.0, "")

        file_path = context.file_path
        # Normalize: use forward slashes, get basename for simple patterns
        file_path_normalized = file_path.replace("\\", "/")
        basename = file_path_normalized.rsplit("/", 1)[-1] if "/" in file_path_normalized else file_path_normalized

        for pattern in self.deny_patterns:
            pattern = pattern.strip()
            if not pattern:
                continue
            # Match against both full path and basename
            if fnmatch.fnmatch(basename, pattern) or fnmatch.fnmatch(file_path_normalized, pattern):
                return (
                    "block",
                    1.0,
                    f"File matches deny pattern '{pattern}'",
                )

        return ("allow", 0.0, "")


class RecentFailureRule(PolicyRule):
    """Check if recent edits to this file have failed.

    Looks at pattern engine file_hotspot data for the specific file.
    If the file appears in recent failure sessions, warn.
    """

    name = "recent_failure"

    def __init__(self, get_replay_redis=None):
        super().__init__()
        self._get_redis = get_replay_redis

    async def evaluate(self, context: PolicyContext) -> tuple[str, float, str]:
        if not self._get_redis:
            return ("allow", 0.0, "")

        try:
            r: aioredis.Redis = self._get_redis()
            from app.patterns.store import get_all_features
            from app.patterns.models import graded_only

            # Check recent sessions for failures involving this file. Rates
            # count graded evidence only -- an unknown-outcome session must
            # not dilute (or inflate) a real failure rate.
            features = graded_only(await get_all_features(r, limit=20))
            if not features:
                return ("allow", 0.0, "")

            file_path_normalized = context.file_path.replace("\\", "/")
            failure_count = 0
            total_with_file = 0

            for f in features:
                file_match = any(
                    file_path_normalized.endswith(fp.replace("\\", "/")) or fp.replace("\\", "/").endswith(file_path_normalized)
                    for fp in f.file_paths
                )
                if file_match:
                    total_with_file += 1
                    if f.outcome == "failure":
                        failure_count += 1

            if total_with_file >= 3 and failure_count / total_with_file >= 0.5:
                return (
                    "warn",
                    0.3,
                    f"Recent sessions editing this file have a high failure rate ({failure_count}/{total_with_file}).",
                )
            return ("allow", 0.0, "")
        except Exception as exc:
            logger.debug("RecentFailureRule error: %s", exc)
            return ("allow", 0.0, "")


class PredictionConfidenceRule(PolicyRule):
    """Require a prediction on elevated tiers; reject low-confidence predictions on full tier.

    Returns the new 'rethink' verdict (handled by AgentGateway service layer
    which interprets the (action, reason) pair).
    """

    name = "prediction_confidence"

    def __init__(self, threshold: float = 0.6):
        super().__init__()
        self.threshold = threshold

    async def evaluate(self, context: PolicyContext) -> tuple[str, float, str]:
        if context.tier in (None, "auto"):
            return ("allow", 0.0, "")
        if context.prediction is None:
            return ("rethink", 0.2, "prediction_required")
        if context.tier == "full" and context.prediction.confidence < self.threshold:
            return ("rethink", 0.3, "low_confidence")
        return ("allow", 0.0, "")
