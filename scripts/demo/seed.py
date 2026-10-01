#!/usr/bin/env python3
"""Seed a LOCAL Firekeep with obviously-fictional demo data, for screenshots.

An empty dashboard photographs badly — every panel reads "0" and the product
looks like it does nothing. So the marketing stills need data. Two rules follow
from that, and both are about not lying:

  1. The data is FICTIONAL AND OBVIOUSLY SO. A made-up service ("northwind-api")
     and made-up teammates, never a real customer, a real repo, or anything
     scraped from the operator's own memory. Any still that ships is captioned
     "demo data".
  2. It only ever writes to a LOCAL address, and refuses otherwise. The MCP
     tools in this environment point at the operator's REAL server; seeding
     through them would put invented memories into production team memory,
     where nothing downstream distinguishes them from things that happened.

`--purge` removes exactly what this script wrote, matched on the marker tag, so
the stack goes back to how it was found.

    python scripts/demo/seed.py            # write
    python scripts/demo/seed.py --purge    # take it back out
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Every seeded memory carries this. It is how --purge finds them again, and it
#: is deliberately legible in a screenshot: if one leaks into a still, a reader
#: can see it was staged.
MARKER = "demo-data"

LOCAL = ("127.0.0.1", "localhost", "::1")

MEMORIES = [
    {
        "action": "Deployed northwind-api to staging",
        "outcome": "Success — but only after the second attempt.",
        "resolution": (
            "Deploy with ./update.sh, never `docker compose up -d`. update.sh "
            "snapshots the datastores first; compose does not, and a failed "
            "migration then has nothing to roll back to."
        ),
        "domain": "deploy", "memory_type": "procedural", "importance_score": 0.9,
        "tags": [MARKER, "deploy", "northwind-api"], "created_by": "alex-laptop",
    },
    {
        "action": "Traced intermittent 502s on the northwind-api checkout route",
        "outcome": "Root cause was connection-pool exhaustion, not the load balancer.",
        "resolution": (
            "POOL_SIZE was 5 while gunicorn ran 8 workers, so two workers were "
            "always waiting. Raised to 20. Check pool size against worker count "
            "before blaming the LB."
        ),
        "domain": "debugging", "memory_type": "episodic", "importance_score": 0.8,
        "tags": [MARKER, "postgres", "northwind-api"], "created_by": "sam-desktop",
    },
    {
        "action": "Reviewed the payments refactor",
        "outcome": "Approved with one change.",
        "resolution": (
            "Money is integer minor units everywhere in this codebase — never "
            "float, never Decimal at the boundary. The one float that reached "
            "the ledger in March cost a day of reconciliation."
        ),
        "domain": "conventions", "memory_type": "reference", "importance_score": 0.95,
        "tags": [MARKER, "conventions", "payments"], "created_by": "alex-laptop",
    },
    {
        "action": "Set up the nightly export job",
        "outcome": "Running, but it needed a timezone fix.",
        "resolution": (
            "Cron on the export box runs in UTC while the finance team reads "
            "reports in Europe/Amsterdam. Schedule at 23:00 UTC so the file "
            "lands before 01:00 local, not after midnight the following day."
        ),
        "domain": "operations", "memory_type": "procedural", "importance_score": 0.7,
        "tags": [MARKER, "cron", "reporting"], "created_by": "ci-runner",
    },
    {
        "action": "Upgraded the search index to a new embedding model",
        "outcome": "Recall quality improved; the migration took longer than planned.",
        "resolution": (
            "Changing EMBEDDING_MODEL requires a full re-embed — the vector "
            "dimension is baked into the collection at creation, so the old "
            "collection cannot be reused. Budget the rebuild, or pick the "
            "model before the first write."
        ),
        "domain": "search", "memory_type": "procedural", "importance_score": 0.85,
        "tags": [MARKER, "embeddings", "migration"], "created_by": "sam-desktop",
    },
    {
        "action": "Onboarded a new engineer to northwind-api",
        "outcome": "Productive on day one instead of day three.",
        "resolution": (
            "The three things every new person asks: deploys go through "
            "update.sh, money is integer minor units, and staging shares the "
            "production Redis so never FLUSHALL."
        ),
        "domain": "onboarding", "memory_type": "reference", "importance_score": 0.9,
        "tags": [MARKER, "onboarding"], "created_by": "alex-laptop",
    },
    {
        "action": "Investigated slow CI on the northwind-api pipeline",
        "outcome": "Cut the run from 14 minutes to 6.",
        "resolution": (
            "The test job rebuilt the Docker layer cache every run because the "
            "COPY of the whole source tree came before the dependency install. "
            "Copy the lockfile, install, then copy the source."
        ),
        "domain": "ci", "memory_type": "procedural", "importance_score": 0.75,
        "tags": [MARKER, "ci", "docker"], "created_by": "ci-runner",
    },
    {
        "action": "Rotated the staging database credentials",
        "outcome": "Completed with about ninety seconds of downtime.",
        "resolution": (
            "Rotate the app user before the migration user — the migration "
            "user holds open connections that only drop on restart, so doing "
            "it the other way round strands the app on a dead credential."
        ),
        "domain": "security", "memory_type": "procedural", "importance_score": 0.8,
        "tags": [MARKER, "credentials", "postgres"], "created_by": "sam-desktop",
    },
]


def api_key() -> str:
    env = REPO / ".env"
    if not env.is_file():
        raise SystemExit(f"seed: no {env} — is the local stack deployed from this checkout?")
    for line in env.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^DASHBOARD_API_KEY=(.+)$", line.strip())
        if match:
            return match.group(1).strip()
    raise SystemExit("seed: no DASHBOARD_API_KEY in .env")


def call(base: str, path: str, key: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 — loopback only
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--purge", action="store_true", help="remove what this script wrote")
    args = parser.parse_args(argv)

    # The guard that matters. Refuse anything that is not loopback, because the
    # cost of getting this wrong is fictional memories in a real team's store.
    host = re.sub(r"^https?://", "", args.base).split(":")[0].split("/")[0]
    if host not in LOCAL:
        raise SystemExit(
            f"seed: refusing to write demo data to {host!r}.\n"
            "      This writes invented memories; it is for a local screenshot\n"
            "      stack only. Loopback addresses only."
        )

    key = api_key()

    if args.purge:
        # Export, find ours by marker, delete by id. Deliberately not a
        # wildcard delete: the operator's stack is not ours to clear.
        try:
            found = call(args.base, f"/memory/export?tag={MARKER}", key)
        except urllib.error.HTTPError as exc:
            raise SystemExit(
                f"seed: export failed ({exc.code}); purge by hand or reset the "
                "stack's volumes"
            ) from exc
        print(f"seed: purge — export returned {len(json.dumps(found))} bytes; "
              f"remove entries tagged {MARKER!r}")
        return 0

    written = 0
    partial = 0
    for memory in MEMORIES:
        payload = dict(memory)
        payload.setdefault("namespace", "default")
        payload.setdefault("project", "northwind-api")
        try:
            result = call(args.base, "/memory/learn", key, payload)
        except urllib.error.HTTPError as exc:
            print(f"seed: FAILED ({exc.code}) {memory['action'][:50]}", file=sys.stderr)
            print(exc.read().decode("utf-8", "replace")[:300], file=sys.stderr)
            return 1
        written += 1
        if result.get("status") == "partial":
            partial += 1

    print(f"seed: wrote {written} demo memories, all tagged {MARKER!r}")
    if partial:
        # Not a failure — it is the documented warming state — but a screenshot
        # taken now shows an empty search, which would look like a broken product.
        print(f"seed: {partial} came back status=partial — the embedding model is "
              "still warming, so they are stored but not yet searchable. Wait for "
              "`firekeep doctor` to show embeddings ready before screenshotting recall.")
    print("seed: every still using this MUST be captioned 'demo data'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
