"""Candidate selection, partitioning and clustering for the Dreaming pass.

PURE. No I/O, no clients, no settings object — every knob is an argument, so the
whole selection policy is unit-testable and reproducible.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from collections.abc import Container
from datetime import datetime, timedelta, timezone

# A missing memory_type means "written before the field existed" — on the live
# store that is half the active corpus. rag.py already treats unknown types with
# the episodic-ish fallback half-life, so selection matches that reading.
_EPISODIC = {"episodic", ""}
# "dream_profile" (Task 7 person profiles) belongs here for the same reason
# "dream" does: without it, a profile could enter its own future clustering
# input. Today this exclusion is redundant with two OTHER independent guards
# — profile.build_profile_payload forces memory_type="reference" (excluded by
# the _EPISODIC check below) and task._scope_filter blocks source="dream_profile"
# at the Qdrant level — but is_candidate is the one PURE function whose
# contract callers can rely on in isolation; its own defence must not rest on
# a field a caller could plausibly forget to set correctly upstream.
_EXCLUDED_SOURCES = {"corpus", "dream", "dream_profile"}


@dataclass
class Candidate:
    id: str
    text: str
    vector: list[float] = field(default_factory=list)
    payload: dict = field(default_factory=dict)


def parse_ts(raw: object) -> datetime | None:
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
    memory_id: str = "",
    consolidated: Container[str] = frozenset(),
) -> bool:
    """`memory_id` + `consolidated` implement the design spec's "not already
    consolidated" criterion (final-review I2+I3) while keeping this function
    PURE: the caller reads the ledger (DreamState.consolidated_set) once per
    tick and passes the whole set in. Nothing here touches Redis — that is
    the property the entire module rests on, and a membership test is
    exactly the shape that invites a lookup to creep in.

    Both default to "no ledger", so every pre-existing caller and test keeps
    its previous meaning; only the run path supplies them.
    """
    # Normalize naive datetime to UTC to match parse_ts's tz-aware output.
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if payload.get("status", "active") != "active":
        return False
    # A memory a stored dream already covers is not consolidatable again.
    # Checked early and cheaply: without it, each run re-selects the same
    # first-N clusters forever and later partitions are never reached.
    if memory_id and memory_id in consolidated:
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

    ts = parse_ts(payload.get("timestamp"))
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
    which are the dedupe keys).

    Fix-round performance note (dreaming Task 6/7 review, I7): this is O(n^2)
    in the number of candidates by construction (every seed compares against
    every remaining other), and each comparison used to call the public
    cosine() above, which recomputes BOTH vectors' L2 norms on every call —
    so a seed's own norm was recomputed once per candidate it was compared
    against. Precomputing each candidate's norm ONCE up front and inlining the
    dot-product/norm division here (rather than changing cosine()'s own
    signature, which test_dreams_select.py pins) measured ~2.3x faster on a
    2000-candidate/1024-dim synthetic benchmark (132.5s -> 58.8s on this
    machine). The public cosine() function is untouched — this optimization
    is local to cluster()'s inner loop.
    """
    remaining = sorted(cands, key=lambda c: c.id)
    norms = {c.id: math.sqrt(sum(x * x for x in c.vector)) for c in remaining}

    def _sim(a: Candidate, b: Candidate) -> float:
        va, vb = a.vector, b.vector
        if not va or not vb or len(va) != len(vb):
            return 0.0
        na, nb = norms[a.id], norms[b.id]
        if na == 0.0 or nb == 0.0:
            return 0.0
        dot = sum(x * y for x, y in zip(va, vb))
        return dot / (na * nb)

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
            if _sim(seed, other) >= threshold:
                members.append(other)
                used.add(other.id)
        if len(members) >= min_size:
            clusters.append(members)
    return clusters


def centroid(vectors: list[list[float]]) -> list[float] | None:
    """Component-wise mean of `vectors`, or None when they cannot be averaged
    (empty input, an empty vector, or disagreeing dimensionality). None is the
    caller's cue to fall back to a vector-free ordering — it must never be
    confused with a zero vector, which is a real (if useless) centroid."""
    usable = [v for v in vectors if v]
    if not usable:
        return None
    dim = len(usable[0])
    if dim == 0 or any(len(v) != dim for v in usable):
        return None
    n = float(len(usable))
    return [sum(v[i] for v in usable) / n for i in range(dim)]


def sample_cluster(members: list[Candidate], max_members: int) -> list[Candidate]:
    """The at-most-`max_members` members of `members` actually sent to the LLM.

    This caps the PROMPT, not the cluster. The caller still treats the whole
    cluster as consolidated and still records the whole cluster's size — see
    store.build_dream_payload's `dream_cluster_size`/`dream_sampled_count` and
    DreamState.mark_consolidated. Marking only the sample consolidated would
    leave the remainder as candidates, they would re-cluster, and the pass
    would re-synthesize the same neighbourhood forever: precisely the
    starvation `dreams:consolidated` exists to prevent.

    WHY a cap exists at all is measured, not assumed — see
    DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS in app/config.py for the numbers.
    Short version: with no cap, 19 of 20 real clusters exceeded the synthesis
    budget and wrote nothing, and the same 23-member cluster capped to 6
    produced 3 good insights in 41.9s.

    SELECTION IS BY CENTRALITY, AND THE CENTRALITY IS ACTUALLY COMPUTED. The
    cluster's centroid is the component-wise mean of its members' vectors —
    which this module already holds, so it costs one pass over the members —
    and the sample is the `max_members` nearest it by cosine, ties broken by
    id ascending. A cluster is a threshold neighbourhood around an arbitrary
    greedy SEED (see cluster()), so the seed is not its most representative
    member and "the first k" would be an arbitrary-but-stable choice; the
    centroid is the one cheap thing here that is actually about the content.

    Deterministic in every branch, which the whole module depends on:
    similarity is a pure function of the stored vectors, ties fall back to id,
    and when no centroid can be computed (missing/ragged vectors — see
    centroid()) the fallback is plain id order, which is arbitrary-but-stable
    and is documented as such rather than dressed up as representative. A
    member with an unusable vector among usable ones simply scores 0.0 and
    sinks; it is not excluded, because a cluster must always be able to
    produce a sample.

    The RESULT is returned in id order, not centrality order. Prompt position
    is not a ranking signal and implying one to the model would be a claim
    nothing here supports; id order also matches cluster_key's own convention.

    A cluster at or below the cap is returned UNCHANGED — same members, same
    order, so the prompt is byte-identical to the pre-cap one and no centroid
    is computed at all. `max_members <= 0` means no cap, for a deploy that
    measured its own backend.
    """
    if max_members <= 0 or len(members) <= max_members:
        return list(members)

    c = centroid([m.vector for m in members])
    if c is None:
        return sorted(members, key=lambda m: m.id)[:max_members]

    cn = math.sqrt(sum(x * x for x in c))
    if cn == 0.0:
        return sorted(members, key=lambda m: m.id)[:max_members]

    scored: list[tuple[float, str, int]] = []
    for i, m in enumerate(members):
        v = m.vector
        if not v or len(v) != len(c):
            sim = 0.0
        else:
            mn = math.sqrt(sum(x * x for x in v))
            sim = 0.0 if mn == 0.0 else sum(x * y for x, y in zip(v, c)) / (mn * cn)
        # Negated similarity first => descending by similarity; then id, then
        # the original index as a final tiebreak so two members that are equal
        # in both still order deterministically.
        scored.append((-sim, m.id, i))

    scored.sort()
    chosen = {i for _, _, i in scored[:max_members]}
    return sorted((members[i] for i in chosen), key=lambda m: m.id)


def cluster_key(cluster_members: list[Candidate]) -> str:
    joined = "|".join(sorted(c.id for c in cluster_members))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def select_clusters(
    cands: list[Candidate], *, threshold: float, min_size: int, max_clusters: int
) -> list[list[Candidate]]:
    """Partition FIRST, cluster within each bucket. A cluster that spans
    workspace/namespace/project is not a cluster — search enforces all three as
    hard must-filters and workspace_id is a tenancy boundary."""
    buckets = partition(cands)
    out: list[list[Candidate]] = []
    for key in sorted(buckets.keys()):
        for cl in cluster(buckets[key], threshold=threshold, min_size=min_size):
            out.append(cl)
            if len(out) >= max_clusters:
                return out
    return out
