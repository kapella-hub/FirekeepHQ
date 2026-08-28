"""Near-duplicate collapse on the /memory/learn write path.

WHAT THIS ACTUALLY DOES — read this before relying on it to retire stale facts.

When a new memory is stored, this module finds existing active memories in the
same domain/namespace whose embedding is within ``SIMILARITY_THRESHOLD`` cosine
of the new one, and marks them ``superseded`` by it. That is a
NEAR-DUPLICATE COLLAPSE, not contradiction detection. There is no negation
check, no entailment check, and no LLM anywhere in this path — the only signal
is cosine similarity, and cosine similarity measures whether two statements are
ABOUT the same thing, not whether they AGREE.

The two failure modes follow directly, and both were reproduced against the
live store:

  * FALSE POSITIVE — "The widget service listens on TCP port 9931" followed by
    the same fact reworded supersedes the first, because they are near
    identical. Nothing contradicted it. This is the common case, and it is
    why supersession here no longer bumps ``contradicted_count`` (see
    ``count_as_contradiction`` below).

  * FALSE NEGATIVE — "The archive job runs nightly at 02:00 UTC" followed by
    "We removed all scheduling from the archive job; it is now triggered
    manually and never runs overnight" supersedes NOTHING: the correction is
    long, differently worded, and lands below the threshold. Both stay active,
    and a later recall returned the STALE fact at rank 1 and its correction at
    rank 2, with no signal that they conflict.

So: this collapses restatements. It does not retire stale facts, and no caller
should be written as though it does. Real contradiction detection needs an
NLI/LLM judgement on the write path — a design change with a real latency cost
on the CPU backends this product ships against, deliberately not smuggled in
under this module's old name.

The module keeps its filename and its public function name because both are
imported across `main.py` and the test suite; the honesty is in the behaviour
(no contradiction bookkeeping for a similarity match), the payload
(``supersede_reason``), and this docstring.
"""

from __future__ import annotations

import logging

from app.db.graph import Neo4jClient
from app.db.vector import VectorClient

logger = logging.getLogger(__name__)

# Cosine floor for "these two memories say the same thing". NOT a contradiction
# threshold — see the module docstring. Raising it collapses fewer restatements;
# lowering it cannot turn this into contradiction detection, only into a
# coarser duplicate merge.
SIMILARITY_THRESHOLD = 0.85

# Stamped onto every memory superseded by this path so an operator reading a
# payload can tell a similarity collapse from a human deprecation or a
# memory-agent contradiction ruling.
SUPERSEDE_REASON = "near-duplicate"


async def detect_and_supersede(
    vector: VectorClient,
    graph: Neo4jClient,
    new_text: str,
    new_vector_id: str,
    new_graph_id: str | None,
    domain: str,
    namespace: str = "default",
    *,
    workspace_id: str,
) -> list[str]:
    """Supersede near-duplicates of a freshly stored memory.

    Searches the vector store for active memories within
    ``SIMILARITY_THRESHOLD`` cosine of *new_text* in the same domain AND
    workspace (identity-v2 D4 — ``workspace_id`` is required and forwarded to
    ``find_similar`` unchanged; the caller is expected to pass the verified
    principal's workspace, exactly as ``/memory/learn`` already does for the
    write itself). Each match is marked superseded by the new memory (WITHOUT
    accruing a contradiction — see the module docstring), and a SUPERSEDES
    edge is created in the graph if a graph ID is available.

    Returns list of superseded memory IDs.
    """
    superseded_ids: list[str] = []

    try:
        # Find similar active memories. As of Dreaming Task 5, find_similar's
        # own filter (app/db/vector.py: _similarity_filter) excludes
        # confirmed_count > 0 points (a confirmed memory must never be
        # auto-superseded by an ordinary /memory/learn — a pre-existing
        # defect) and source="dream" points (a dream must never be merged
        # into or superseded by the episode it summarised). Nothing below
        # needs to duplicate those guards — they hold for every caller of
        # find_similar, not just this one. Identity-v2 D4 adds workspace_id
        # to that same filter, closing cross-workspace supersession.
        similar = await vector.find_similar(
            text=new_text,
            namespace=namespace,
            domain=domain,
            threshold=SIMILARITY_THRESHOLD,
            top_k=4,
            workspace_id=workspace_id,
        )

        for match in similar:
            # Skip if it's the same memory we just created
            if match["id"] == new_vector_id:
                continue

            # High similarity + same domain = a restatement of the same fact.
            logger.info(
                "Superseding near-duplicate memory %s (similarity=%.3f) with %s",
                match["id"],
                match["score"],
                new_vector_id,
            )

            # Update old memory status. count_as_contradiction=False: similarity
            # is not disagreement, and a restatement must not be recorded as
            # having been contradicted (that number feeds confidence, recall
            # ranking, and the memory agent's tie-breaks).
            try:
                await vector.update_status(
                    memory_id=match["id"],
                    status="superseded",
                    superseded_by=new_vector_id,
                    reason=SUPERSEDE_REASON,
                    count_as_contradiction=False,
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
                        reason=(
                            f"Auto-detected near-duplicate "
                            f"(similarity {match['score']:.3f})"
                        ),
                        detected="auto",
                    )
                except Exception:
                    logger.warning("Failed to create supersession edge for %s", match["id"])

    except Exception:
        logger.warning("Near-duplicate collapse failed, proceeding without supersession")

    return superseded_ids
