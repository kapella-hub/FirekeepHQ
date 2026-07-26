"""One-off recovery: requeue memory:backfill:dlq entries onto the backfill stream.

For deployments that predate ``POST /ops/dlq/requeue`` (e.g. the office cluster
image that dead-lettered 52 memories while it had no embedding backend). Pops
the OLDEST DLQ records first and re-XADDs each to ``memory:backfill`` with
``attempts=0``, so the 60s ``drain_backfill_queue`` beat task re-attempts
embed + upsert against the (now working) backend. Safe to re-run: upserts are
idempotent (point ids are uuid5 of the text), and a malformed record is pushed
back and reported, never dropped.

Designed to run INSIDE a cortex container (redis package + REDIS_URL present),
without needing the script baked into the image — pipe it over stdin:

    # Kubernetes / Rancher kubectl shell
    kubectl exec -i deploy/firekeep-cortex-api -- python - \
        < cortex/scripts/requeue_backfill_dlq.py

    # Docker (personal VPS)
    docker exec -i firekeep-cortex-api python - \
        < cortex/scripts/requeue_backfill_dlq.py

Environment:
    REDIS_URL   (default: "redis://redis:6379/0" — cortex data DB)
    DLQ_LIMIT   (default: 10000 — max records to requeue in one run)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import redis

STREAM_KEY = "memory:backfill"
DLQ_KEY = "memory:backfill:dlq"


def main() -> int:
    url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    limit = int(os.environ.get("DLQ_LIMIT", "10000"))
    client = redis.Redis.from_url(url, decode_responses=True)

    initial_len = client.llen(DLQ_KEY)
    print(f"DLQ {DLQ_KEY}: {initial_len} record(s); requeueing up to {limit}")

    requeued = malformed = lost = 0
    for _ in range(min(limit, initial_len)):
        raw = client.rpop(DLQ_KEY)
        if raw is None:
            break
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            record = None
        if not isinstance(record, dict):
            # Restore is guarded: the popped record exists only in this
            # process — if the restore also fails, print it IN FULL so the
            # operator can re-add it by hand, never lose it silently.
            try:
                client.lpush(DLQ_KEY, raw)
            except Exception:
                lost += 1
                print(f"  RECORD LOST (restore failed) — re-add by hand:\n{raw}", file=sys.stderr)
                break
            malformed += 1
            print(f"  kept malformed record in DLQ: {raw[:120]!r}", file=sys.stderr)
            continue
        payload = record.get("payload") or "{}"
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        try:
            client.xadd(
                STREAM_KEY,
                {
                    "memory_id": str(record.get("memory_id", "")),
                    "text": str(record.get("text", "")),
                    "payload": payload,
                    "attempts": "0",
                    "next_attempt_at": "0",
                    "enqueued_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            requeued += 1
        except Exception as exc:
            try:
                client.lpush(DLQ_KEY, raw)
                print(f"  stream write failed, record restored — stopping: {exc}", file=sys.stderr)
            except Exception:
                lost += 1
                print(
                    f"  RECORD LOST (stream write and restore both failed: {exc}) "
                    f"— re-add by hand:\n{raw}",
                    file=sys.stderr,
                )
            break

    try:
        remaining = client.llen(DLQ_KEY)
        stream_len = client.xlen(STREAM_KEY)
    except Exception:
        remaining = stream_len = "unknown (redis unreachable)"
    print(
        f"done: requeued={requeued} malformed_kept={malformed} lost={lost} "
        f"dlq_remaining={remaining} stream_depth={stream_len}"
    )
    print("the 60s drain beat task will now re-embed; watch /ops/queues or /health")
    return 0 if malformed == 0 and lost == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
