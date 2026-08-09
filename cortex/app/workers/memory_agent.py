"""Memory Agent worker — autonomous knowledge custodian.

Runs as a periodic Celery task performing five analysis and repair passes
against the knowledge corpus: duplicate detection, orphan cleanup,
deep contradiction scan, confidence decay, and cluster coherence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import redis
import redis.asyncio
from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    Range,
)

from app.config import get_settings
from app.db.vector import FIREKEEP_UUID_NAMESPACE, _merge_lifecycle
from app.workers.sleep_cycle import _get_neo4j_driver, _get_redis_client, celery_app

logger = logging.getLogger(__name__)

# Redis lock key for preventing overlapping agent runs
AGENT_LOCK_KEY = "firekeep:memory_agent:lock"

# LLM prompt for merge synthesis
MERGE_SYSTEM_PROMPT = (
    "You are a knowledge consolidation engine. You are given a set of duplicate "
    "memory entries that describe the same concept or action. Synthesize them into "
    "a single improved memory that combines the best information from all entries.\n\n"
    "Return ONLY a JSON object with these fields:\n"
    '  {"text": "the synthesized memory text", "domain": "the domain", "tags": ["tag1", "tag2"]}\n\n'
    "Do not include markdown fencing or explanations. Output valid JSON only."
)


def _get_qdrant_client() -> QdrantClient:
    """Create a synchronous Qdrant client from settings."""
    s = get_settings()
    return QdrantClient(host=s.QDRANT_HOST, port=s.QDRANT_PORT)


def _fire_webhook_sync(redis_url: str, event_type: str, payload: dict[str, Any]) -> None:
    """Fire webhooks synchronously from the Celery worker context.

    Creates a temporary async event loop to call fire_webhooks.
    """
    try:
        from app.webhooks import fire_webhooks

        async def _fire():
            r = redis.asyncio.from_url(redis_url, decode_responses=True)
            try:
                await fire_webhooks(r, event_type, payload)
            finally:
                await r.aclose()

        asyncio.run(_fire())
    except Exception:
        logger.warning("Failed to fire webhook %s", event_type)


def _active_non_corpus_filter() -> Filter:
    """Scope filter shared by the maintenance passes: active memories only,
    never corpus chunks. Corpus chunks are document fragments, not competing
    memories — they must never be merged, superseded, or reclassified by
    the agent passes (SP0 B1, defect #3).

    Also excludes source="dream" (Dreaming Task 5, audit finding #2): without
    this, duplicate_detection_pass could merge two dreams (or a dream with a
    surviving source) and deep_contradiction_pass could supersede a dream with
    its own source episode — a feedback loop no dream code participates in.

    ...and source="dream_profile", the per-human person profiles written by
    app/dreams/profile.py, which was MISSING here while the docs claimed it
    was present (final-review I1). A profile is a single point that is
    REPLACED in place on every dream run, so both maintenance passes are
    actively wrong on it: duplicate_detection_pass would LLM-merge it into
    some other memory's text (destroying the one thing the briefing reads by
    point id), and deep_contradiction_pass would supersede it against the
    very memories it was synthesized from. Broad prose at domain="general" is
    exactly the shape that trips both.

    `confirmed_count > 0` is DELIBERATELY NOT excluded here, even though the
    confirmed-memory hole this filter's callers had is real (see
    `_dedup_scope_filter` and `deep_contradiction_pass`). This filter is
    shared by three passes and blanket exclusion is wrong for two of them:

      * deep_contradiction_pass — the filter scopes the similarity QUERY as
        well as the scroll, so excluding confirmed memories would stop one
        being FOUND, not just stop it being buried. A confirmed memory must
        still be able to supersede a stale rival; that pass therefore keeps
        full scope and refuses the write instead.
      * cluster_coherence_pass — it rewrites `domain`, never status or text,
        so it does not bury anything a human confirmed. Excluding confirmed
        memories would also drop them out of the per-domain CENTROIDS, which
        changes outlier detection for the UNCONFIRMED memories around them —
        a behaviour change to memories the protection is not about.

    Only dedup wants the exclusion, and it takes it via `_dedup_scope_filter`.
    """
    return Filter(
        must=[FieldCondition(key="status", match=MatchValue(value="active"))],
        must_not=[
            FieldCondition(key="source", match=MatchValue(value="corpus")),
            FieldCondition(key="source", match=MatchValue(value="dream")),
            FieldCondition(key="source", match=MatchValue(value="dream_profile")),
        ],
    )


def _dedup_scope_filter() -> Filter:
    """`_active_non_corpus_filter` plus: never a human-confirmed memory.

    A merge is not a ranking decision that a later pass can revisit — it
    REPLACES text. `_merge_cluster` re-embeds the LLM's synthesis, writes it
    as a new point, and supersedes every original including the keeper, so
    there is no merge outcome in which a confirmed memory's own wording
    survives. Worse, `_merge_lifecycle` folds `confirmed_count` forward as a
    max, so the synthesized point inherits the human's confirmation — text
    nobody vouched for, carrying the mark that says somebody did.

    Refusing to supersede the confirmed member (the shape used in
    `deep_contradiction_pass`) would not be enough here — but NOT because it
    leaves a residual near-duplicate behind. Exclusion leaves one too,
    whenever two or more unconfirmed members remain to merge: the confirmed
    memory stays active beside their merged survivor, which is a near
    duplicate of it. `test_confirmed_memory_is_not_merged_while_its_
    duplicates_still_are` demonstrates precisely that, so "it would leave a
    duplicate" cannot be the argument that separates the two options.

    What exclusion uniquely prevents is the two things a refusal cannot. A
    refused member is still a CLUSTER member: it is still folded into the
    merged payload, so `_merge_lifecycle` still launders its
    `confirmed_count` onto the synthesis, and its text is still sent to the
    merge model in the prompt. Only keeping the point out of the cluster
    stops both. That is also what the two standing precedents do —
    `gc.py::_scan_candidates` skips `confirmed_count > 0` as its first test,
    and `vector.py::_similarity_filter` carries the same `must_not` for
    learn-time contradiction detection.

    Derived from the shared filter rather than restated so the corpus/dream
    conditions cannot drift apart from it.

    Note the condition is `Range(gt=0)`, not "field absent": a point that
    predates the field has no `confirmed_count` to match, so `must_not` lets
    it through and legacy memories stay eligible for dedup.
    """
    scope = _active_non_corpus_filter()
    return Filter(
        must=list(scope.must or []),
        must_not=[
            *(scope.must_not or []),
            FieldCondition(key="confirmed_count", range=Range(gt=0)),
        ],
    )


# ---------------------------------------------------------------------------
# Pass 1: Duplicate Detection & Merge
# ---------------------------------------------------------------------------


def duplicate_detection_pass() -> dict[str, Any]:
    """Find near-duplicate active memories and merge them.

    Gated behind DEDUP_ENABLED (default False). Corpus chunks and
    human-confirmed memories are excluded from dedup scope entirely
    (see `_dedup_scope_filter`), and merges are restricted to same-domain
    clusters (SP0 B1, defect #3).
    """
    settings = get_settings()
    if not settings.DEDUP_ENABLED:
        logger.info("Dedup pass disabled via DEDUP_ENABLED=false")
        return {"status": "disabled", "merged": 0, "details": []}

    client = _get_qdrant_client()
    collection = settings.QDRANT_COLLECTION
    threshold = settings.DEDUP_SIMILARITY_THRESHOLD
    batch_limit = settings.AGENT_BATCH_LIMIT
    merged_count = 0
    results: list[dict] = []

    # Active memories only; never corpus chunks (document shredding guard),
    # never a memory a human confirmed (its text cannot survive a merge).
    dedup_filter = _dedup_scope_filter()

    try:
        # Scroll all active non-corpus memories
        memories: list[dict] = []
        offset = None
        while len(memories) < batch_limit:
            points, next_offset = client.scroll(
                collection_name=collection,
                scroll_filter=dedup_filter,
                limit=min(100, batch_limit - len(memories)),
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            for p in points:
                memories.append({
                    "id": str(p.id),
                    "text": p.payload.get("text", "") if p.payload else "",
                    "domain": p.payload.get("domain", "") if p.payload else "",
                    "tags": p.payload.get("tags", []) if p.payload else [],
                    "confirmed_count": p.payload.get("confirmed_count", 0) if p.payload else 0,
                    "contradicted_count": p.payload.get("contradicted_count", 0) if p.payload else 0,
                    "vector": p.vector,
                    "payload": p.payload or {},
                })
            if next_offset is None or not points:
                break
            offset = next_offset

        if len(memories) < 2:
            return {"status": "ok", "merged": 0, "details": []}

        processed_ids: set[str] = set()
        clusters: list[list[dict]] = []

        for mem in memories:
            if mem["id"] in processed_ids:
                continue

            similar_results = client.query_points(
                collection_name=collection,
                query=mem["vector"],
                query_filter=dedup_filter,
                limit=10,
                with_payload=True,
            )

            cluster = [mem]
            for point in similar_results.points:
                pid = str(point.id)
                if pid == mem["id"] or pid in processed_ids:
                    continue
                if point.score >= threshold:
                    match = next((m for m in memories if m["id"] == pid), None)
                    # Same-domain restriction: never merge across domains.
                    if match and match["domain"] == mem["domain"]:
                        cluster.append(match)

            if len(cluster) >= 2:
                clusters.append(cluster)
                for c in cluster:
                    processed_ids.add(c["id"])

        for cluster in clusters:
            if merged_count >= batch_limit:
                break
            merge_result = _merge_cluster(client, cluster, settings)
            if merge_result:
                results.append(merge_result)
                merged_count += 1

    except Exception:
        logger.exception("Error in duplicate_detection_pass")
    finally:
        client.close()

    for r in results:
        _fire_webhook_sync(settings.REDIS_URL, "agent.merged", r)

    return {"status": "ok", "merged": merged_count, "details": results}


def _embed_sync(settings: Any, text: str) -> list[float]:
    """Synchronous embedding call for the Celery worker context.

    Raises on any failure — callers must treat a failed embed as a hard
    abort, never write text whose stored vector doesn't match (SP0 principle).
    """
    headers = (
        {"Authorization": f"Bearer {settings.LLM_API_KEY}"}
        if settings.LLM_API_KEY
        else {}
    )
    response = httpx.post(
        f"{settings.LLM_BASE_URL}/embeddings",
        json={"model": settings.EMBEDDING_MODEL, "input": text},
        headers=headers,
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["data"][0]["embedding"]


def _created_sort_key(member: dict) -> str:
    """Sort key for lifecycle folding: created_at, falling back to timestamp."""
    payload = member.get("payload", {})
    return payload.get("created_at") or payload.get("timestamp") or ""


def _merge_cluster(
    client: QdrantClient,
    cluster: list[dict],
    settings: Any,
) -> dict | None:
    """Merge a cluster of duplicate memories. Returns merge info or None.

    SP0 B1: when the LLM produces new merged text, the text is RE-EMBEDDED
    and written as a NEW point keyed to uuid5(merged_text), inheriting
    lifecycle via _merge_lifecycle (max confirmed_count of the cluster,
    earliest created_at). All cluster members — keeper included — are then
    marked superseded by the new point. If embedding fails, the merge is
    aborted with no writes at all.
    """
    # Try LLM merge first
    merged_text = None
    method = "fallback"

    try:
        texts = [m["text"] for m in cluster]
        prompt = "Merge these duplicate memories into one:\n\n" + "\n---\n".join(texts)

        response = httpx.post(
            f"{settings.LLM_BASE_URL}/chat/completions",
            json={
                "model": settings.LLM_MODEL,
                "messages": [
                    {"role": "system", "content": MERGE_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"} if settings.LLM_API_KEY else {},
            timeout=60.0,
        )
        response.raise_for_status()
        msg = response.json()["choices"][0]["message"]
        content = msg.get("content") or ""
        # Fallback: some models (e.g. qwen3) put output in a reasoning field
        if not content.strip():
            content = msg.get("reasoning") or ""
        parsed = json.loads(content)
        merged_text = parsed.get("text", "")

        # Validate: merged text should be non-empty and reasonable length
        min_len = min(len(t) for t in texts) // 2
        if merged_text.strip() and len(merged_text) >= max(min_len, 10):
            method = "llm"
        else:
            merged_text = None
    except Exception:
        logger.warning("LLM merge failed, using fallback")

    # Always select highest confidence memory as keeper
    best = max(
        cluster,
        key=lambda m: (1 + m["confirmed_count"]) / (1 + m["contradicted_count"]),
    )
    if merged_text is None:
        merged_text = best["text"]
    keeper = best

    if merged_text == keeper["text"]:
        # Text unchanged (fallback path): keeper's stored vector already
        # matches its text. No re-embed, no re-key — supersede losers only.
        merged_into = keeper["id"]
        superseded_ids = [m["id"] for m in cluster if m["id"] != keeper["id"]]
    else:
        # Text changed: re-embed FIRST. A failure here aborts the whole merge
        # before any write — never store text whose vector doesn't match.
        try:
            new_vector = _embed_sync(settings, merged_text)
        except Exception:
            logger.error(
                "Merge aborted: failed to embed merged text for cluster %s",
                [m["id"] for m in cluster],
            )
            return None

        merged_into = str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, merged_text))

        # Build the merged payload: fresh copy of the keeper's payload with the
        # new text, then fold lifecycle fields from every cluster member via
        # _merge_lifecycle. Folding newest-first means the LAST fold (the
        # earliest member) supplies created_at — earliest created_at wins;
        # confirmed_count accumulates as max across all members.
        merged_payload = dict(keeper["payload"])
        merged_payload.update({
            "text": merged_text,
            "domain": keeper["domain"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "active",
            "superseded_by": None,
        })
        for member in sorted(cluster, key=_created_sort_key, reverse=True):
            merged_payload = _merge_lifecycle(member["payload"], merged_payload)

        try:
            client.upsert(
                collection_name=settings.QDRANT_COLLECTION,
                points=[
                    PointStruct(
                        id=merged_into,
                        vector=new_vector,
                        payload=merged_payload,
                    )
                ],
            )
        except Exception:
            logger.error("Merge aborted: failed to upsert merged point %s", merged_into)
            return None

        # Every original member (keeper included) is now superseded by the
        # merged point.
        superseded_ids = [m["id"] for m in cluster if m["id"] != merged_into]

    # Update superseded memories' status
    for sid in superseded_ids:
        try:
            client.set_payload(
                collection_name=settings.QDRANT_COLLECTION,
                payload={
                    "status": "superseded",
                    "superseded_by": merged_into,
                },
                points=[sid],
            )
        except Exception:
            logger.warning("Failed to supersede memory %s", sid)

    # Create SUPERSEDES edges in Neo4j
    try:
        driver = _get_neo4j_driver()
        with driver.session() as session:
            for sid in superseded_ids:
                session.run(
                    "MERGE (ref_new:MemoryRef {vector_id: $keeper_id}) "
                    "MERGE (ref_old:MemoryRef {vector_id: $old_id}) "
                    "MERGE (ref_new)-[:SUPERSEDES {reason: $reason, detected: 'agent', timestamp: datetime()}]->(ref_old)",
                    keeper_id=merged_into,
                    old_id=sid,
                    reason=f"Agent duplicate merge ({method})",
                )
    except Exception:
        logger.warning("Failed to create SUPERSEDES edges for merge")

    return {
        "merged_into": merged_into,
        "superseded": superseded_ids,
        "method": method,
    }


# ---------------------------------------------------------------------------
# Pass 2: Orphan Cleanup
# ---------------------------------------------------------------------------


def orphan_cleanup_pass() -> dict[str, Any]:
    """Delete orphaned nodes (degree 0) from Neo4j.

    Gated behind GC_PURGE_ENABLED (default False) alongside every other
    hard-delete path: this pass destroys graph rows irreversibly, so it runs
    only where the operator has explicitly opted into purging.
    """
    settings = get_settings()
    if not settings.GC_PURGE_ENABLED:
        logger.info("Orphan cleanup disabled via GC_PURGE_ENABLED=false")
        return {"status": "disabled", "nodes_removed": []}

    batch_limit = settings.AGENT_BATCH_LIMIT
    nodes_removed: list[dict] = []

    try:
        driver = _get_neo4j_driver()
        with driver.session() as session:
            with session.begin_transaction() as tx:
                result = tx.run(
                    "MATCH (n) "
                    "WHERE (n:Domain OR n:Concept OR n:Action "
                    "       OR n:Outcome OR n:Resolution OR n:MemoryRef) "
                    "  AND NOT (n)--() "
                    "WITH n LIMIT $limit "
                    "WITH n, labels(n)[0] AS label, "
                    "     COALESCE(n.name, n.description, n.id, n.vector_id, 'unnamed') AS name "
                    "DETACH DELETE n "
                    "RETURN label, name",
                    limit=batch_limit,
                )
                for record in result:
                    nodes_removed.append({
                        "label": record["label"],
                        "name": record["name"],
                    })
    except Exception:
        logger.exception("Error in orphan_cleanup_pass")

    if nodes_removed:
        _fire_webhook_sync(
            settings.REDIS_URL,
            "agent.orphan_cleaned",
            {"nodes_removed": nodes_removed},
        )

    return {"status": "ok", "nodes_removed": nodes_removed}


# ---------------------------------------------------------------------------
# Pass 3: Deep Contradiction Scan
# ---------------------------------------------------------------------------


def deep_contradiction_pass() -> dict[str, Any]:
    """Find contradictions missed at learn-time across different domains.

    Corpus chunks are excluded from scope — document fragments are not
    competing memories to auto-supersede (SP0 B1 follow-up).

    Human-confirmed memories stay IN scope but are never the superseded side
    (see the guard below): confirmation must protect a memory from burial
    without also disqualifying it from winning.
    """
    settings = get_settings()
    client = _get_qdrant_client()
    collection = settings.QDRANT_COLLECTION
    batch_limit = settings.AGENT_BATCH_LIMIT
    contradictions: list[dict] = []
    scope_filter = _active_non_corpus_filter()

    try:
        # Scroll active non-corpus memories
        memories: list[dict] = []
        offset = None
        while len(memories) < batch_limit:
            points, next_offset = client.scroll(
                collection_name=collection,
                scroll_filter=scope_filter,
                limit=min(100, batch_limit - len(memories)),
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            for p in points:
                memories.append({
                    "id": str(p.id),
                    "text": p.payload.get("text", "") if p.payload else "",
                    "domain": p.payload.get("domain", "") if p.payload else "",
                    "timestamp": p.payload.get("timestamp", "") if p.payload else "",
                    "confirmed_count": p.payload.get("confirmed_count", 0) if p.payload else 0,
                    "contradicted_count": p.payload.get("contradicted_count", 0) if p.payload else 0,
                    # Carried so the contested skip holds ACROSS passes: a pair
                    # flagged yesterday must not be re-contested (and possibly
                    # re-pointed at a different partner) before it is resolved.
                    "contested": bool(p.payload.get("contested")) if p.payload else False,
                    # Carried so a coexist VERDICT holds across passes: both
                    # sides of a resolved pair stay active with unchanged texts
                    # still inside the similarity band, so without this marker
                    # the pass would re-contest the identical pair every night
                    # and the verdict would be functionally meaningless.
                    "coexist_with": p.payload.get("coexist_with") if p.payload else None,
                    "vector": p.vector,
                })
            if next_offset is None or not points:
                break
            offset = next_offset

        processed_pairs: set[tuple[str, str]] = set()

        for mem in memories:
            if len(contradictions) >= batch_limit:
                break

            # Search for similar memories in 0.85-0.95 range (not already SUPERSEDES-linked)
            similar_results = client.query_points(
                collection_name=collection,
                query=mem["vector"],
                query_filter=scope_filter,
                limit=5,
                with_payload=True,
            )

            for point in similar_results.points:
                pid = str(point.id)
                if pid == mem["id"]:
                    continue

                pair_key = tuple(sorted([mem["id"], pid]))
                if pair_key in processed_pairs:
                    continue
                processed_pairs.add(pair_key)

                if not (0.85 <= point.score <= 0.95):
                    continue

                # Check if already linked by SUPERSEDES in Neo4j
                if _has_supersedes_link(mem["id"], pid):
                    continue

                # Determine which is stale: older + lower confidence loses
                other = next((m for m in memories if m["id"] == pid), None)
                if other is None:
                    continue

                # A memory superseded EARLIER IN THIS PASS is out of play for
                # every later pair. The Qdrant write already happened but the
                # in-memory dicts scrolled at the start don't know it — without
                # this guard a chain A~B~C (B confirmed) superseded A via the
                # (A,B) pair and then contested (A,C), producing a dispute
                # whose other half is invisible to recall and to the inbox's
                # active-only contested section.
                if mem.get("_superseded_in_pass") or other.get("_superseded_in_pass"):
                    continue

                # A pair a human already resolved as "both true" (coexist
                # verdict) is settled — re-flagging it would undo the verdict.
                if (
                    mem.get("coexist_with") == other["id"]
                    or other.get("coexist_with") == mem["id"]
                ):
                    continue

                mem_confidence = (1 + mem["confirmed_count"]) / (1 + mem["contradicted_count"])
                other_confidence = (1 + other["confirmed_count"]) / (1 + other["contradicted_count"])

                # Stale side: lower confidence, or if equal, older
                if mem_confidence < other_confidence or (
                    mem_confidence == other_confidence and mem["timestamp"] < other["timestamp"]
                ):
                    stale, keeper = mem, other
                else:
                    stale, keeper = other, mem

                # Human confirmation is PROTECTION, not a tiebreak input.
                # The ratio above already reads confirmed_count, which is
                # what made this look handled — but it only ever ranks: a
                # memory confirmed once (2.0) still loses to one confirmed
                # three times (4.0), and a confirmed-but-contradicted memory
                # (1+1)/(1+2)=0.67 loses to a never-confirmed one (1.0).
                # Either way an explicit human "this is correct" ended in
                # status=superseded, a permanent 0.5 recall multiplier.
                #
                # Skip the pair rather than inverting it: inverting would
                # bury the side the ratio says is better on no human signal
                # at all, and when BOTH sides are confirmed there is nothing
                # to invert to. One rule, both directions — this pass never
                # supersedes a confirmed memory, and a confirmed KEEPER
                # still supersedes an unconfirmed rival exactly as before.
                if (stale.get("confirmed_count") or 0) > 0:
                    logger.debug(
                        "Skipping contradiction %s -> %s: stale side is human-confirmed",
                        keeper["id"], stale["id"],
                    )
                    continue

                # CONTESTED, not silently superseded (Knowledge Autopilot
                # round 1). When NEITHER side carries human confirmation, the
                # confidence ratio above is a guess dressed as a decision —
                # both counts are usually 0/0 and the "winner" is just the
                # newer timestamp. Superseding on that made the loser
                # unrecallable (recall's filter is a hard status=active) with
                # nobody ever seeing the conflict. Now: both sides stay
                # active, both get contested flags, recall annotates the
                # dispute, and the autopilot inbox surfaces the pair for a
                # human verdict (resolved via /memory/contested/resolve).
                # A confirmed KEEPER against an unconfirmed rival still
                # supersedes exactly as before — human evidence is a real
                # signal and acting on it is not guessing.
                if (keeper.get("confirmed_count") or 0) > 0:
                    try:
                        client.set_payload(
                            collection_name=collection,
                            payload={
                                "status": "superseded",
                                "superseded_by": keeper["id"],
                                # The digest counts supersessions by this stamp;
                                # without it a nightly-pass supersession under a
                                # months-old confirmed keeper is invisible to
                                # "what changed this week".
                                "superseded_at": datetime.now(timezone.utc).isoformat(),
                            },
                            points=[stale["id"]],
                        )
                    except Exception:
                        logger.warning("Failed to supersede memory %s", stale["id"])
                        continue
                    stale["_superseded_in_pass"] = True
                    try:
                        driver = _get_neo4j_driver()
                        with driver.session() as session:
                            session.run(
                                "MERGE (ref_new:MemoryRef {vector_id: $keeper_id}) "
                                "MERGE (ref_old:MemoryRef {vector_id: $old_id}) "
                                "MERGE (ref_new)-[:SUPERSEDES {reason: $reason, detected: 'agent', timestamp: datetime()}]->(ref_old)",
                                keeper_id=keeper["id"],
                                old_id=stale["id"],
                                reason=f"Deep contradiction scan (similarity={point.score:.3f})",
                            )
                    except Exception:
                        logger.warning("Failed to create SUPERSEDES edge")
                    contradiction_info = {
                        "kept": keeper["id"],
                        "superseded": stale["id"],
                        "similarity": round(point.score, 4),
                    }
                else:
                    # One conflict at a time per memory: a pair where either
                    # side is already contested waits for the open dispute to
                    # be resolved first.
                    if mem.get("contested") or other.get("contested"):
                        continue
                    contested_at = datetime.now(timezone.utc).isoformat()
                    try:
                        for this_id, other_id in (
                            (mem["id"], other["id"]),
                            (other["id"], mem["id"]),
                        ):
                            client.set_payload(
                                collection_name=collection,
                                payload={
                                    "contested": True,
                                    "contested_with": other_id,
                                    "contested_at": contested_at,
                                },
                                points=[this_id],
                            )
                    except Exception:
                        logger.warning(
                            "Failed to contest pair %s / %s", mem["id"], other["id"]
                        )
                        continue
                    mem["contested"] = True
                    other["contested"] = True
                    contradiction_info = {
                        "contested": [mem["id"], other["id"]],
                        "similarity": round(point.score, 4),
                    }

                contradictions.append(contradiction_info)

                _fire_webhook_sync(
                    settings.REDIS_URL,
                    "agent.contradiction_found",
                    contradiction_info,
                )

    except Exception:
        logger.exception("Error in deep_contradiction_pass")
    finally:
        client.close()

    return {"status": "ok", "contradictions_found": len(contradictions), "details": contradictions}


def _has_supersedes_link(id_a: str, id_b: str) -> bool:
    """Check if two memories are already linked by SUPERSEDES in Neo4j."""
    try:
        driver = _get_neo4j_driver()
        with driver.session() as session:
            result = session.run(
                "OPTIONAL MATCH (a:MemoryRef {vector_id: $id_a})-[:SUPERSEDES]-(b:MemoryRef {vector_id: $id_b}) "
                "RETURN a IS NOT NULL AND b IS NOT NULL AS linked",
                id_a=id_a, id_b=id_b,
            )
            record = result.single()
            return bool(record and record["linked"])
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Pass 4 (removed): Backlink Reinforcement — write-only, never queried by recall
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pass 5: Cluster Coherence
# ---------------------------------------------------------------------------


def cluster_coherence_pass() -> dict[str, Any]:
    """Check domain coherence and reclassify outlier memories.

    Corpus chunks are excluded from scope — their domain reflects the source
    document, not a memory cluster, and must never be rewritten by centroid
    outlier detection (SP0 B1 follow-up).
    """
    settings = get_settings()
    client = _get_qdrant_client()
    collection = settings.QDRANT_COLLECTION
    batch_limit = settings.AGENT_BATCH_LIMIT
    results: list[dict] = []

    try:
        # Scroll all active non-corpus memories, group by domain (bounded to avoid OOM)
        domains: dict[str, list[dict]] = {}
        offset = None
        max_scroll = batch_limit * 10  # upper bound on total memories scanned
        total_scanned = 0

        while total_scanned < max_scroll:
            points, next_offset = client.scroll(
                collection_name=collection,
                scroll_filter=_active_non_corpus_filter(),
                limit=min(100, max_scroll - total_scanned),
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )

            for p in points:
                total_scanned += 1
                payload = p.payload or {}
                domain = payload.get("domain", "general")
                domains.setdefault(domain, []).append({
                    "id": str(p.id),
                    "text": payload.get("text", ""),
                    "domain": domain,
                    "vector": p.vector,
                })

            if next_offset is None or not points:
                break
            offset = next_offset

        # Compute domain centroids (average vector)
        domain_centroids: dict[str, list[float]] = {}
        for domain, mems in domains.items():
            if len(mems) < 3:
                continue
            vectors = [m["vector"] for m in mems if m["vector"]]
            if not vectors:
                continue
            dim = len(vectors[0])
            centroid = [sum(v[d] for v in vectors) / len(vectors) for d in range(dim)]
            domain_centroids[domain] = centroid

        # For each domain with 3+ memories, find outliers
        for domain, mems in domains.items():
            if len(mems) < 3 or domain not in domain_centroids:
                continue
            if len(results) >= batch_limit:
                break

            centroid = domain_centroids[domain]

            # Compute similarity of each memory to its domain centroid
            similarities: list[tuple[dict, float]] = []
            for mem in mems:
                if not mem["vector"]:
                    continue
                sim = _cosine_similarity(mem["vector"], centroid)
                similarities.append((mem, sim))

            if len(similarities) < 3:
                continue

            scores = [s for _, s in similarities]
            mean_sim = statistics.mean(scores)
            std_sim = statistics.stdev(scores) if len(scores) > 1 else 0.0

            if std_sim == 0:
                continue

            # Flag outliers: more than 1 stddev below mean
            threshold = mean_sim - std_sim

            for mem, sim in similarities:
                if sim >= threshold:
                    continue
                if len(results) >= batch_limit:
                    break

                # Check if a better domain exists
                best_domain = domain
                best_sim = sim
                for other_domain, other_centroid in domain_centroids.items():
                    if other_domain == domain:
                        continue
                    other_sim = _cosine_similarity(mem["vector"], other_centroid)
                    if other_sim > best_sim + 0.1:  # Require 0.1 margin
                        best_domain = other_domain
                        best_sim = other_sim

                if best_domain != domain:
                    # Reclassify
                    try:
                        client.set_payload(
                            collection_name=collection,
                            payload={"domain": best_domain},
                            points=[mem["id"]],
                        )
                    except Exception:
                        logger.warning("Failed to reclassify memory %s", mem["id"])
                        continue

                    # Update domain relationship in Neo4j
                    try:
                        driver = _get_neo4j_driver()
                        with driver.session() as session:
                            with session.begin_transaction() as tx:
                                # Remove old domain relationship
                                tx.run(
                                    "MATCH (ref:MemoryRef {vector_id: $vid})-[r:RELATES_TO]->(d:Domain {name: $old_domain}) "
                                    "DELETE r",
                                    vid=mem["id"], old_domain=domain,
                                )
                                # Create new domain relationship
                                tx.run(
                                    "MERGE (ref:MemoryRef {vector_id: $vid}) "
                                    "MERGE (d:Domain {name: $new_domain}) "
                                    "MERGE (ref)-[:RELATES_TO]->(d)",
                                    vid=mem["id"], new_domain=best_domain,
                                )
                    except Exception:
                        logger.warning("Failed to update Neo4j domain for %s", mem["id"])

                    reclassify_info = {
                        "memory_id": mem["id"],
                        "from_domain": domain,
                        "to_domain": best_domain,
                        "similarity_improvement": round(best_sim - sim, 4),
                    }
                    results.append(reclassify_info)
                    _fire_webhook_sync(
                        settings.REDIS_URL,
                        "agent.reclassified",
                        reclassify_info,
                    )

    except Exception:
        logger.exception("Error in cluster_coherence_pass")
    finally:
        client.close()

    return {"status": "ok", "reclassified": len(results), "details": results}


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Pass: Access-count flush (Redis hash -> Qdrant payloads)
# ---------------------------------------------------------------------------


def flush_access_counts() -> dict[str, Any]:
    """Flush recall access-count deltas from Redis into Qdrant payloads.

    The recall hot path does HINCRBY into ACCESS_COUNTS_KEY (see
    main.py:memory_recall); this pass rotates that hash to a ":flushing"
    key (atomic RENAME so concurrent recalls keep incrementing a fresh
    hash), then adds each delta onto the persisted payload access_count.
    A crashed flush leaves the ":flushing" key behind; the next run
    processes it before rotating again.
    """
    from app.workers.gc import ACCESS_COUNTS_KEY

    settings = get_settings()
    r = _get_redis_client()
    client = _get_qdrant_client()
    flushing_key = f"{ACCESS_COUNTS_KEY}:flushing"
    flushed = 0
    stale = 0

    try:
        if not r.exists(flushing_key):
            try:
                r.rename(ACCESS_COUNTS_KEY, flushing_key)
            except redis.ResponseError:
                # No accumulated counts — nothing to flush.
                return {"status": "ok", "flushed": 0, "stale": 0}

        counts = r.hgetall(flushing_key)
        for memory_id, delta in counts.items():
            points = client.retrieve(
                settings.QDRANT_COLLECTION, ids=[memory_id], with_payload=True
            )
            if not points:
                # Memory was deleted (GC or manual) — drop the stale delta.
                stale += 1
                r.hdel(flushing_key, memory_id)
                continue
            current = int((points[0].payload or {}).get("access_count") or 0)
            # Idempotent recovery: drop the delta from the hash BEFORE writing
            # it to the payload. A crash between the two loses the delta — a
            # benign undercount of a best-effort signal. The reverse order
            # would double-apply the delta on the next recovery run.
            r.hdel(flushing_key, memory_id)
            client.set_payload(
                collection_name=settings.QDRANT_COLLECTION,
                payload={"access_count": current + int(delta)},
                points=[memory_id],
            )
            flushed += 1
    except Exception:
        logger.exception("Error in flush_access_counts")
        return {"status": "error", "flushed": flushed, "stale": stale}
    finally:
        client.close()

    return {"status": "ok", "flushed": flushed, "stale": stale}


def flush_last_recalled() -> dict[str, Any]:
    """Flush recall-timestamp deltas from Redis into Qdrant `last_recalled_at`.

    Mirrors flush_access_counts' rotate-then-drain discipline for the
    LAST_RECALLED_KEY hash (HSET on the recall hot path). A simple overwrite
    per memory_id (last-writer-wins on 'now'); feeds the skill staleness sweep.
    """
    from app.workers.gc import LAST_RECALLED_KEY

    settings = get_settings()
    r = _get_redis_client()
    client = _get_qdrant_client()
    flushing_key = f"{LAST_RECALLED_KEY}:flushing"
    flushed = 0

    try:
        if not r.exists(flushing_key):
            try:
                r.rename(LAST_RECALLED_KEY, flushing_key)
            except redis.ResponseError:
                return {"status": "ok", "flushed": 0}

        entries = r.hgetall(flushing_key)
        for memory_id, ts in entries.items():
            # hdel before set_payload: a crash between them loses this delta —
            # a benign undercount of a best-effort freshness signal.
            r.hdel(flushing_key, memory_id)
            client.set_payload(
                collection_name=settings.QDRANT_COLLECTION,
                payload={"last_recalled_at": ts},
                points=[memory_id],
            )
            flushed += 1
    except Exception:
        logger.exception("Error in flush_last_recalled")
        return {"status": "error", "flushed": flushed}
    finally:
        client.close()

    return {"status": "ok", "flushed": flushed}


# ---------------------------------------------------------------------------
# Main Celery task
# ---------------------------------------------------------------------------


@celery_app.task(name="app.workers.memory_agent.run_memory_agent")
def run_memory_agent() -> dict[str, Any]:
    """Run the memory-agent maintenance passes sequentially.

    Checks AGENT_ENABLED kill switch, acquires Redis SETNX lock,
    runs each pass with isolated error handling, and releases the lock.
    """
    settings = get_settings()

    # Kill switch
    if not settings.AGENT_ENABLED:
        logger.info("Memory agent disabled via AGENT_ENABLED=false")
        return {"status": "disabled"}

    # Acquire lock
    redis_client = _get_redis_client()
    lock_ttl = settings.AGENT_SCHEDULE_HOURS * 3600
    acquired = redis_client.set(AGENT_LOCK_KEY, "1", nx=True, ex=lock_ttl)
    if not acquired:
        logger.info("Memory agent lock already held, skipping run")
        return {"status": "locked"}

    logger.info("Memory agent starting — running maintenance passes")
    results: dict[str, Any] = {"status": "ok", "passes": {}}

    from app.skills.staleness import skill_staleness_pass

    passes = [
        ("duplicate_detection", duplicate_detection_pass),
        ("orphan_cleanup", orphan_cleanup_pass),
        ("deep_contradiction", deep_contradiction_pass),
        ("cluster_coherence", cluster_coherence_pass),
        ("access_count_flush", flush_access_counts),
        # last-recalled flush must precede the staleness sweep so freshness
        # timestamps are current when it evaluates (blueprint ordering).
        ("last_recalled_flush", flush_last_recalled),
        ("skill_staleness", skill_staleness_pass),
    ]

    for name, func in passes:
        try:
            logger.info("Memory agent: starting %s pass", name)
            result = func()
            results["passes"][name] = result
            logger.info("Memory agent: %s pass completed — %s", name, result.get("status", "unknown"))
        except Exception:
            logger.exception("Memory agent: %s pass failed", name)
            results["passes"][name] = {"status": "error"}

    # Pass 5 (skill synthesis catch-all)
    try:
        from app.workers.skill_synthesis import skill_synthesis_pass
        pass5_result = skill_synthesis_pass()
        results["passes"]["skill_synthesis"] = pass5_result
        logger.info("Pass 5 (skill synthesis): %s", pass5_result)
    except Exception as e:
        logger.error("Pass 5 (skill synthesis) failed: %s", e)
        results["passes"]["skill_synthesis"] = {"status": "error"}

    # Release lock
    try:
        redis_client.delete(AGENT_LOCK_KEY)
    except Exception:
        logger.warning("Failed to release memory agent lock")

    logger.info("Memory agent completed all passes")
    return results
