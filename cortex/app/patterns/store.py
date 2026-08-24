"""Pattern store — persist and query session features and pattern cards in Redis DB 6.

Uses the same Redis DB as replay/evals since patterns are derived from that data.
"""

from __future__ import annotations

import json
import logging
import re

import redis
import redis.asyncio as aioredis

from app.patterns.models import Dataset, Experiment, PatternCard, SessionFeatures, graded_only
from app.patterns.lifecycle import apply_lifecycle

logger = logging.getLogger(__name__)

# Redis key layout (all in DB 6 alongside replay/eval data)
_FEATURE_PREFIX = "rp:features:"
_FEATURE_INDEX = "rp:feature_index"  # Sorted set: session_id scored by timestamp
_PATTERN_PREFIX = "rp:pattern:"
_PATTERN_INDEX = "rp:pattern_index"  # Sorted set: pattern_id scored by confidence
_DATASET_PREFIX = "rp:dataset:"
_DATASET_INDEX = "rp:dataset_index"  # Sorted set: dataset_id scored by timestamp
_EXPERIMENT_PREFIX = "rp:experiment:"
_EXPERIMENT_INDEX = "rp:experiment_index"  # Sorted set: experiment_id scored by timestamp

_DEFAULT_TTL = 30 * 86400  # 30 days


# ---------------------------------------------------------------------------
# Session Features
# ---------------------------------------------------------------------------


async def store_features(
    r: aioredis.Redis,
    features: SessionFeatures,
    ttl_days: int = 30,
) -> bool:
    """Store extracted features for a session. Returns True on success.

    Grade-dominant (D9e): a stalled ungraded writer, or any later legacy
    re-extract of a session an upgrade already graded, must not regress
    stored provenance from task_result back to legacy. Guarded by the same
    WATCH/MULTI CAS shape as app.evals.store.store_eval.
    """
    key = f"{_FEATURE_PREFIX}{features.session_id}"
    data = features.model_dump_json()
    ttl = ttl_days * 86400
    incoming_graded = features.outcome_source == "task_result"
    try:
        for _attempt in range(8):
            try:
                async with r.pipeline() as pipe:
                    await pipe.watch(key)
                    existing_raw = await pipe.get(key)
                    if existing_raw and not incoming_graded:
                        try:
                            existing = SessionFeatures.model_validate_json(existing_raw)
                            if existing.outcome_source == "task_result":
                                await pipe.unwatch()
                                return False        # never regress graded -> legacy
                        except Exception:
                            pass
                    pipe.multi()
                    pipe.set(key, data, ex=ttl)
                    pipe.zadd(_FEATURE_INDEX, {features.session_id: features.created_at.timestamp()})
                    await pipe.execute()
                    return True
            except redis.WatchError:
                continue
        logger.warning("Failed to store features for %s: CAS exhausted after 8 attempts", features.session_id)
        return False
    except Exception as e:
        logger.warning("Failed to store features for %s: %s", features.session_id, e)
        return False


async def get_all_features(
    r: aioredis.Redis,
    limit: int = 500,
) -> list[SessionFeatures]:
    """Load all cached session features, most recent first."""
    try:
        session_ids = await r.zrevrange(_FEATURE_INDEX, 0, limit - 1)
        if not session_ids:
            return []

        features: list[SessionFeatures] = []
        for sid in session_ids:
            key = f"{_FEATURE_PREFIX}{sid}"
            raw = await r.get(key)
            if raw:
                try:
                    features.append(SessionFeatures.model_validate_json(raw))
                except Exception:
                    continue
        return features
    except Exception as e:
        logger.warning("Failed to load features: %s", e)
        return []


async def get_feature_count(r: aioredis.Redis) -> int:
    """Return the number of cached session features."""
    try:
        return await r.zcard(_FEATURE_INDEX)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Pattern Cards
# ---------------------------------------------------------------------------


async def store_patterns(
    r: aioredis.Redis,
    patterns: list[PatternCard],
    ttl_days: int = 30,
) -> int:
    """Store pattern cards. Returns count stored."""
    stored = 0
    for p in patterns:
        try:
            key = f"{_PATTERN_PREFIX}{p.id}"
            data = p.model_dump_json()
            await r.set(key, data, ex=ttl_days * 86400)
            await r.zadd(_PATTERN_INDEX, {p.id: p.confidence})
            stored += 1
        except Exception as e:
            logger.warning("Failed to store pattern %s: %s", p.id, e)
    return stored


async def get_patterns(
    r: aioredis.Redis,
    limit: int = 50,
) -> list[PatternCard]:
    """Get all patterns sorted by confidence (highest first)."""
    try:
        pattern_ids = await r.zrevrange(_PATTERN_INDEX, 0, limit - 1)
        if not pattern_ids:
            return []

        patterns: list[PatternCard] = []
        for pid in pattern_ids:
            key = f"{_PATTERN_PREFIX}{pid}"
            raw = await r.get(key)
            if raw:
                try:
                    patterns.append(PatternCard.model_validate_json(raw))
                except Exception:
                    continue
        return patterns
    except Exception as e:
        logger.warning("Failed to load patterns: %s", e)
        return []


async def promote_all_patterns(r: aioredis.Redis) -> int:
    """Load all patterns, run lifecycle evaluation, store back. Returns count updated."""
    try:
        all_patterns = await get_patterns(r, limit=500)
        if not all_patterns:
            return 0

        updated = apply_lifecycle(all_patterns)
        stored = await store_patterns(r, updated)
        logger.info("Lifecycle pass: %d patterns evaluated, %d stored", len(all_patterns), stored)
        return stored
    except Exception as e:
        logger.warning("Failed to run lifecycle: %s", e)
        return 0


async def get_relevant_patterns(
    r: aioredis.Redis,
    goal: str = "",
    files: list[str] | None = None,
    limit: int = 5,
    exclude_agent: str = "",
) -> list[PatternCard]:
    """Get patterns relevant to a goal and/or file list.

    Loads all patterns sorted by confidence, then boosts score if
    tags/description match goal keywords or file paths overlap.

    Args:
        exclude_agent: If set, only return patterns from OTHER agents'
            sessions (cross-agent learning). Patterns with no source_agent
            are included since they could be from any agent.
    """
    try:
        all_patterns = await get_patterns(r, limit=100)
        if not all_patterns:
            return []

        # Only briefing-eligible patterns: procedural/risk at trial+ stage
        _briefing_categories = ("procedural", "risk")
        _briefing_stages = ("trial", "validated")
        all_patterns = [
            p for p in all_patterns
            if p.category in _briefing_categories and p.stage in _briefing_stages
        ]
        if not all_patterns:
            return []

        # Filter by agent if cross-agent learning requested
        if exclude_agent:
            all_patterns = [
                p for p in all_patterns
                if p.source_agent and p.source_agent != exclude_agent
            ]
            if not all_patterns:
                return []

        goal_words = set(goal.lower().split()) if goal else set()
        file_set = set(files) if files else set()

        scored: list[tuple[float, PatternCard]] = []
        for p in all_patterns:
            score = p.confidence

            # Boost for keyword match in description or tags
            if goal_words:
                desc_words = set(p.description.lower().split())
                tag_words = set(t.lower() for t in p.tags)
                overlap = goal_words & (desc_words | tag_words)
                if overlap:
                    score += 0.1 * len(overlap)

            # Boost for file path match in tags
            if file_set:
                pattern_files = set(p.tags)  # files often stored as tags
                if file_set & pattern_files:
                    score += 0.2

            scored.append((score, p))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in scored[:limit]]

    except Exception as e:
        logger.warning("Failed to get relevant patterns: %s", e)
        return []


async def get_observed_patterns(
    r: aioredis.Redis,
    *,
    agent_id: str,
    goal: str = "",
    limit: int = 1,
) -> list[PatternCard]:
    """The caller's OWN candidate/observed patterns — descriptive, UNVALIDATED — for
    the N=1 briefing surface. Distinct from get_relevant_patterns (trial+ only).
    Backend errors degrade to [] (same contract as get_relevant_patterns)."""
    try:
        patterns = await get_patterns(r, limit=100)  # reuse the same loader
        observed_stages = ("candidate", "observed")
        briefing_categories = ("procedural", "risk")
        mine = [
            p for p in patterns
            if p.category in briefing_categories
            and p.stage in observed_stages
            and p.source_agent == agent_id
        ]
        mine.sort(key=lambda p: (p.confidence, p.evidence_count), reverse=True)
        return mine[:limit]
    except Exception:  # noqa: BLE001 — degrade to empty, never fail the briefing
        return []


# ---------------------------------------------------------------------------
# Feedback Loop: tip tracking
# ---------------------------------------------------------------------------

_TIP_LOG_PREFIX = "rp:tip_log:"  # rp:tip_log:{session_id} → JSON list of pattern IDs


async def record_tip_shown(
    r: aioredis.Redis, session_id: str, pattern_ids: list[str], group: str = "treatment"
) -> int:
    """Record that patterns were shown to a session via briefing.

    This is the input side of the feedback loop. We store which tips
    each session received so we can later compare outcomes.

    Args:
        group: A/B test group — "treatment" (tips shown) or "control" (tips withheld).
    """
    if not pattern_ids:
        return 0

    key = f"{_TIP_LOG_PREFIX}{session_id}"
    data = json.dumps({"pattern_ids": pattern_ids, "group": group})
    await r.set(key, data, ex=_DEFAULT_TTL)

    # NOTE (outcome truth, 2026-08-23, round-6 finding 3): this used to also
    # rewrite the session's SessionFeatures record (tips_shown field) here.
    # DELETED: nothing reads SessionFeatures.tips_shown (repo-wide search
    # confirms only the field def and this writer touched it), and the
    # rewrite read a whole legacy features object and wrote it back --
    # clobbering a concurrent graded write even under XX+KEEPTTL, bypassing
    # store_features' grade-dominance guard (D9e).

    # Increment times_shown on each pattern. xx=True, keepttl=True (D11):
    # this is bookkeeping, not a reason to refresh a card's 30-day life or
    # resurrect one that expired between the GET and this SET.
    for pid in pattern_ids:
        pk = f"{_PATTERN_PREFIX}{pid}"
        raw = await r.get(pk)
        if raw:
            try:
                card = PatternCard.model_validate_json(raw)
                card.times_shown += 1
                await r.set(pk, card.model_dump_json(), xx=True, keepttl=True)
            except Exception as e:
                logger.debug("Failed to update pattern %s: %s", pid, e)

    return len(pattern_ids)


async def _load_tip_groups(r: aioredis.Redis) -> dict[str, dict]:
    """Load tip log records and parse group assignments.

    Returns a dict mapping session_id -> {"pattern_ids": [...], "group": "treatment"|"control"}.
    Handles both old format (plain list) and new format (dict with group).
    """
    tip_logs: dict[str, dict] = {}
    cursor = 0
    while True:
        cursor, batch = await r.scan(cursor, match=f"{_TIP_LOG_PREFIX}*", count=100)
        for key in batch:
            sid = key.removeprefix(_TIP_LOG_PREFIX) if isinstance(key, str) else key.decode().removeprefix(_TIP_LOG_PREFIX)
            raw = await r.get(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    # Old format: plain list of pattern IDs (pre-A/B)
                    tip_logs[sid] = {"pattern_ids": data, "group": "treatment"}
                elif isinstance(data, dict):
                    tip_logs[sid] = data
            except (json.JSONDecodeError, TypeError):
                continue
        if cursor == 0:
            break
    return tip_logs


async def compute_tip_effectiveness(
    r: aioredis.Redis,
    briefing_to_session: dict[str, str] | None = None,
) -> list[dict]:
    """Measure how tips affect session outcomes.

    For each pattern, compares:
    - Success rate of sessions in treatment group (tips shown)
    - Success rate of sessions in control group (tips withheld)
    - Success rate of sessions that didn't receive the tip at all

    Returns patterns sorted by tip_lift (biggest improvement first).

    SP1b §11: tips are recorded under the briefing's server-minted briefing_id
    (strategy_tips_section), while SessionFeatures are keyed by session_id.
    `briefing_to_session` (built from Bridge's stored briefing_id field, see
    patterns/api.py) re-keys tip logs briefing_id -> session_id so the join
    below closes. None -> no remap (unchanged behavior).
    """
    try:
        # Load all features -- rates below may only count graded evidence.
        features = await get_all_features(r, limit=500)
        features = graded_only(features)
        if len(features) < 5:
            return []

        # Load all patterns
        patterns = await get_patterns(r, limit=100)
        if not patterns:
            return []

        # Load A/B group assignments from tip logs
        tip_groups = await _load_tip_groups(r)

        # SP1b §11 reconciliation: re-key briefing_id -> session_id. Logs already
        # keyed by a session_id (POST /patterns/tip-shown) aren't in the map and
        # pass through unchanged. briefing_id (uuid4 hex, 32 chars) and session_id
        # (uuid4[:12]) never collide -- in the common case.
        #
        # Collision guard (T34 review): it IS possible for a briefing-keyed
        # entry to remap onto a session_id that already has its own authentic
        # session-keyed entry (e.g. a stale/duplicate briefing_id, or a test
        # harness recording both). `_build_briefing_map` can't detect this --
        # it only sees Bridge's session list, not the tip log keys -- so the
        # remap loop is the only place that can. A naive last-write-wins
        # remap would let processing order (Redis SCAN order is unspecified)
        # silently decide which log survives. Instead: an authentic
        # session-keyed entry always wins over a remapped one.
        if briefing_to_session:
            remapped: dict[str, dict] = {}
            for key, log in tip_groups.items():
                new_key = briefing_to_session.get(key)
                if new_key is None:
                    # Not a briefing_id in the map -- already session-keyed
                    # (or an unmapped key); keep as-is.
                    remapped[key] = log
                elif new_key not in tip_groups:
                    # Remap briefing_id -> session_id; safe, no authentic
                    # session-keyed entry exists at that session_id.
                    remapped[new_key] = log
                else:
                    logger.warning(
                        "tip log collision: briefing-keyed entry %r remaps to "
                        "%r, but an authentic session-keyed entry already "
                        "exists there -- keeping the session-keyed entry",
                        key, new_key,
                    )
            tip_groups = remapped

        results = []
        for pattern in patterns:
            pid = pattern.id

            # Classify sessions into treatment, control, and no-tip groups
            treatment = []
            control = []
            no_tip = []

            for f in features:
                log = tip_groups.get(f.session_id)
                if log and pid in log.get("pattern_ids", []):
                    if log.get("group") == "control":
                        control.append(f)
                    else:
                        treatment.append(f)
                else:
                    no_tip.append(f)

            with_tip = treatment  # backward compat: "with_tip" = treatment group
            without_tip = control + no_tip

            if not with_tip or not without_tip:
                continue

            success_with = sum(1 for f in with_tip if f.outcome == "success")
            success_without = sum(1 for f in without_tip if f.outcome == "success")

            rate_with = success_with / len(with_tip) if with_tip else 0
            rate_without = success_without / len(without_tip) if without_tip else 0
            tip_lift = rate_with - rate_without

            # Update the pattern card with feedback data
            pattern.sessions_with_tip = len(with_tip)
            pattern.success_with_tip = success_with
            pattern.success_without_tip = success_without
            pattern.tip_lift = tip_lift

            # Persist updated stats. xx=True, keepttl=True (D11): the
            # dashboard calls GET /patterns/effectiveness on load, so a bare
            # ex=_DEFAULT_TTL here gave fabricated-era cards a fresh 30 days
            # per visit; xx also keeps this update-only so a card that
            # expired between the GET and this SET stays gone.
            pk = f"{_PATTERN_PREFIX}{pid}"
            await r.set(pk, pattern.model_dump_json(), xx=True, keepttl=True)

            entry = {
                "id": pid,
                "description": pattern.description,
                "recommendation": pattern.recommendation,
                "times_shown": pattern.times_shown,
                "sessions_with_tip": len(with_tip),
                "sessions_without_tip": len(without_tip),
                "success_rate_with_tip": round(rate_with, 3),
                "success_rate_without_tip": round(rate_without, 3),
                "tip_lift": round(tip_lift, 3),
                "verdict": "effective" if tip_lift > 0.05 else ("neutral" if tip_lift > -0.05 else "counterproductive"),
            }

            # A/B breakdown (only if we have control group data)
            if control:
                control_success = sum(1 for f in control if f.outcome == "success")
                control_rate = control_success / len(control)
                ab_lift = rate_with - control_rate
                entry["ab_test"] = {
                    "treatment_sessions": len(treatment),
                    "control_sessions": len(control),
                    "treatment_success_rate": round(rate_with, 3),
                    "control_success_rate": round(control_rate, 3),
                    "ab_lift": round(ab_lift, 3),
                }

            results.append(entry)

        results.sort(key=lambda x: x["tip_lift"], reverse=True)
        return results

    except Exception as e:
        logger.warning("Failed to compute tip effectiveness: %s", e)
        return []


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


async def store_dataset(r: aioredis.Redis, dataset: Dataset, ttl_days: int = 30) -> bool:
    """Store a dataset. Returns True on success."""
    try:
        key = f"{_DATASET_PREFIX}{dataset.id}"
        await r.set(key, dataset.model_dump_json(), ex=ttl_days * 86400)
        ts = dataset.created_at.timestamp()
        await r.zadd(_DATASET_INDEX, {dataset.id: ts})
        return True
    except Exception as e:
        logger.warning("Failed to store dataset %s: %s", dataset.id, e)
        return False


async def get_dataset(r: aioredis.Redis, dataset_id: str) -> Dataset | None:
    """Load a single dataset by ID."""
    try:
        raw = await r.get(f"{_DATASET_PREFIX}{dataset_id}")
        if raw:
            return Dataset.model_validate_json(raw)
        return None
    except Exception as e:
        logger.warning("Failed to load dataset %s: %s", dataset_id, e)
        return None


async def list_datasets(r: aioredis.Redis, limit: int = 50) -> list[Dataset]:
    """List all datasets, most recent first."""
    try:
        ids = await r.zrevrange(_DATASET_INDEX, 0, limit - 1)
        datasets: list[Dataset] = []
        for did in ids:
            raw = await r.get(f"{_DATASET_PREFIX}{did}")
            if raw:
                try:
                    datasets.append(Dataset.model_validate_json(raw))
                except Exception:
                    continue
        return datasets
    except Exception as e:
        logger.warning("Failed to list datasets: %s", e)
        return []


async def delete_dataset(r: aioredis.Redis, dataset_id: str) -> bool:
    """Delete a dataset. Returns True if it existed."""
    try:
        key = f"{_DATASET_PREFIX}{dataset_id}"
        removed = await r.delete(key)
        await r.zrem(_DATASET_INDEX, dataset_id)
        return removed > 0
    except Exception as e:
        logger.warning("Failed to delete dataset %s: %s", dataset_id, e)
        return False


async def materialize_dataset(r: aioredis.Redis, dataset: Dataset) -> Dataset:
    """Run filter criteria against all session features and populate session_ids.

    Filters applied in order: date range, agent IDs, goal pattern, outcome.
    Updates the dataset in-place and stores it back to Redis.
    """
    all_features = await get_all_features(r, limit=5000)

    matched: list[str] = []
    success_count = 0
    failure_count = 0
    unknown_count = 0
    total_duration = 0
    duration_count = 0

    goal_re = re.compile(dataset.goal_pattern, re.IGNORECASE) if dataset.goal_pattern else None

    for f in all_features:
        # Date range filter
        if dataset.date_min and f.created_at < dataset.date_min:
            continue
        if dataset.date_max and f.created_at > dataset.date_max:
            continue

        # Agent ID filter (SessionFeatures doesn't have agent_id, skip if filter set but can't match)
        # Note: agent_ids filter would require enriched features; for now we match on tags
        if dataset.agent_ids:
            agent_match = any(aid in f.tags for aid in dataset.agent_ids)
            if not agent_match:
                continue

        # Goal pattern filter (matches against tags since SessionFeatures has no goal field)
        if goal_re:
            tag_str = " ".join(f.tags)
            if not goal_re.search(tag_str):
                continue

        # An outcome-filtered dataset admits only measured task-result
        # provenance. A fabricated legacy "success" is excluded from
        # membership entirely, not relabeled inside the filtered cohort.
        if dataset.outcome_filter and (
            f.outcome_source != "task_result"
            or f.outcome != dataset.outcome_filter
        ):
            continue

        matched.append(f.session_id)
        if f.outcome_source == "task_result" and f.outcome == "success":
            success_count += 1
        elif f.outcome_source == "task_result" and f.outcome == "failure":
            failure_count += 1
        else:
            unknown_count += 1
        if f.duration_ms is not None:
            total_duration += f.duration_ms
            duration_count += 1

    graded_count = success_count + failure_count
    dataset.session_ids = matched
    dataset.session_count = len(matched)
    dataset.metrics_summary = {
        "success_count": success_count,
        "failure_count": failure_count,
        "unknown_count": unknown_count,
        "success_rate": (
            round(success_count / graded_count, 3) if graded_count else None
        ),
        "avg_duration_ms": (
            round(total_duration / duration_count) if duration_count else 0
        ),
    }

    await store_dataset(r, dataset)
    return dataset


async def get_dataset_features(
    r: aioredis.Redis, dataset: Dataset
) -> list[SessionFeatures]:
    """Load SessionFeatures for all sessions in a dataset."""
    features: list[SessionFeatures] = []
    for sid in dataset.session_ids:
        raw = await r.get(f"{_FEATURE_PREFIX}{sid}")
        if raw:
            try:
                features.append(SessionFeatures.model_validate_json(raw))
            except Exception:
                continue
    return features


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


async def store_experiment(r: aioredis.Redis, experiment: Experiment, ttl_days: int = 30) -> bool:
    """Store an experiment. Returns True on success."""
    try:
        key = f"{_EXPERIMENT_PREFIX}{experiment.id}"
        await r.set(key, experiment.model_dump_json(), ex=ttl_days * 86400)
        ts = experiment.created_at.timestamp()
        await r.zadd(_EXPERIMENT_INDEX, {experiment.id: ts})
        return True
    except Exception as e:
        logger.warning("Failed to store experiment %s: %s", experiment.id, e)
        return False


async def get_experiment(r: aioredis.Redis, experiment_id: str) -> Experiment | None:
    """Load a single experiment by ID."""
    try:
        raw = await r.get(f"{_EXPERIMENT_PREFIX}{experiment_id}")
        if raw:
            return Experiment.model_validate_json(raw)
        return None
    except Exception as e:
        logger.warning("Failed to load experiment %s: %s", experiment_id, e)
        return None


async def list_experiments(r: aioredis.Redis, limit: int = 50) -> list[Experiment]:
    """List all experiments, most recent first."""
    try:
        ids = await r.zrevrange(_EXPERIMENT_INDEX, 0, limit - 1)
        experiments: list[Experiment] = []
        for eid in ids:
            raw = await r.get(f"{_EXPERIMENT_PREFIX}{eid}")
            if raw:
                try:
                    experiments.append(Experiment.model_validate_json(raw))
                except Exception:
                    continue
        return experiments
    except Exception as e:
        logger.warning("Failed to list experiments: %s", e)
        return []


async def delete_experiment(r: aioredis.Redis, experiment_id: str) -> bool:
    """Delete an experiment. Returns True if it existed."""
    try:
        key = f"{_EXPERIMENT_PREFIX}{experiment_id}"
        removed = await r.delete(key)
        await r.zrem(_EXPERIMENT_INDEX, experiment_id)
        return removed > 0
    except Exception as e:
        logger.warning("Failed to delete experiment %s: %s", experiment_id, e)
        return False
