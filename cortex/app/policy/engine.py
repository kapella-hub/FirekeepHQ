"""Policy engine — evaluates compound rules and returns allow/warn/rethink/block decisions.

The engine runs all enabled PolicyRule instances against a given context
(file_path, agent_id, session_id) and aggregates results into a single
PolicyDecision. Precedence (highest wins): block > rethink > warn > allow.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class PolicyDecision(BaseModel):
    """Aggregate decision from all policy rules."""

    action: Literal["allow", "warn", "rethink", "block"] = "allow"
    reasons: list[str] = []
    risk_score: float = 0.0  # 0-1, clamped aggregate
    signals: dict = {}  # individual signal values keyed by rule name


class PolicyContext(BaseModel):
    """Input context for policy evaluation."""

    file_path: str
    agent_id: str = ""
    session_id: str = ""
    tier: Optional[str] = None  # "auto" | "lightweight" | "full"
    prediction: Optional[object] = None  # app.agent_gateway.models.Prediction


class PolicyRule:
    """Base class for individual policy rules.

    Subclasses must implement evaluate() and set a unique `name`.
    """

    name: str = "base"
    enabled: bool = True

    async def evaluate(self, context: PolicyContext) -> tuple[str, float, str]:
        """Evaluate this rule against a context.

        Returns:
            (action, risk_contribution, reason)
            action: "allow", "warn", or "block"
            risk_contribution: 0.0-1.0 contribution to aggregate risk
            reason: human-readable explanation (empty string if allow)
        """
        return ("allow", 0.0, "")


class PolicyEngine:
    """Runs all registered rules and aggregates into a PolicyDecision."""

    def __init__(self, rules: list[PolicyRule] | None = None):
        self.rules: list[PolicyRule] = rules or []

    def add_rule(self, rule: PolicyRule) -> None:
        self.rules.append(rule)

    def get_rule(self, name: str) -> PolicyRule | None:
        for r in self.rules:
            if r.name == name:
                return r
        return None

    def list_rules(self) -> list[dict]:
        return [{"name": r.name, "enabled": r.enabled} for r in self.rules]

    def toggle_rule(self, name: str) -> bool | None:
        """Toggle a rule's enabled state. Returns new state or None if not found."""
        rule = self.get_rule(name)
        if rule is None:
            return None
        rule.enabled = not rule.enabled
        return rule.enabled

    async def evaluate(self, context: PolicyContext) -> PolicyDecision:
        """Run all enabled rules and return an aggregate decision.

        Precedence (highest wins): block > rethink > warn > allow.
        """
        _ACTION_PRECEDENCE: dict[str, int] = {"allow": 0, "warn": 1, "rethink": 2, "block": 3}
        final_action: Literal["allow", "warn", "rethink", "block"] = "allow"
        reasons: list[str] = []
        total_risk = 0.0
        signals: dict = {}

        for rule in self.rules:
            if not rule.enabled:
                continue

            try:
                action, risk, reason = await rule.evaluate(context)
            except Exception as exc:
                logger.warning("Policy rule %s failed: %s", rule.name, exc)
                # Rule failure should not block work
                signals[rule.name] = {"error": str(exc)}
                continue

            signals[rule.name] = {
                "action": action,
                "risk": risk,
                "reason": reason,
            }

            total_risk += risk

            if reason:
                reasons.append(f"[{rule.name}] {reason}")

            # Escalate by precedence: block > rethink > warn > allow
            if _ACTION_PRECEDENCE.get(action, 0) > _ACTION_PRECEDENCE.get(final_action, 0):
                final_action = action  # type: ignore[assignment]

        return PolicyDecision(
            action=final_action,
            reasons=reasons,
            risk_score=min(total_risk, 1.0),
            signals=signals,
        )
