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


def dream_point_id(cluster_key: str) -> str:
    return str(uuid.uuid5(DREAM_NS, f"dream::{cluster_key}"))


def profile_point_id(member_id: str, workspace_id: str) -> str:
    return str(uuid.uuid5(DREAM_NS, f"profile::{member_id}::{workspace_id}"))


def _uniform(members: list[Candidate], key: str) -> str:
    values = {str(m.payload.get(key) or "") for m in members}
    if len(values) > 1:
        raise ValueError(f"cluster is not homogeneous in {key}: {sorted(values)}")
    return values.pop() if values else ""


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
        "dreamed_from": list(insight.source_ids),
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
        "domain": _uniform(members, "domain") or "general",
        "tags": ["dream"],
        # Recall reads memory_type from the projection, GC from top-level — write
        # both so they can never disagree about this point.
        "metadata": {"memory_type": "procedural", "dream_cluster_key": cluster_key},
    }


async def write_dream(
    vector, insight: Insight, members: list[Candidate], *, cluster_key: str, run_id: str
) -> str:
    payload = build_dream_payload(insight, members, cluster_key=cluster_key, run_id=run_id)
    point_id = dream_point_id(cluster_key)
    await vector.upsert_point(point_id, insight.content, payload)
    return point_id
