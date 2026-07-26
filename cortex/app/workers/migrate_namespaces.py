"""One-time namespace migration task.

Normalizes all existing namespace values in both Qdrant and Neo4j
to match the new convention: lowercase, hyphens to underscores.

Usage:
    celery -A app.workers.sleep_cycle call app.workers.migrate_namespaces.migrate_namespaces
"""

from __future__ import annotations

import logging
from typing import Any

from app.models import normalize_namespace
from app.workers.sleep_cycle import celery_app
from app.config import get_settings

logger = logging.getLogger(__name__)


def _migrate_qdrant() -> dict[str, Any]:
    """Scroll all Qdrant points and update namespace payloads."""
    from qdrant_client import QdrantClient

    settings = get_settings()
    client = QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
    collection = settings.QDRANT_COLLECTION

    updated = 0
    errors = 0
    offset = None

    while True:
        result = client.scroll(
            collection_name=collection,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points, next_offset = result

        if not points:
            break

        for point in points:
            payload = point.payload or {}
            old_ns = payload.get("namespace", "default")
            new_ns = normalize_namespace(old_ns)
            if old_ns != new_ns:
                try:
                    client.set_payload(
                        collection_name=collection,
                        payload={"namespace": new_ns},
                        points=[point.id],
                    )
                    updated += 1
                    logger.info("Qdrant: %s -> %s (point %s)", old_ns, new_ns, point.id)
                except Exception as exc:
                    errors += 1
                    logger.error("Qdrant update failed for %s: %s", point.id, exc)

        if next_offset is None:
            break
        offset = next_offset

    logger.info("Qdrant migration complete: %d updated, %d errors", updated, errors)
    return {"updated": updated, "errors": errors}


def _migrate_neo4j() -> dict[str, Any]:
    """Merge duplicate Namespace nodes in Neo4j."""
    from neo4j import GraphDatabase

    settings = get_settings()
    driver = GraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )

    merged = 0
    errors = 0

    with driver.session() as session:
        # Get all namespace nodes
        result = session.run("MATCH (ns:Namespace) RETURN ns.name AS name")
        namespaces = [record["name"] for record in result if record["name"] is not None]

        # Group by normalized name
        groups: dict[str, list[str]] = {}
        for ns in namespaces:
            normalized = normalize_namespace(ns)
            groups.setdefault(normalized, []).append(ns)

        for normalized, originals in groups.items():
            if len(originals) == 1 and originals[0] == normalized:
                continue  # Already correct, no merge needed

            try:
                # Rename single namespace node
                if len(originals) == 1:
                    session.run(
                        "MATCH (ns:Namespace {name: $old_name}) "
                        "SET ns.name = $new_name",
                        old_name=originals[0],
                        new_name=normalized,
                    )
                    merged += 1
                    logger.info("Neo4j: renamed %s -> %s", originals[0], normalized)
                else:
                    # Multiple duplicates: merge all edges into one canonical node
                    # Keep or create the canonical node
                    session.run(
                        "MERGE (canonical:Namespace {name: $canonical_name})",
                        canonical_name=normalized,
                    )
                    for original in originals:
                        if original == normalized:
                            continue
                        # Move CONTAINS edges from duplicate to canonical
                        session.run(
                            "MATCH (dup:Namespace {name: $dup_name})-[r:CONTAINS]->(target) "
                            "MATCH (canonical:Namespace {name: $canonical_name}) "
                            "MERGE (canonical)-[:CONTAINS]->(target) "
                            "DELETE r",
                            dup_name=original,
                            canonical_name=normalized,
                        )
                        # Delete the duplicate node (DETACH handles any remaining edges)
                        session.run(
                            "MATCH (dup:Namespace {name: $dup_name}) "
                            "DETACH DELETE dup",
                            dup_name=original,
                        )
                        merged += 1
                        logger.info("Neo4j: merged %s into %s", original, normalized)
            except Exception as exc:
                errors += 1
                logger.error("Neo4j merge failed for %s: %s", normalized, exc)

    driver.close()
    logger.info("Neo4j migration complete: %d merged, %d errors", merged, errors)
    return {"merged": merged, "errors": errors}


@celery_app.task(name="app.workers.migrate_namespaces.migrate_namespaces")
def migrate_namespaces() -> dict[str, Any]:
    """One-time task: normalize all namespace values in Qdrant and Neo4j.

    Safe to run multiple times (idempotent).
    """
    logger.info("Starting namespace migration...")
    result: dict[str, Any] = {}

    try:
        result["qdrant"] = _migrate_qdrant()
    except Exception as exc:
        logger.error("Qdrant migration failed: %s", exc)
        result["qdrant"] = {"error": str(exc)}

    try:
        result["neo4j"] = _migrate_neo4j()
    except Exception as exc:
        logger.error("Neo4j migration failed: %s", exc)
        result["neo4j"] = {"error": str(exc)}

    has_errors = (
        "error" in result.get("qdrant", {})
        or "error" in result.get("neo4j", {})
    )
    result["status"] = "partial" if has_errors else "completed"

    logger.info("Namespace migration finished: %s", result)
    return result
