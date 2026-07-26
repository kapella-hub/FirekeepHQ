"""One-off cleanup: remove the retired BACKLINK edges (+ orphaned MemoryRef nodes).

Automatic backlink creation was disabled long ago, but ~161K pre-existing
`BACKLINK` relationships and their `MemoryRef` nodes remain in Neo4j "pending a
separate cleanup step" (see cortex/CLAUDE.md). This is that step.

SAFETY — MemoryRef nodes are SHARED: they carry both the dead `BACKLINK` edges
AND live `SUPERSEDES` supersession chains (app/workers/memory_agent.py). So this
script deletes:
  1. every `BACKLINK` relationship, then
  2. only the `MemoryRef` nodes left with NO relationships at all (backlink-only
     refs). MemoryRef nodes still in a SUPERSEDES chain keep their edges and survive.

After this runs, `GET /memory/{id}/backlinks` returns empty — expected and
harmless (no new backlinks are ever created; the endpoint is vestigial).

DRY-RUN BY DEFAULT: reports counts and does nothing. Set BACKLINK_CLEANUP_APPLY=1
to actually delete. Deletes are batched via CALL { } IN TRANSACTIONS so a 161K
edge sweep never runs as one giant transaction.

Designed to run inside a cortex container (neo4j driver + NEO4J_* present),
pipeable over stdin:

    # dry run (report only)
    kubectl exec -i deploy/firekeep-cortex-api -- python - \
        < cortex/scripts/cleanup_backlink_edges.py

    # apply
    kubectl exec -i deploy/firekeep-cortex-api -- \
        env BACKLINK_CLEANUP_APPLY=1 python - \
        < cortex/scripts/cleanup_backlink_edges.py

Environment:
    NEO4J_URI       (default: bolt://neo4j:7687)
    NEO4J_USER      (default: neo4j)
    NEO4J_PASSWORD  (required)
    BACKLINK_CLEANUP_APPLY  (default unset = dry run; "1" = delete)
    BACKLINK_BATCH_SIZE     (default: 10000)
"""
from __future__ import annotations

import os
import sys

from neo4j import GraphDatabase


def main() -> int:
    uri = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")
    apply = os.environ.get("BACKLINK_CLEANUP_APPLY", "") not in ("", "0", "false", "no")
    batch = int(os.environ.get("BACKLINK_BATCH_SIZE", "10000"))

    if not password:
        print("NEO4J_PASSWORD is required", file=sys.stderr)
        return 2

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            backlinks = session.run(
                "MATCH ()-[b:BACKLINK]->() RETURN count(b) AS c"
            ).single()["c"]
            memrefs = session.run(
                "MATCH (m:MemoryRef) RETURN count(m) AS c"
            ).single()["c"]
            supersede_refs = session.run(
                "MATCH (m:MemoryRef) WHERE (m)-[:SUPERSEDES]-() RETURN count(DISTINCT m) AS c"
            ).single()["c"]
            print(
                f"BACKLINK edges: {backlinks}\n"
                f"MemoryRef nodes: {memrefs} (in SUPERSEDES chains: {supersede_refs}, "
                f"so ~{memrefs - supersede_refs} are backlink-only candidates for removal)"
            )

            if not apply:
                print("\nDRY RUN — nothing deleted. Set BACKLINK_CLEANUP_APPLY=1 to apply.")
                return 0

            print(f"\nApplying (batch size {batch})...")
            # Batched edge delete — CALL { } IN TRANSACTIONS runs in autocommit.
            session.run(
                "MATCH ()-[b:BACKLINK]->() "
                "CALL { WITH b DELETE b } IN TRANSACTIONS OF $batch ROWS",
                batch=batch,
            ).consume()
            # Now delete MemoryRef nodes orphaned by the edge removal (no rels left).
            session.run(
                "MATCH (m:MemoryRef) WHERE NOT (m)--() "
                "CALL { WITH m DELETE m } IN TRANSACTIONS OF $batch ROWS",
                batch=batch,
            ).consume()

            after_edges = session.run(
                "MATCH ()-[b:BACKLINK]->() RETURN count(b) AS c"
            ).single()["c"]
            after_refs = session.run(
                "MATCH (m:MemoryRef) RETURN count(m) AS c"
            ).single()["c"]
            print(
                f"done: BACKLINK edges now {after_edges} (was {backlinks}); "
                f"MemoryRef nodes now {after_refs} (was {memrefs}, "
                f"{memrefs - after_refs} orphans removed)"
            )
            print("Note: GET /memory/{id}/backlinks now returns empty — expected.")
            return 0
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(main())
