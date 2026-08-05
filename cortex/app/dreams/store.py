"""The dedicated write path for dreams. Never /memory/learn, never VectorClient.upsert.

Three reasons, each verified against the code:
  1. /memory/learn runs contradiction detection, which auto-supersedes up to 4 live
     memories at 0.85 cosine and does NOT check confirmed_count — a dream, being a
     high-similarity summary of a neighbourhood, is the likeliest thing to trip it.
  2. VectorClient.upsert derives the point id as uuid5(text) and merges lifecycle
     from whatever sits there, so a dream can inherit another point's status, and a
     "continuously updated" profile would instead accumulate live near-duplicates.
  3. upsert nests unrecognised metadata while scroll/filter read top-level, so a
     nested source="dream" is invisible to the filter that prevents dream-of-a-dream.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.dreams.select import Candidate
from app.dreams.synthesize import Insight

DREAM_NS = uuid.UUID("6f1d0c9a-7c1e-5b2a-9f43-0d5e8a2b4c71")


def dream_point_id(cluster_key: str, index: int = 0) -> str:
    """`index` distinguishes multiple insights synthesized from the SAME
    cluster (synthesize() may return up to 3) without folding the index into
    `cluster_key` itself — index==0 (the default) reproduces the original
    id exactly, so every existing caller/test is unaffected."""
    seed = cluster_key if index == 0 else f"{cluster_key}:{index}"
    return str(uuid.uuid5(DREAM_NS, f"dream::{seed}"))


def profile_point_id(member_id: str, workspace_id: str) -> str:
    return str(uuid.uuid5(DREAM_NS, f"profile::{member_id}::{workspace_id}"))


def _uniform(members: list[Candidate], key: str) -> str:
    """The cluster's shared value for `key`. RAISES when they disagree.

    Only legitimate for keys that `select.partition_key` actually makes
    invariant — `workspace_id`, `namespace`, `project`. A cluster is built
    inside one of those buckets, so disagreement means the partitioning broke
    and writing a point that silently claims one tenant's workspace for
    another's memories would be a cross-tenant leak. Failing loud is right.

    For anything else use `_uniform_or_blank`: raising about a key nothing
    ever promised is not a safety check, it is a crash.
    """
    values = {str(m.payload.get(key) or "") for m in members}
    if len(values) > 1:
        raise ValueError(f"cluster is not homogeneous in {key}: {sorted(values)}")
    return values.pop() if values else ""


def _uniform_or_blank(members: list[Candidate], key: str) -> str:
    """The cluster's shared value for `key`, or "" when they disagree.

    For keys that are NOT partition invariants. `domain` is the live case, and
    it took down the whole pass: clusters are grouped by cosine similarity and
    partitioned by (workspace_id, namespace, project) — `domain` is in neither,
    so a cluster is free to span several, and on the production store one
    routinely does. A real cluster carried seven spellings of a single concept
    (`ansible`, `ansible-playbooks`, `automation-playbooks`, `automation-portal`,
    `automation_playbooks`, `automationportal`, `infrastructure-automation`),
    which is exactly the label fragmentation `memory_agent`'s cluster-coherence
    pass exists to reconcile and exactly what the clusterer is supposed to see
    past.

    Passing that through `_uniform` raised, `run_one_unit` recorded
    status="error", and the tick wrote nothing — AFTER a successful ~32s
    synthesis that had already produced usable insights. The `or "general"` at
    the call site is the proof this was never the intent: it can only mean the
    author expected a falsy return for "no single answer", which is what this
    function does and what `_uniform` does only for the empty case.

    Deliberately mirrors `task.py::_uniform_or_blank`, which reached the same
    conclusion for profile groups. Not shared between the two modules: task.py's
    operates on profile groups keyed by (member_id, workspace_id) and store.py's
    on clusters, and collapsing them would tie two different grouping contracts
    to one helper for the sake of four identical lines.
    """
    values = {str(m.payload.get(key) or "") for m in members}
    return values.pop() if len(values) == 1 else ""


def build_dream_payload(
    insight: Insight, members: list[Candidate], *, cluster_key: str, run_id: str
) -> dict:
    workspace_id = _uniform(members, "workspace_id")
    namespace = _uniform(members, "namespace")
    project = _uniform(members, "project")
    now = datetime.now(timezone.utc).isoformat()
    member_ids = sorted({str(m.payload.get("member_id") or "") for m in members} - {""})
    return {
        "text": insight.content,
        "source": "dream",
        "dream_run_id": run_id,
        "dream_cluster_key": cluster_key,
        # Three provenance facts, deliberately distinct — a reader who conflates
        # them gets a different (and wrong) answer to "what does this dream
        # cover?":
        #
        #   dreamed_from        the ids the MODEL CITED. Unchanged in meaning by
        #                       cluster sampling — it was already a subset of the
        #                       cluster, since a model cites only what it used.
        #   dream_sampled_count how many episodes the model was SHOWN.
        #   dream_cluster_size  how many the cluster HAS, i.e. what this dream is
        #                       about and what mark_consolidated recorded.
        #
        # Sampling (synthesize.sample_cluster, capped by
        # DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS) changes only the middle one.
        # Without it the stored point would imply all N members were read, which
        # is the one thing capping must not be allowed to misreport: "summarised
        # from 5 of 23" and "from 23 of 23" have to be distinguishable here.
        #
        # Two integers, and deliberately NO derived `dream_sampled: bool`. The
        # boolean is `sampled_count < cluster_size` — recomputable by anyone, and
        # a stored copy is a second source of truth that can disagree with the
        # numbers the moment one writer sets one field and not the other. Two
        # numbers also answer the question the boolean cannot: how much less.
        #
        # `insight.sample_size` is 0 only for an Insight built by hand rather
        # than by parse_insights; falling back to the cluster size there states
        # the pre-sampling reality (no sampling happened) rather than writing a 0
        # that reads as "the model saw nothing".
        "dreamed_from": list(insight.source_ids),
        "dream_cluster_size": len(members),
        "dream_sampled_count": (
            getattr(insight, "sample_size", 0) or len(members)
        ),
        # procedural, NEVER reference: reference means no age decay at all, and an
        # auto-approved unreviewed memory must be able to fade if it stops helping.
        "memory_type": "procedural",
        "status": "active",
        "confirmed_count": 0,
        "contradicted_count": 0,
        "superseded_by": None,
        "timestamp": now,
        "created_at": now,
        "workspace_id": workspace_id,
        "namespace": namespace,
        "project": project or None,
        "member_id": member_ids[0] if len(member_ids) == 1 else None,
        "agent_id": "dream",
        "session_id": None,
        # NOT _uniform: domain is not a partition invariant and a real cluster
        # spans several. See _uniform_or_blank's docstring.
        "domain": _uniform_or_blank(members, "domain") or "general",
        "tags": ["dream"],
        # Recall reads memory_type from the projection, GC from top-level — write
        # both so they can never disagree about this point.
        "metadata": {"memory_type": "procedural", "dream_cluster_key": cluster_key},
    }


async def write_dream(
    vector, insight: Insight, members: list[Candidate], *, cluster_key: str, run_id: str,
    index: int = 0,
) -> str:
    """`index` (default 0, backward compatible) selects which of a cluster's
    up-to-3 insights this call writes — see dream_point_id. It feeds ONLY the
    point id: `cluster_key` reaches build_dream_payload UNCHANGED, so the
    stored `dream_cluster_key` / provenance always names the real cluster,
    never a synthetic "key:i" value a caller looking it up later wouldn't
    recognise (fix-round review, dreaming Task 6/7)."""
    payload = build_dream_payload(insight, members, cluster_key=cluster_key, run_id=run_id)
    point_id = dream_point_id(cluster_key, index)
    await vector.upsert_point(point_id, insight.content, payload)
    return point_id
