"""Pattern analyzer — discovers strategy patterns from session features.

No ML, no LLM. Just counting and conditional probabilities.
Compares success rates across cohorts to find what works.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from datetime import datetime, timezone

import redis.asyncio as aioredis

from app.patterns.models import PatternCard, SessionFeatures, graded_only
from app.patterns.store import get_all_features, get_patterns

logger = logging.getLogger(__name__)


def _confidence(effect_size: float, evidence_count: int) -> float:
    """Compute confidence from effect size and sample size.

    Formula: effect_size * 2 * min(evidence_count / 20, 1.0), clamped 0.05-0.99
    """
    sample_factor = min(evidence_count / 20, 1.0)
    raw = abs(effect_size) * 2 * sample_factor
    return max(0.05, min(0.99, raw))


def _pattern_id(pattern_type: str, key: str) -> str:
    """Generate a stable pattern ID from type and key."""
    h = hashlib.sha256(f"{pattern_type}:{key}".encode()).hexdigest()[:12]
    return f"pat_{pattern_type}_{h}"


def _success_rate(features: list[SessionFeatures]) -> float | None:
    """Compute success rate over graded features only. None if nothing graded."""
    graded = graded_only(features)
    if not graded:
        return None
    successes = sum(1 for f in graded if f.outcome == "success")
    return successes / len(graded)


# ---------------------------------------------------------------------------
# Pattern Detectors
# ---------------------------------------------------------------------------


def _detect_memory_first(
    all_features: list[SessionFeatures],
    baseline_rate: float,
) -> PatternCard | None:
    """Compare success rate of sessions that read memory before claims vs without."""
    memory_first: list[SessionFeatures] = []
    no_memory_first: list[SessionFeatures] = []

    for f in all_features:
        # Check if memory_read appears before any claim/lease in tool_sequence
        has_memory_before_claim = False
        for event_type in f.tool_sequence:
            if event_type == "memory_read":
                has_memory_before_claim = True
                break
            if event_type in ("claim", "lease", "file_edit", "file_write"):
                break

        if has_memory_before_claim:
            memory_first.append(f)
        elif f.memory_reads > 0 or f.claim_count > 0:
            no_memory_first.append(f)

    if len(memory_first) < 2 or len(no_memory_first) < 2:
        return None

    rate_with = _success_rate(memory_first)
    rate_without = _success_rate(no_memory_first)
    effect = rate_with - rate_without
    evidence = len(memory_first) + len(no_memory_first)

    if abs(effect) < 0.05:
        return None

    lift = (rate_with / rate_without) if rate_without > 0 else 1.0

    direction = "higher" if effect > 0 else "lower"
    return PatternCard(
        id=_pattern_id("memory_first", "default"),
        description=f"Sessions that read memory before file operations have {direction} success rate ({rate_with:.0%} vs {rate_without:.0%})",
        pattern_type="memory_first",
        category="procedural",
        confidence=_confidence(effect, evidence),
        evidence_count=evidence,
        baseline_rate=baseline_rate,
        pattern_rate=rate_with,
        lift=round(lift, 2),
        recommendation="Read memory before starting file edits" if effect > 0 else "Memory-first approach may not help in this codebase",
        tags=["memory", "strategy"],
    )


def _detect_file_hotspot(
    all_features: list[SessionFeatures],
    baseline_rate: float,
) -> list[PatternCard]:
    """Find files that correlate with success or failure."""
    file_sessions: dict[str, list[SessionFeatures]] = {}
    for f in all_features:
        for path in f.file_paths:
            file_sessions.setdefault(path, []).append(f)

    patterns: list[PatternCard] = []
    for path, sessions in file_sessions.items():
        if len(sessions) < 3:
            continue

        rate = _success_rate(sessions)
        effect = rate - baseline_rate

        if abs(effect) < 0.1:
            continue

        lift = (rate / baseline_rate) if baseline_rate > 0 else 1.0
        direction = "success" if effect > 0 else "failure"

        # Extract module from path (first directory component)
        parts = path.strip("/").split("/")
        scope_module = parts[1] if len(parts) > 1 else parts[0] if parts else ""

        patterns.append(PatternCard(
            id=_pattern_id("file_hotspot", path),
            description=f"File '{path}' correlates with {direction} ({rate:.0%} vs {baseline_rate:.0%} baseline)",
            pattern_type="file_hotspot",
            category="risk",
            confidence=_confidence(effect, len(sessions)),
            evidence_count=len(sessions),
            baseline_rate=baseline_rate,
            pattern_rate=rate,
            lift=round(lift, 2),
            recommendation=f"Exercise caution when editing '{path}'" if effect < 0 else f"'{path}' edits tend to succeed",
            tags=[path, "file"],
            scope_module=scope_module,
        ))

    return patterns


def _detect_tool_sequence(
    all_features: list[SessionFeatures],
    baseline_rate: float,
) -> list[PatternCard]:
    """Find 2-gram tool sequences that correlate with outcomes."""
    bigram_sessions: dict[str, list[SessionFeatures]] = {}

    for f in all_features:
        seen_bigrams: set[str] = set()
        for i in range(len(f.tool_sequence) - 1):
            bigram = f"{f.tool_sequence[i]}→{f.tool_sequence[i + 1]}"
            if bigram not in seen_bigrams:
                seen_bigrams.add(bigram)
                bigram_sessions.setdefault(bigram, []).append(f)

    patterns: list[PatternCard] = []
    for bigram, sessions in bigram_sessions.items():
        if len(sessions) < 3:
            continue

        rate = _success_rate(sessions)
        effect = rate - baseline_rate

        if abs(effect) < 0.1:
            continue

        lift = (rate / baseline_rate) if baseline_rate > 0 else 1.0
        direction = "success" if effect > 0 else "failure"

        patterns.append(PatternCard(
            id=_pattern_id("tool_sequence", bigram),
            description=f"Sequence '{bigram}' correlates with {direction} ({rate:.0%} vs {baseline_rate:.0%} baseline)",
            pattern_type="tool_sequence",
            category="procedural",
            confidence=_confidence(effect, len(sessions)),
            evidence_count=len(sessions),
            baseline_rate=baseline_rate,
            pattern_rate=rate,
            lift=round(lift, 2),
            recommendation=f"Consider using the '{bigram}' pattern" if effect > 0 else f"Avoid the '{bigram}' sequence when possible",
            tags=["tool_sequence", bigram],
        ))

    return patterns


def _detect_memory_usage(
    all_features: list[SessionFeatures],
    baseline_rate: float,
) -> PatternCard | None:
    """Compare success rate of above-median vs below-median memory usage."""
    features_with_memory = [f for f in all_features if f.memory_reads + f.memory_writes > 0]
    if len(features_with_memory) < 4:
        return None

    total_usage = sorted(f.memory_reads + f.memory_writes for f in all_features)
    median = total_usage[len(total_usage) // 2]

    if median == 0:
        return None

    above = [f for f in all_features if (f.memory_reads + f.memory_writes) > median]
    below = [f for f in all_features if (f.memory_reads + f.memory_writes) <= median]

    if len(above) < 2 or len(below) < 2:
        return None

    rate_above = _success_rate(above)
    rate_below = _success_rate(below)
    effect = rate_above - rate_below
    evidence = len(above) + len(below)

    if abs(effect) < 0.05:
        return None

    lift = (rate_above / rate_below) if rate_below > 0 else 1.0
    direction = "higher" if effect > 0 else "lower"

    return PatternCard(
        id=_pattern_id("memory_usage", "median_split"),
        description=f"Sessions with above-median memory usage have {direction} success rate ({rate_above:.0%} vs {rate_below:.0%})",
        pattern_type="memory_usage",
        category="behavioral",
        confidence=_confidence(effect, evidence),
        evidence_count=evidence,
        baseline_rate=baseline_rate,
        pattern_rate=rate_above,
        lift=round(lift, 2),
        recommendation="Use memory more actively" if effect > 0 else "Memory overuse may indicate struggling sessions",
        tags=["memory", "usage"],
    )


def _detect_duration(
    all_features: list[SessionFeatures],
    baseline_rate: float,
) -> list[PatternCard]:
    """Bucket sessions by duration, compare success rates."""
    with_duration = [f for f in all_features if f.duration_ms is not None and f.duration_ms > 0]
    if len(with_duration) < 4:
        return []

    # Bucket into short/medium/long by terciles
    durations = sorted(f.duration_ms for f in with_duration)
    t1 = durations[len(durations) // 3]
    t2 = durations[2 * len(durations) // 3]

    buckets = {
        "short": [f for f in with_duration if f.duration_ms <= t1],
        "medium": [f for f in with_duration if t1 < f.duration_ms <= t2],
        "long": [f for f in with_duration if f.duration_ms > t2],
    }

    patterns: list[PatternCard] = []
    for label, sessions in buckets.items():
        if len(sessions) < 2:
            continue

        rate = _success_rate(sessions)
        effect = rate - baseline_rate

        if abs(effect) < 0.1:
            continue

        lift = (rate / baseline_rate) if baseline_rate > 0 else 1.0
        direction = "success" if effect > 0 else "failure"

        patterns.append(PatternCard(
            id=_pattern_id("duration", label),
            description=f"{label.title()} sessions correlate with {direction} ({rate:.0%} vs {baseline_rate:.0%} baseline)",
            pattern_type="duration",
            category="behavioral",
            confidence=_confidence(effect, len(sessions)),
            evidence_count=len(sessions),
            baseline_rate=baseline_rate,
            pattern_rate=rate,
            lift=round(lift, 2),
            recommendation=f"{label.title()}-duration sessions tend to {'succeed' if effect > 0 else 'fail'}",
            tags=["duration", label],
        ))

    return patterns


def _detect_failure_mode(
    all_features: list[SessionFeatures],
    baseline_rate: float,
) -> list[PatternCard]:
    """Find common event patterns that appear before failures."""
    failures = [f for f in all_features if f.outcome == "failure"]
    successes = [f for f in all_features if f.outcome == "success"]

    if len(failures) < 2 or len(successes) < 2:
        return []

    # Count event types in failures vs successes
    failure_type_freq: Counter[str] = Counter()
    success_type_freq: Counter[str] = Counter()

    for f in failures:
        for et in set(f.tool_type_counts.keys()):
            failure_type_freq[et] += 1

    for f in successes:
        for et in set(f.tool_type_counts.keys()):
            success_type_freq[et] += 1

    patterns: list[PatternCard] = []
    for event_type, fail_count in failure_type_freq.items():
        fail_rate = fail_count / len(failures)
        success_count = success_type_freq.get(event_type, 0)
        succ_rate = success_count / len(successes) if successes else 0

        # Look for event types much more common in failures
        effect = fail_rate - succ_rate
        if effect < 0.2:
            continue

        evidence = fail_count + success_count
        if evidence < 3:
            continue

        patterns.append(PatternCard(
            id=_pattern_id("failure_mode", event_type),
            description=f"'{event_type}' appears in {fail_rate:.0%} of failures but only {succ_rate:.0%} of successes",
            pattern_type="failure_mode",
            category="risk",
            confidence=_confidence(effect, evidence),
            evidence_count=evidence,
            baseline_rate=succ_rate,
            pattern_rate=fail_rate,
            lift=round((fail_rate / succ_rate) if succ_rate > 0 else 2.0, 2),
            recommendation=f"Watch for '{event_type}' events — they correlate with failure",
            tags=["failure", event_type],
        ))

    return patterns


# ---------------------------------------------------------------------------
# Main Analyzer
# ---------------------------------------------------------------------------


async def analyze_patterns(
    replay_redis: aioredis.Redis,
    min_sessions: int = 5,
) -> list[PatternCard]:
    """Run all pattern detectors on cached session features.

    Args:
        replay_redis: Redis client for DB 6.
        min_sessions: Minimum sessions required before analysis runs.

    Returns:
        List of PatternCards sorted by confidence descending.
    """
    all_features = await get_all_features(replay_redis)
    graded_features = graded_only(all_features)

    if len(graded_features) < min_sessions:
        logger.debug(
            "Not enough graded sessions for pattern analysis (%d < %d)",
            len(graded_features), min_sessions,
        )
        return []

    baseline_rate = _success_rate(graded_features)
    if baseline_rate is None:
        return []

    patterns: list[PatternCard] = []

    # Run each detector
    p = _detect_memory_first(graded_features, baseline_rate)
    if p:
        patterns.append(p)

    patterns.extend(_detect_file_hotspot(graded_features, baseline_rate))
    patterns.extend(_detect_tool_sequence(graded_features, baseline_rate))

    p = _detect_memory_usage(graded_features, baseline_rate)
    if p:
        patterns.append(p)

    patterns.extend(_detect_duration(graded_features, baseline_rate))
    patterns.extend(_detect_failure_mode(graded_features, baseline_rate))

    # Set last_matched_at on all newly discovered patterns
    now = datetime.now(timezone.utc)
    for p in patterns:
        p.last_matched_at = now

    # Merge with existing stored patterns to preserve lifecycle state
    existing = await get_patterns(replay_redis, limit=500)
    existing_map = {p.id: p for p in existing}

    for i, p in enumerate(patterns):
        if p.id in existing_map:
            old = existing_map[p.id]
            # Preserve lifecycle fields from existing pattern
            p.stage = old.stage
            p.category = old.category if old.category != "procedural" else p.category  # keep explicit category
            p.promoted_at = old.promoted_at
            p.quarantine_reason = old.quarantine_reason
            p.quarantined_at = old.quarantined_at
            p.scope_goal_type = old.scope_goal_type or p.scope_goal_type
            p.scope_module = old.scope_module or p.scope_module
            p.scope_service = old.scope_service or p.scope_service
            # Preserve feedback loop data
            p.times_shown = old.times_shown
            p.sessions_with_tip = old.sessions_with_tip
            p.success_with_tip = old.success_with_tip
            p.success_without_tip = old.success_without_tip
            p.tip_lift = old.tip_lift
            p.source_agent = old.source_agent or p.source_agent

    # Sort by confidence descending
    patterns.sort(key=lambda p: p.confidence, reverse=True)

    logger.info("Pattern analysis found %d patterns from %d graded sessions", len(patterns), len(graded_features))
    return patterns
