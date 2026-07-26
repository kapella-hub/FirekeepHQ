"""One-time migration: promote nested ``metadata.memory_type`` to top-level (SP0, B2).

GC (``cortex/app/workers/gc.py``) reads ``memory_type`` from the top-level
Qdrant payload, but historical writes nested it under ``metadata`` — so every
old memory scored as unused-episodic and was eviction-eligible regardless of
its real type (defect #4). The write path now stores it top-level; this script
backfills existing points.

Idempotent: points that already carry a top-level ``memory_type`` are skipped.
Points with no ``memory_type`` anywhere are left alone (GC's half-life fallback
handles them). Supports ``--dry-run``.

Usage::

    QDRANT_HOST=localhost python -m cortex.scripts.promote_memory_type [--dry-run]

Environment:
    QDRANT_HOST       (default: "qdrant")
    QDRANT_PORT       (default: 6333)
    QDRANT_COLLECTION (default: "firekeep_memory")
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from qdrant_client import AsyncQdrantClient

BATCH_SIZE = 500


async def promote_memory_type(
    client, collection: str, dry_run: bool = False
) -> dict:
    """Copy nested metadata.memory_type to the top-level payload."""
    offset = None
    promoted = 0
    skipped = 0
    while True:
        batch, offset = await client.scroll(
            collection_name=collection,
            limit=BATCH_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not batch:
            break

        by_value: dict[str, list] = {}
        for point in batch:
            payload = point.payload or {}
            if payload.get("memory_type"):
                skipped += 1  # already promoted — idempotency guard
                continue
            nested = (payload.get("metadata") or {}).get("memory_type")
            if not nested:
                skipped += 1  # nothing to promote
                continue
            by_value.setdefault(nested, []).append(point.id)

        for value, ids in by_value.items():
            promoted += len(ids)
            if not dry_run:
                await client.set_payload(
                    collection_name=collection,
                    payload={"memory_type": value},
                    points=ids,
                )
            print(f"  {'would promote' if dry_run else 'promoted'} "
                  f"{len(ids)} point(s) -> memory_type={value!r}")

        if offset is None:
            break

    return {"promoted": promoted, "skipped": skipped, "dry_run": dry_run}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args()

    host = os.environ.get("QDRANT_HOST", "qdrant")
    port = int(os.environ.get("QDRANT_PORT", "6333"))
    collection = os.environ.get("QDRANT_COLLECTION", "firekeep_memory")

    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"[{mode}] Promoting metadata.memory_type in '{collection}' at {host}:{port}")

    client = AsyncQdrantClient(host=host, port=port)
    result = await promote_memory_type(client, collection, dry_run=args.dry_run)
    print(f"Done. {result}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
