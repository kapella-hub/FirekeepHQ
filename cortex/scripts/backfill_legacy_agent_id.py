"""One-time backfill: tag pre-team-continuity Qdrant points with a legacy sentinel.

Scrolls all points in the memory collection. For any point where the payload is
missing ``agent_id`` or has ``agent_id="unknown"`` with a ``timestamp``/``created_at``
before today's UTC date, sets ``agent_id="legacy-pre-team-continuity"``.

Idempotent: once tagged, a re-run will skip the point because ``agent_id`` is no
longer ``None`` or ``"unknown"``.

Usage::

    QDRANT_HOST=localhost QDRANT_COLLECTION=firekeep_memory \
        python -m cortex.scripts.backfill_legacy_agent_id

Environment:
    QDRANT_HOST       (default: "qdrant")
    QDRANT_PORT       (default: 6333)
    QDRANT_COLLECTION (default: "firekeep_memory")
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

from qdrant_client import AsyncQdrantClient

LEGACY_SENTINEL = "legacy-pre-team-continuity"
BATCH_SIZE = 500


async def main() -> int:
    host = os.environ.get("QDRANT_HOST", "qdrant")
    port = int(os.environ.get("QDRANT_PORT", "6333"))
    collection = os.environ.get("QDRANT_COLLECTION", "firekeep_memory")
    client = AsyncQdrantClient(host=host, port=port)

    cutoff = datetime.now(timezone.utc).date().isoformat()
    print(
        f"Backfilling agent_id on points in '{collection}' "
        f"at {host}:{port} (cutoff: created before {cutoff})"
    )

    offset = None
    updated = 0
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

        to_update_ids = []
        for point in batch:
            payload = point.payload or {}
            aid = payload.get("agent_id")
            ts = payload.get("timestamp") or payload.get("created_at") or ""
            # Only backfill records that look pre-team-continuity:
            #   - no agent_id at all, OR
            #   - agent_id == "unknown" AND created before today's UTC date.
            # The cutoff guard makes the script idempotent for old data while
            # leaving today's genuinely-untagged writes (e.g. unmigrated callers)
            # alone — those should be fixed at the source, not retroactively tagged.
            if aid is None or (aid == "unknown" and (ts == "" or ts < cutoff)):
                to_update_ids.append(point.id)
            else:
                skipped += 1

        if to_update_ids:
            await client.set_payload(
                collection_name=collection,
                payload={"agent_id": LEGACY_SENTINEL},
                points=to_update_ids,
            )
            updated += len(to_update_ids)
            print(f"  +{len(to_update_ids)} (running total: {updated})")

        if offset is None:
            break

    print(f"Done. Updated {updated}, skipped {skipped}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
