"""Candidate selection, partitioning and clustering for the Dreaming pass.

PURE. No I/O, no clients, no settings object — every knob is an argument, so the
whole selection policy is unit-testable and reproducible.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# A missing memory_type means "written before the field existed" — on the live
# store that is half the active corpus. rag.py already treats unknown types with
# the episodic-ish fallback half-life, so selection matches that reading.
_EPISODIC = {"episodic", ""}
_EXCLUDED_SOURCES = {"corpus", "dream"}


@dataclass
class Candidate:
    id: str
    text: str
    vector: list[float] = field(default_factory=list)
    payload: dict = field(default_factory=dict)


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_candidate(
    payload: dict,
    *,
    now: datetime,
    min_age_days: int,
    owm_floor: float,
    owm_prior_n: int,
) -> bool:
    if payload.get("status", "active") != "active":
        return False
    if str(payload.get("source", "")) in _EXCLUDED_SOURCES:
        return False
    if str(payload.get("memory_type", "") or "") not in _EPISODIC:
        return False
    try:
        if int(payload.get("confirmed_count", 0) or 0) > 0:
            return False
    except (TypeError, ValueError):
        return False

    ts = _parse_ts(payload.get("timestamp"))
    if ts is None or ts > now - timedelta(days=min_age_days):
        return False

    # OWM as credit signal. Only decisive once there is real evidence (n >= prior):
    # condemned memories don't deserve abstraction, and PROVEN ones already earn
    # their rank — consolidating those would hand their position to a memory with
    # no track record of its own.
    try:
        n = int(payload.get("owm_n") or 0)
        eff = payload.get("owm_efficacy")
        if n >= owm_prior_n and eff is not None:
            eff = float(eff)
            if eff < owm_floor or eff > 0.5:
                return False
    except (TypeError, ValueError):
        pass

    return True


def partition_key(payload: dict) -> tuple[str, str, str]:
    def s(k: str) -> str:
        v = payload.get(k)
        return "" if v is None else str(v)

    return (s("workspace_id"), s("namespace"), s("project"))


def partition(cands: list[Candidate]) -> dict[tuple[str, str, str], list[Candidate]]:
    buckets: dict[tuple[str, str, str], list[Candidate]] = {}
    for c in cands:
        buckets.setdefault(partition_key(c.payload), []).append(c)
    return buckets


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def cluster(
    cands: list[Candidate], *, threshold: float, min_size: int
) -> list[list[Candidate]]:
    """Greedy single-pass clustering. Deterministic: input is sorted by id first,
    so the same store always yields the same clusters (and the same cluster keys,
    which are the dedupe keys)."""
    remaining = sorted(cands, key=lambda c: c.id)
    clusters: list[list[Candidate]] = []
    used: set[str] = set()
    for seed in remaining:
        if seed.id in used:
            continue
        members = [seed]
        used.add(seed.id)
        for other in remaining:
            if other.id in used:
                continue
            if cosine(seed.vector, other.vector) >= threshold:
                members.append(other)
                used.add(other.id)
        if len(members) >= min_size:
            clusters.append(members)
    return clusters


def cluster_key(cluster_members: list[Candidate]) -> str:
    joined = "|".join(sorted(c.id for c in cluster_members))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def select_clusters(
    cands: list[Candidate], *, threshold: float, min_size: int, max_clusters: int
) -> list[list[Candidate]]:
    """Partition FIRST, cluster within each bucket. A cluster that spans
    workspace/namespace/project is not a cluster — search enforces all three as
    hard must-filters and workspace_id is a tenancy boundary."""
    out: list[list[Candidate]] = []
    for key in sorted(partition(cands).keys()):
        for cl in cluster(partition(cands)[key], threshold=threshold, min_size=min_size):
            out.append(cl)
            if len(out) >= max_clusters:
                return out
    return out
