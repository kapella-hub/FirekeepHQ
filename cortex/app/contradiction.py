"""Contradiction detection for FirekeepCortex.

When a new memory is stored, this module checks for highly similar existing
memories in the same domain. If found, the older memories are automatically
superseded and a SUPERSEDES edge is created in the knowledge graph.
"""

from __future__ import annotations

import logging

from app.db.graph import Neo4jClient
from app.db.vector import VectorClient

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.85


async def detect_and_supersede(
    vector: VectorClient,
    graph: Neo4jClient,
    new_text: str,
    new_vector_id: str,
    new_graph_id: str | None,
    domain: str,
    namespace: str = "default",
) -> list[str]:
    """Check if new memory contradicts existing ones and auto-supersede.

    Searches the vector store for active memories that are highly similar
    (above *SIMILARITY_THRESHOLD*) to *new_text* within the same domain.
    Each match is marked as superseded by the new memory, and a
    SUPERSEDES edge is created in the graph if a graph ID is available.

    Returns list of superseded memory IDs.
    """
    superseded_ids: list[str] = []

    try:
        # Find similar active memories
        similar = await vector.find_similar(
            text=new_text,
            namespace=namespace,
            domain=domain,
            threshold=SIMILARITY_THRESHOLD,
            top_k=4,
        )

        for match in similar:
            # Skip if it's the same memory we just created
            if match["id"] == new_vector_id:
                continue

            # High similarity + same domain = likely contradiction/update
            logger.info(
                "Auto-superseding memory %s (score=%.3f) with %s",
                match["id"],
                match["score"],
                new_vector_id,
            )

            # Update old memory status
            try:
                await vector.update_status(
                    memory_id=match["id"],
                    status="superseded",
                    superseded_by=new_vector_id,
                )
                superseded_ids.append(match["id"])
            except Exception:
                logger.warning("Failed to supersede memory %s", match["id"])
                continue

            # Create graph edge if we have graph IDs
            if new_graph_id:
                try:
                    await graph.create_supersession(
                        newer_id=new_graph_id,
                        older_id=match["id"],
                        reason=f"Auto-detected: new memory with similarity {match['score']:.3f}",
                        detected="auto",
                    )
                except Exception:
                    logger.warning("Failed to create supersession edge for %s", match["id"])

    except Exception:
        logger.warning("Contradiction detection failed, proceeding without supersession")

    return superseded_ids
