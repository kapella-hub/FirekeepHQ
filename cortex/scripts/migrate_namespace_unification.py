"""One-time migration: unify the ``firekeepbridge`` namespace into ``default`` (SP0, C1).

Bridge distillates were historically written under namespace ``firekeepbridge``
while agent ``memory_learn`` calls store under ``default``. Proactive recall's
must-filter on ``firekeepbridge`` meant agent-learned memories never surfaced
(defect #9). This script re-tags both halves of the store:

1. Qdrant: every point with payload ``namespace="firekeepbridge"`` -> ``"default"``.
2. Neo4j: every ``(:Namespace {name:"firekeepbridge"})-[:CONTAINS]->(d)`` edge is
   re-linked to ``(:Namespace {name:"default"})`` and the old edge deleted; the
   ``firekeepbridge`` Namespace node is removed once it has no remaining edges.

Idempotent: re-runs match zero points/edges. Supports ``--dry-run``.

Usage::

    QDRANT_HOST=localhost NEO4J_URI=bolt://localhost:7687 NEO4J_PASSWORD=... \
        python -m cortex.scripts.migrate_namespace_unification [--dry-run]

Environment:
    QDRANT_HOST       (default: "qdrant")
    QDRANT_PORT       (default: 6333)
    QDRANT_COLLECTION (default: "firekeep_memory")
    NEO4J_URI         (default: "bolt://neo4j:7687")
    NEO4J_USER        (default: "neo4j")
    NEO4J_PASSWORD    (required for the Neo4j half)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from neo4j import AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

OLD_NAMESPACE = "firekeepbridge"
NEW_NAMESPACE = "default"
BATCH_SIZE = 500

_RELINK_QUERY = """
MATCH (old:Namespace {name: $old_ns})-[c:CONTAINS]->(d)
MERGE (new:Namespace {name: $new_ns})
MERGE (new)-[:CONTAINS]->(d)
DELETE c
RETURN count(d) AS relinked
"""

_ORPHAN_QUERY = """
MATCH (old:Namespace {name: $old_ns})
WHERE NOT (old)-[:CONTAINS]->()
DETACH DELETE old
RETURN count(old) AS deleted
"""

_DRY_RUN_QUERY = """
MATCH (old:Namespace {name: $old_ns})-[:CONTAINS]->(d)
RETURN count(d) AS relinked
"""


async def migrate_qdrant_namespace(
    client,
    collection: str,
    old_ns: str = OLD_NAMESPACE,
    new_ns: str = NEW_NAMESPACE,
    dry_run: bool = False,
) -> dict:
    """Re-tag all Qdrant points with payload namespace=old_ns to new_ns."""
    ns_filter = Filter(
        must=[FieldCondition(key="namespace", match=MatchValue(value=old_ns))]
    )
    offset = None
    updated = 0
    while True:
        batch, offset = await client.scroll(
            collection_name=collection,
            scroll_filter=ns_filter,
            limit=BATCH_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not batch:
            break
        ids = [point.id for point in batch]
        updated += len(ids)
        if not dry_run:
            await client.set_payload(
                collection_name=collection,
                payload={"namespace": new_ns},
                points=ids,
            )
        print(f"  qdrant: {'would update' if dry_run else 'updated'} "
              f"{len(ids)} (running total: {updated})")
        if offset is None:
            break
    return {"updated": updated, "dry_run": dry_run}


async def migrate_neo4j_namespace(
    session,
    old_ns: str = OLD_NAMESPACE,
    new_ns: str = NEW_NAMESPACE,
    dry_run: bool = False,
) -> dict:
    """Re-link CONTAINS edges from old_ns Namespace node to new_ns; drop orphan."""
    if dry_run:
        result = await session.run(_DRY_RUN_QUERY, old_ns=old_ns)
        record = await result.single()
        relinked = record["relinked"] if record else 0
        return {"relinked": relinked, "orphan_deleted": 0, "dry_run": True}

    result = await session.run(_RELINK_QUERY, old_ns=old_ns, new_ns=new_ns)
    record = await result.single()
    relinked = record["relinked"] if record else 0

    result = await session.run(_ORPHAN_QUERY, old_ns=old_ns)
    record = await result.single()
    deleted = record["deleted"] if record else 0

    return {"relinked": relinked, "orphan_deleted": deleted, "dry_run": False}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args()

    qdrant_host = os.environ.get("QDRANT_HOST", "qdrant")
    qdrant_port = int(os.environ.get("QDRANT_PORT", "6333"))
    collection = os.environ.get("QDRANT_COLLECTION", "firekeep_memory")
    neo4j_uri = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
    neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
    neo4j_password = os.environ.get("NEO4J_PASSWORD", "")

    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"[{mode}] Unifying namespace '{OLD_NAMESPACE}' -> '{NEW_NAMESPACE}'")

    qdrant = AsyncQdrantClient(host=qdrant_host, port=qdrant_port)
    q_result = await migrate_qdrant_namespace(
        qdrant, collection, dry_run=args.dry_run
    )
    print(f"Qdrant: {q_result}")

    if not neo4j_password:
        print("NEO4J_PASSWORD not set — skipping Neo4j half. "
              "Re-run with credentials to complete the migration.",
              file=sys.stderr)
        return 1

    driver = AsyncGraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
    try:
        async with driver.session() as session:
            n_result = await migrate_neo4j_namespace(session, dry_run=args.dry_run)
        print(f"Neo4j: {n_result}")
    finally:
        await driver.close()

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
