"""The identity-v2 freeze migration (spec D6). Ships INERT.

Nothing in this module runs at import, at startup, or on deploy. There is no
Celery task, no beat entry, and no lifespan hook — it is invoked by a human,
inside a maintenance freeze, as::

    python -m app.workers.memory_identity_migration <subcommand>

with subcommands ``dry-run``, ``execute``, ``mark-flipped``, ``graph-remap``,
``fold-hashes``, ``verify`` and ``resume``. Deploying the code is not consent
to migrate; the live-store run happens only on a separate, explicit user go,
after the dry-run report has been reviewed.

WHAT IT DOES. Learned-memory identity was ``uuid5(text)``, so identical text in
two workspaces landed on ONE point: workspace A's memory left A's recall and
B gained it, with A's provenance riding along underneath B's ownership. D1
re-keys memories to ``memory_point_id(workspace_id, namespace, text)``. Every
existing point therefore has to move, and this module is what moves them —
into a NEW collection (``firekeep_memory_v2``) that then becomes the
configured name, because Qdrant refuses an alias colliding with an existing
collection and has no rename, so the "old name becomes an alias" cut-over was
disproven empirically in review.

WHY IT IS SHAPED LIKE THIS. Every guard below is a specific disproven failure
mode, not defensive habit:

* **The freeze is load-bearing.** A no-freeze scroll-copy of a live collection
  loses roughly half of the concurrent writes — new ids distribute uniformly
  while the scroll cursor advances in id order — and races every in-place
  mutator (gc, memory_agent, owm, contradiction supersession, access-count
  flushes) with no constructible catch-up. So ``execute`` refuses unless
  ``MIGRATION_FREEZE=true``, and the source ``points_count`` recorded at the
  start is a fingerprint every later step re-checks: if the number moved, the
  freeze leaked and the run aborts rather than producing a copy nobody can
  reason about.

* **Classification is provenance-first.** Two real shapes defeat the id
  predicate in opposite directions. Legacy corpus chunks (pre-65606df) have
  ids that ARE ``uuid5(text)`` while being corpus, whose identity is
  source-scoped and must not be re-keyed. Mojibake-repaired memories (~19 on
  the live store) have ids matching NEITHER scheme, because the text was
  edited after minting — which disproves "payload text never diverges from
  minting text". So ``source``/``memory_type`` decide first and the id
  predicate only confirms.

* **Twin merge is order-independent by construction.** The D5 compat window
  lets a relearn create a v2 point beside its v1 original. Both want the same
  target id. Rather than depend on which one the scroll reaches first, the
  plan groups them and ``merge_target_group`` folds them once, deterministically
  — v2 text and vector win, ``_merge_lifecycle`` carries status, counters and
  archive provenance across.

* **The map is built before anything is written.** Classification is a pure
  function of the payload, so the whole old->new map is derivable in one
  read-only pass. Reference fields (``superseded_by``, ``contested_with``) are
  then rewritten during the copy against a COMPLETE map, which no
  rewrite-as-you-go ordering could guarantee.

* **Everything unrecognised is copied, never dropped.** A payload shape nobody
  predicted lands in ``BUCKET_UNCLASSIFIED``, is copied verbatim at its own id,
  and is counted in the report. Losing a memory is worse than failing to
  improve it.

Order is enforced by a Redis state machine (``mem:migration:v2:state``): step
N+1 refuses until step N is marked complete. The FLIP itself is an operator
act (``QDRANT_COLLECTION=firekeep_memory_v2`` plus a container recreate) that
this tool cannot perform, only record — and ``mark-flipped`` refuses to record
it unless the running process's own settings prove it happened.

See ``docs/superpowers/specs/2026-08-27-memory-identity-v2-design.md`` D6 for
the design and ``docs/guides/backup-and-restore.md`` for the runbook.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from qdrant_client.models import (
    OptimizersConfigDiff,
    PayloadSchemaType,
    PointStruct,
    SearchParams,
)

from app.config import Settings, get_settings
from app.db.vector import _merge_lifecycle, _v1_point_id, memory_point_id
from app.models import normalize_namespace
from app.workspace_migration import QUARANTINE_WORKSPACE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Names and keys
# ---------------------------------------------------------------------------

#: The new canonical collection. NOT an alias of the old name -- Qdrant v1.13.2
#: refuses an alias whose name collides with an existing collection, has no
#: rename, and `VectorClient.initialize()` would try to create a collection at
#: an alias name and abort startup. Both facts were verified against a live
#: instance during review; the alias cut-over is disproven, not deferred.
SHADOW_COLLECTION = "firekeep_memory_v2"

#: The state machine. One Redis hash, read by every step before it acts.
STATE_KEY = "mem:migration:v2:state"

#: The old->new id map, mirrored from the JSONL artifact. A CACHE, never the
#: source of truth: `owm.py` (D7) reads it to translate historical replay
#: events, and treats an empty/absent hash as "unavailable", not as "nothing
#: moved" -- the difference between a degraded join and wiping every migrated
#: memory's efficacy score.
IDMAP_REDIS_KEY = "mem:idmap:v2"

#: Written only by a verify pass that fully succeeded. `owm.py` reads its
#: presence to decide whether a missing idmap means "pre-migration deploy"
#: (normal) or "expired cache" (skip the stale-reset sweep, loudly).
MIGRATION_COMPLETE_KEY = "mem:migration:v2:complete"

#: The durable form of the map. `cortex-api` mounts `./backups` (read-only, for
#: `GET /ops/backups`) and nothing else writable, so this default sits beside
#: the freeze-start cold backup that is the run's restore point -- deliberately
#: the same directory an operator already treats as durable. The mount is `:ro`
#: in `docker-compose.yml`, so the operator either remounts it writable for the
#: window or passes `--idmap-path`; `execute` checks writability UP FRONT and
#: says which, rather than discovering it after writing half a collection.
DEFAULT_IDMAP_PATH = "/backups/mem-idmap-v2.jsonl"

#: Recall bookkeeping, folded through the map post-flip. Duplicated from
#: `app/workers/gc.py` and `app/skills/api.py` rather than imported, because
#: importing either pulls a Celery app into a tool that must stay inert.
ACCESS_COUNTS_KEY = "memory:access_counts"
LAST_RECALLED_KEY = "memory:last_recalled"

#: Point-id-valued payload fields. Both are user-rendered (rag.py:1435,1441)
#: and autopilot-read, so a stale reference is visible, not merely internal.
REFERENCE_FIELDS = ("superseded_by", "contested_with")

#: The payload indexes `VectorClient` and the filtered read paths rely on.
#: `workspace_id` is the tenancy `must` on every search; it is created here
#: explicitly because `initialize()` only creates the first two.
PAYLOAD_INDEX_FIELDS = ("tags", "namespace", "workspace_id")

#: Fields per Redis command. Both the idmap mirror and the hash folds move
#: six-figure field counts at this store's scale, and one command carrying all
#: of them blocks the single-threaded instance the rest of the freeze runs on.
_REDIS_BATCH = 1000

#: Relationship types a MemoryRef participates in, from every write site:
#: SUPERSEDES (memory_agent.py:476,730), BACKLINK (graph.py:1155,1160) and
#: RELATES_TO (memory_agent.py:948). Enumerated because plain Cypher cannot
#: parameterise a relationship type and this deployment has no APOC.
_MEMORY_REF_REL_TYPES = ("SUPERSEDES", "BACKLINK", "RELATES_TO")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

STEP_COPY = "copy"
STEP_FLIPPED = "flipped"
STEP_GRAPH_REMAP = "graph_remap"
STEP_HASH_FOLD = "hash_fold"
STEP_VERIFY = "verify"

#: Execution order. `_require_step_complete` walks it; nothing may skip.
STEP_ORDER = (STEP_COPY, STEP_FLIPPED, STEP_GRAPH_REMAP, STEP_HASH_FOLD, STEP_VERIFY)

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETE = "complete"

#: Cursor sentinels. The scroll cursor is a Qdrant point id while the copy is
#: walking the source; these two mark the phases after it, so a crash in the
#: group-merge phase resumes there instead of re-walking a finished scroll.
CURSOR_GROUPS = "__groups__"
CURSOR_DONE = "__done__"


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

BUCKET_CORPUS = "corpus"
BUCKET_DREAM = "dream"
BUCKET_PROFILE = "profile"
BUCKET_SKILL = "skill"
BUCKET_V2 = "v2"
BUCKET_MIGRATABLE = "v1_migratable"
BUCKET_REPAIRED = "repaired_text"
BUCKET_QUARANTINE = "quarantine"
BUCKET_UNCLASSIFIED = "unclassified"

ALL_BUCKETS = (
    BUCKET_CORPUS, BUCKET_DREAM, BUCKET_PROFILE, BUCKET_SKILL, BUCKET_V2,
    BUCKET_MIGRATABLE, BUCKET_REPAIRED, BUCKET_QUARANTINE, BUCKET_UNCLASSIFIED,
)

#: Buckets whose points are re-keyed. Everything else keeps its id.
REKEYED_BUCKETS = frozenset({BUCKET_MIGRATABLE, BUCKET_REPAIRED})

#: Buckets whose id is derived from (workspace, namespace, text) and whose
#: payload must therefore carry the NORMALIZED namespace, or the D1 invariant
#: "the id is derivable from the payload" stops holding.
MEMORY_SCHEME_BUCKETS = frozenset({BUCKET_V2, BUCKET_MIGRATABLE, BUCKET_REPAIRED})


class MigrationRefused(RuntimeError):
    """A precondition failed. Nothing was written; nothing needs undoing."""


@dataclass(frozen=True)
class Classification:
    """One point's verdict. ``new_id == old_id`` means "stays where it is"."""

    bucket: str
    old_id: str
    new_id: str
    reason: str


@dataclass
class CopyPoint:
    """A point as it will be written into the shadow collection."""

    id: str
    vector: Any
    payload: dict


# ---------------------------------------------------------------------------
# Classification -- pure, provenance-first
# ---------------------------------------------------------------------------


def classify(point: Any) -> Classification:
    """Bucket one point by PROVENANCE, with the id predicate only confirming.

    Accepts anything carrying ``.id`` and ``.payload`` (a Qdrant ``Record``,
    a ``PointStruct``, or a test double).

    The order of the checks is the whole design, and it is the order review
    forced:

    1. ``source == "corpus"`` FIRST. Legacy corpus chunks written before
       65606df have ids that ARE ``uuid5(text)``, so an id-shape classifier
       calls them migratable and re-keys them — breaking corpus's
       source-scoped identity, under which deleting one member's source used
       to delete another member's identical chunk.
    2. dream / profile / skill, by their own provenance. They mint through
       different namespaces entirely and must be copied untouched.
    3. The quarantine sentinel, BEFORE the workspace check, so re-classifying
       an already-migrated store is stable: a quarantine point carries a
       truthy (sentinel) workspace_id and a v1-shaped id, which would
       otherwise read as "migratable" on a second pass. Mirrors
       ``workspace_migration._is_quarantined`` — either stamp is sufficient.
    4. A falsy workspace_id quarantines. There is no way to guess who owned
       an unattributed memory, and guessing is the defect this whole change
       exists to remove.
    5. Only now does the id decide, and only between v2 / v1 / repaired.

    An absent ``namespace`` key reads as ``"default"``, matching
    ``namespace_condition``'s legacy semantics: points written before the
    field existed belong to the default category, not to no category.
    """
    pid = str(point.id)
    payload = getattr(point, "payload", None) or {}

    source = payload.get("source")
    if source == "corpus":
        return Classification(BUCKET_CORPUS, pid, pid, "source == 'corpus'")
    if source == "dream":
        return Classification(BUCKET_DREAM, pid, pid, "source == 'dream'")
    if source == "dream_profile":
        return Classification(BUCKET_PROFILE, pid, pid, "source == 'dream_profile'")
    if payload.get("memory_type") == "skill":
        return Classification(BUCKET_SKILL, pid, pid, "memory_type == 'skill'")

    workspace_id = payload.get("workspace_id")
    if workspace_id == QUARANTINE_WORKSPACE or payload.get("legacy_unscoped") is True:
        return Classification(BUCKET_QUARANTINE, pid, pid, "already quarantined")

    text = payload.get("text")
    if not isinstance(text, str) or not text:
        return Classification(
            BUCKET_UNCLASSIFIED, pid, pid, "no memory text to re-home from"
        )

    if not workspace_id:
        return Classification(BUCKET_QUARANTINE, pid, pid, "no verified workspace_id")

    namespace = normalize_namespace(payload.get("namespace") or "default")
    v2_id = memory_point_id(workspace_id, namespace, text)
    if pid == v2_id:
        return Classification(BUCKET_V2, pid, pid, "id already matches memory_point_id")
    if pid == _v1_point_id(text):
        return Classification(BUCKET_MIGRATABLE, pid, v2_id, "id is uuid5(text)")
    return Classification(
        BUCKET_REPAIRED, pid, v2_id, "id matches neither scheme; re-homed from payload"
    )


def _reference_ids(value: Any) -> list[str]:
    """The point ids a reference field names. Scalar today, list-tolerant."""
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and v]
    return []


def rewrite_references(payload: dict, mapping: dict[str, str]) -> dict:
    """Return a copy of ``payload`` with reference fields mapped old->new.

    Unmapped ids pass through unchanged — corpus, dream, skill and already-v2
    ids never appear in the map, and neither does a reference that was already
    dangling before the migration (which must stay dangling: inventing a
    target would be worse than the honest break the verify pass baselines).
    """
    out = dict(payload)
    for field_name in REFERENCE_FIELDS:
        value = out.get(field_name)
        if isinstance(value, str) and value:
            out[field_name] = mapping.get(value, value)
        elif isinstance(value, (list, tuple)):
            out[field_name] = [
                mapping.get(v, v) if isinstance(v, str) else v for v in value
            ]
    return out


def prepared_payload(bucket: str, payload: dict, mapping: dict[str, str]) -> dict:
    """The payload as written into the shadow, for one bucket.

    Quarantine gains the sentinel workspace and the ``legacy_unscoped`` stamp:
    a workspace no principal holds makes the point uniformly invisible to BOTH
    recall legs (the vector leg filters ``workspace_id`` as a hard ``must``;
    the graph leg denies ``legacy_unscoped`` in ``_scope_verdict``), and
    ``backfill_memories`` skips it so the next restart cannot silently adopt
    the whole bucket into the deployment workspace.

    Memory-scheme buckets get the NORMALIZED namespace, because the id was
    minted over the normalized value and D1 requires the id to stay derivable
    from the payload.
    """
    out = rewrite_references(payload, mapping)
    if bucket == BUCKET_QUARANTINE:
        out["workspace_id"] = QUARANTINE_WORKSPACE
        out["legacy_unscoped"] = True
    elif bucket in MEMORY_SCHEME_BUCKETS:
        out["namespace"] = normalize_namespace(out.get("namespace") or "default")
    return out


def merge_target_group(
    target: str, members: Sequence[Any], mapping: dict[str, str]
) -> CopyPoint:
    """Fold every point destined for one id into a single deterministic point.

    The ordinary case is the D5 twin: a v1 point and the v2 point a post-deploy
    relearn created from the same text. ``_merge_lifecycle(v1 as existing, v2 as
    fresh)`` is the spec's rule — v2 text and vector win; created_at, agent_id,
    project, counters (max), status and archive provenance survive from the v1
    original, so the migration cannot resurrect an archived memory as active.

    WINNER RULE, and why it is not "whichever we saw first": the member sitting
    AT the target id is the winner if one exists (that is the v2 twin); failing
    that, the lexicographically smallest old id wins. Remaining members fold in
    ascending id order. The result is a pure function of the member set, so the
    scroll order — which Qdrant does not promise — cannot change what is
    stored. Both orders are asserted in the tests.
    """
    ordered = sorted(members, key=lambda m: str(m.id))
    winner = next((m for m in ordered if str(m.id) == target), ordered[0])
    payload = dict(winner.payload or {})
    for other in ordered:
        if other is winner:
            continue
        payload = _merge_lifecycle(dict(other.payload or {}), payload)
    return CopyPoint(
        id=target,
        vector=winner.vector,
        payload=prepared_payload(BUCKET_V2, payload, mapping),
    )


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass
class MigrationPlan:
    """The dry run's whole output: what will move, where, and what conflicts."""

    source_collection: str
    source_points_count: int
    generated_at: str
    counts: dict[str, int]
    mapping: dict[str, str]
    #: target id -> the source ids that will be folded into it (>1 member only)
    target_groups: dict[str, list[str]]
    #: predicted v2 ids that already exist in the source, for any reason
    occupied_targets: list[str]
    #: occupancy that is NOT a legitimate v2 twin -- execute refuses on these
    conflicts: list[dict[str, str]]
    #: [owner_id, field, referenced_id] triples already broken before the run
    dangling_baseline: list[list[str]]
    examples: dict[str, list[str]]
    parity_probe_ids: list[str]
    fidelity_sample_ids: list[str]
    collapsed: int
    expected_shadow_counts: dict[str, int]
    expected_shadow_total: int

    def to_dict(self) -> dict:
        return {
            "source_collection": self.source_collection,
            "source_points_count": self.source_points_count,
            "generated_at": self.generated_at,
            "counts": self.counts,
            "target_groups": self.target_groups,
            "occupied_targets": self.occupied_targets,
            "conflicts": self.conflicts,
            "dangling_baseline": self.dangling_baseline,
            "examples": self.examples,
            "parity_probe_ids": self.parity_probe_ids,
            "fidelity_sample_ids": self.fidelity_sample_ids,
            "collapsed": self.collapsed,
            "expected_shadow_counts": self.expected_shadow_counts,
            "expected_shadow_total": self.expected_shadow_total,
        }

    @classmethod
    def from_dict(cls, data: dict, mapping: dict[str, str] | None = None) -> "MigrationPlan":
        """Rebuild from the artifact.

        ``mapping`` is carried separately (the JSONL) rather than inside the
        JSON summary: at this store's scale it is six figures of entries, and
        the summary is meant to be read by a human before they authorise a
        run.
        """
        return cls(
            source_collection=data["source_collection"],
            source_points_count=data["source_points_count"],
            generated_at=data["generated_at"],
            counts=dict(data["counts"]),
            mapping=dict(mapping or {}),
            target_groups={k: list(v) for k, v in data["target_groups"].items()},
            occupied_targets=list(data["occupied_targets"]),
            conflicts=[dict(c) for c in data["conflicts"]],
            dangling_baseline=[list(x) for x in data["dangling_baseline"]],
            examples={k: list(v) for k, v in data["examples"].items()},
            parity_probe_ids=list(data["parity_probe_ids"]),
            fidelity_sample_ids=list(data["fidelity_sample_ids"]),
            collapsed=data["collapsed"],
            expected_shadow_counts=dict(data["expected_shadow_counts"]),
            expected_shadow_total=data["expected_shadow_total"],
        )


def _stride_sample(candidates: Sequence[str], n: int) -> list[str]:
    """A deterministic spread of up to ``n`` ids over a sorted candidate list.

    Deliberately not ``random.sample``: ``execute`` re-derives the plan from
    the live store and refuses if it disagrees with the reviewed dry run, so a
    sample that differed between two derivations of the same store would turn
    a correctness guard into a coin flip. Striding sorted uuids is uncorrelated
    with content — which is all "random" was ever buying here — and it is
    reproducible from the store alone, with no seed to carry.
    """
    pool = sorted(candidates)
    if n <= 0 or not pool:
        return []
    if len(pool) <= n:
        return pool
    step = len(pool) / n
    return [pool[int(i * step)] for i in range(n)]


class _PlanAccumulator:
    """Builds a plan one point at a time, holding ids rather than payloads.

    The point of the incremental shape: at this store's scale the source is
    six figures of points whose payloads are the bulk of the bytes, and a plan
    only ever needs each point's id, bucket and reference edges. Accumulating
    keeps the tool's footprint proportional to the ID count (tens of MB)
    instead of to the whole collection.
    """

    def __init__(self) -> None:
        self.counts = {bucket: 0 for bucket in ALL_BUCKETS}
        self.mapping: dict[str, str] = {}
        self.all_ids: set[str] = set()
        self.bucket_of: dict[str, str] = {}
        self.ref_edges: list[tuple[str, str, str]] = []
        self.examples: dict[str, list[str]] = {b: [] for b in ALL_BUCKETS}

    def add(self, point: Any) -> Classification:
        verdict = classify(point)
        payload = getattr(point, "payload", None) or {}
        self.all_ids.add(verdict.old_id)
        self.bucket_of[verdict.old_id] = verdict.bucket
        self.counts[verdict.bucket] += 1
        if verdict.new_id != verdict.old_id:
            self.mapping[verdict.old_id] = verdict.new_id
        for field_name in REFERENCE_FIELDS:
            for ref in _reference_ids(payload.get(field_name)):
                self.ref_edges.append((verdict.old_id, field_name, ref))
        if len(self.examples[verdict.bucket]) < 3:
            self.examples[verdict.bucket].append(verdict.old_id)
        return verdict

    def build(self, *, source_collection: str, source_points_count: int,
              parity_n: int, fidelity_n: int) -> MigrationPlan:
        # Occupancy. A predicted v2 id that already exists in the source is
        # either the D5 twin (legitimate -- merge), a point that is ITSELF
        # leaving (the contrived case the spec names: a v1 memory whose literal
        # text is shaped like a v2 seed, so uuid5(text) lands on some other
        # memory's predicted id -- it vacates, and there is nothing to merge),
        # or a genuine conflict with something that is not a memory at all.
        # Only the first is merged; the third refuses, because folding a corpus
        # chunk into a memory would destroy both.
        groups: dict[str, list[str]] = {}
        for old, new in sorted(self.mapping.items()):
            groups.setdefault(new, []).append(old)

        occupied_targets = sorted(t for t in groups if t in self.all_ids)
        conflicts: list[dict[str, str]] = []
        for target in occupied_targets:
            if target in self.mapping:
                continue  # vacating: it is being re-keyed away from this id
            if self.bucket_of[target] == BUCKET_V2:
                groups[target].append(target)  # the D5 twin joins its own group
                continue
            conflicts.append({
                "target": target,
                "tenant_bucket": self.bucket_of[target],
                "incoming": ",".join(groups[target]),
            })

        target_groups = {
            target: sorted(members)
            for target, members in groups.items()
            if len(members) > 1
        }
        collapsed = sum(len(members) - 1 for members in target_groups.values())
        grouped_members = {m for members in target_groups.values() for m in members}

        dangling = sorted(
            [owner, field_name, ref]
            for owner, field_name, ref in self.ref_edges
            if ref not in self.all_ids
        )

        expected = dict(self.counts)
        expected[BUCKET_V2] = (
            self.counts[BUCKET_V2] + self.counts[BUCKET_MIGRATABLE]
            + self.counts[BUCKET_REPAIRED] - collapsed
        )
        expected[BUCKET_MIGRATABLE] = 0
        expected[BUCKET_REPAIRED] = 0

        return MigrationPlan(
            source_collection=source_collection,
            source_points_count=source_points_count,
            generated_at=datetime.now(timezone.utc).isoformat(),
            counts=dict(self.counts),
            mapping=dict(self.mapping),
            target_groups=target_groups,
            occupied_targets=occupied_targets,
            conflicts=conflicts,
            dangling_baseline=dangling,
            examples={k: list(v) for k, v in self.examples.items()},
            parity_probe_ids=_stride_sample(sorted(self.all_ids), parity_n),
            # Fidelity compares a copied point field-by-field against its
            # source, so a merged point -- whose payload legitimately differs
            # from both parents -- is excluded rather than special-cased.
            fidelity_sample_ids=_stride_sample(
                [old for old in self.mapping if old not in grouped_members],
                fidelity_n,
            ),
            collapsed=collapsed,
            expected_shadow_counts=expected,
            expected_shadow_total=source_points_count - collapsed,
        )


def build_plan(
    points: Iterable[Any],
    *,
    source_collection: str,
    source_points_count: int,
    parity_n: int = 50,
    fidelity_n: int = 100,
) -> MigrationPlan:
    """Classify every point and work out exactly what the copy will do.

    Pure: no I/O, no clients, no store mutation. ``execute`` runs this over a
    fresh scroll and compares the result with the reviewed dry run's, so any
    drift in the store between review and execution is caught before a single
    point is written.
    """
    accumulator = _PlanAccumulator()
    for point in points:
        accumulator.add(point)
    return accumulator.build(
        source_collection=source_collection,
        source_points_count=source_points_count,
        parity_n=parity_n, fidelity_n=fidelity_n,
    )


async def scan_plan(client, source: str, *, batch_size: int = 256) -> MigrationPlan:
    """``build_plan`` over a streamed scroll — the whole store, never in memory."""
    accumulator = _PlanAccumulator()
    async for batch, _ in _scroll(client, source, batch_size=batch_size):
        for record in batch:
            accumulator.add(record)
    return accumulator.build(
        source_collection=source,
        source_points_count=await _points_count(client, source),
        parity_n=50, fidelity_n=100,
    )


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def plan_path(idmap_path: str) -> str:
    """The dry-run summary sits beside the map it describes."""
    return f"{idmap_path}.plan.json"


def _write_atomic(path: str, text: str) -> None:
    """Write via a temp file + replace: a half-written map is worse than none."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)


def write_idmap(idmap_path: str, mapping: dict[str, str]) -> None:
    """Write the durable JSONL map (one ``{"old","new"}`` object per line)."""
    body = "".join(
        json.dumps({"old": old, "new": new}, separators=(",", ":")) + "\n"
        for old, new in sorted(mapping.items())
    )
    _write_atomic(idmap_path, body)


def read_idmap(idmap_path: str) -> dict[str, str]:
    """Read the durable map. The file is the source of truth, Redis a cache."""
    path = Path(idmap_path)
    if not path.exists():
        raise MigrationRefused(
            f"identity map artifact missing at {idmap_path} — it is the durable "
            "form of the migration and every later step reads it"
        )
    mapping: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            mapping[row["old"]] = row["new"]
    return mapping


def _assert_writable(path: str) -> None:
    """Fail before the copy, not during it.

    `cortex-api` mounts `./backups` READ-ONLY. Discovering that after writing
    half a collection leaves an operator inside a freeze with a partial shadow
    and no map; discovering it here costs nothing.
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        probe = target.with_suffix(target.suffix + ".probe")
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise MigrationRefused(
            f"identity map path {path} is not writable ({exc}). The container's "
            "./backups mount is read-only by default: remount it writable for "
            "the migration window, or pass --idmap-path to a writable volume."
        ) from exc


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


def _text(value: Any) -> Any:
    """Redis returns bytes unless the client decodes; `main.py` does not."""
    return value.decode("utf-8") if isinstance(value, bytes) else value


async def read_state(redis_client) -> dict[str, str]:
    """The recorded state, or ``{}`` before any run has started."""
    raw = await redis_client.hgetall(STATE_KEY)
    return {_text(k): _text(v) for k, v in (raw or {}).items()}


async def _write_state(redis_client, **fields) -> None:
    await redis_client.hset(STATE_KEY, mapping={k: str(v) for k, v in fields.items()})


async def _require_step_complete(redis_client, step: str) -> dict[str, str]:
    """Refuse until every step before ``step`` is recorded complete.

    The order is not bureaucratic. The graph remap MUST come after the flip:
    run before it, the remapped rows name ids the live collection lacks, and
    ``_scope_verdict``'s resolve-fail path (rag.py:929-933) drops every one of
    them — emptying the graph leg of recall for as long as the freeze lasts.
    """
    state = await read_state(redis_client)
    needed = STEP_ORDER[: STEP_ORDER.index(step)]
    for prior in needed:
        if not _is_complete(state, prior):
            raise MigrationRefused(
                f"cannot start step '{step}': step '{prior}' is not complete "
                f"(state: {state or 'no run recorded'})"
            )
    return state


def _is_complete(state: dict[str, str], step: str) -> bool:
    """A step is complete once a LATER step started, or it finished itself."""
    current = state.get("step")
    if current is None:
        return False
    if current == step:
        return state.get("status") == STATUS_COMPLETE
    if current not in STEP_ORDER:
        raise MigrationRefused(
            f"recorded step {current!r} is not a known step — the state hash "
            f"{STATE_KEY} is corrupt and the run cannot be reasoned about"
        )
    return STEP_ORDER.index(current) > STEP_ORDER.index(step)


def _require_freeze(settings: Settings) -> None:
    if not settings.MIGRATION_FREEZE:
        raise MigrationRefused(
            "refusing to migrate without MIGRATION_FREEZE=true. A copy taken "
            "from a live collection loses roughly half of the concurrent "
            "writes (uniformly-distributed new ids vs an id-ordered scroll "
            "cursor) and races every in-place mutator, with no catch-up pass "
            "that can repair it."
        )


async def _points_count(client, collection: str) -> int:
    info = await client.get_collection(collection)
    return int(info.points_count or 0)


async def _require_fingerprint(client, state: dict[str, str], source: str) -> int:
    """The freeze's own proof. A moved count means the freeze leaked."""
    recorded = state.get("source_points_count_at_start")
    live = await _points_count(client, source)
    if recorded is not None and int(recorded) != live:
        raise MigrationRefused(
            f"source fingerprint mismatch: {source} held {recorded} points when "
            f"the run started and holds {live} now. Something wrote past the "
            "freeze; the run cannot be reasoned about and must not continue."
        )
    return live


# ---------------------------------------------------------------------------
# Scrolling
# ---------------------------------------------------------------------------


async def _scroll(client, collection: str, *, batch_size: int = 256,
                  offset: Any = None, with_vectors: bool = False):
    """Yield ``(batch, next_offset)`` pairs so a caller can persist the cursor."""
    while True:
        records, next_offset = await client.scroll(
            collection_name=collection,
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=with_vectors,
        )
        if records:
            yield records, next_offset
        if next_offset is None:
            return
        offset = next_offset


# ---------------------------------------------------------------------------
# Step: dry run
# ---------------------------------------------------------------------------


async def dry_run(client, *, source: str, idmap_path: str = DEFAULT_IDMAP_PATH,
                  batch_size: int = 256, redis_client=None) -> MigrationPlan:
    """Classify the whole source collection and write the plan artifact.

    Read-only with respect to the STORE (it does write the plan artifact), and
    runnable outside the freeze so an operator can plan the window with real
    numbers. Mandatory: ``execute`` refuses without a reviewed plan on disk.

    REGENERATING A PLAN OVER A RUN IS REFUSED, and this is a safety property,
    not tidiness. After the flip ``QDRANT_COLLECTION`` names the shadow, so a
    plain ``dry-run`` with no ``--source`` classifies the SHADOW and overwrites
    the artifact at this path. Every fatal check in ``verify`` is a comparison
    against that artifact, so a plan derived from the shadow makes them compare
    the shadow with itself: the bucket census matches by construction, the map
    is empty so the fidelity sample is empty, the dangling baseline already
    contains whatever is broken — and a damaged store passes and gets the
    completion marker. ``verify`` refuses a foreign plan too (belt and braces);
    this refuses to create one.
    """
    if redis_client is not None:
        state = await read_state(redis_client)
        if state and source == state.get("shadow_collection", SHADOW_COLLECTION):
            raise MigrationRefused(
                f"refusing to build a plan from {source}: it is this run's "
                "SHADOW, and the plan artifact is what verify checks the shadow "
                "AGAINST. Pass --source to name the original collection, or "
                "--idmap-path to write somewhere the run does not read."
            )
    existing_plan = Path(plan_path(idmap_path))
    if existing_plan.exists():
        previous = json.loads(existing_plan.read_text(encoding="utf-8"))
        if previous.get("source_collection") != source:
            raise MigrationRefused(
                f"a plan for {previous.get('source_collection')!r} already sits "
                f"at {existing_plan}; refusing to overwrite it with one for "
                f"{source!r}. Use --idmap-path for a separate plan."
            )

    plan = await scan_plan(client, source, batch_size=batch_size)
    _write_atomic(plan_path(idmap_path), json.dumps(plan.to_dict(), indent=2) + "\n")
    logger.info(
        "dry run: %d points, %d re-keyed, %d collapsed, %d conflicts, %d already dangling",
        plan.source_points_count, len(plan.mapping), plan.collapsed,
        len(plan.conflicts), len(plan.dangling_baseline),
    )
    return plan


def _require_plan_is_this_runs(plan: MigrationPlan, state: dict[str, str]) -> None:
    """The plan must describe the collection this run started from.

    Every fatal check in ``verify`` compares the shadow against this artifact,
    so a plan describing something else does not weaken the verification — it
    INVERTS it. The reproduced case: after the flip, a plain ``dry-run`` with
    no ``--source`` classifies the shadow and overwrites the artifact; the
    bucket census then matches by construction, the map is empty so the
    fidelity sample compares nothing, and the dangling baseline already
    contains every break in the shadow. A store missing a point passed all
    three and was handed the completion marker.

    Binding it to the run's own fingerprint costs two comparisons and makes
    that substitution impossible to perform by accident.
    """
    recorded_source = state.get("source_collection")
    recorded_count = state.get("source_points_count_at_start")
    if plan.source_collection != recorded_source or (
            recorded_count is not None
            and plan.source_points_count != int(recorded_count)):
        raise MigrationRefused(
            f"the plan artifact describes {plan.source_collection!r} at "
            f"{plan.source_points_count} points, but this run started from "
            f"{recorded_source!r} at {recorded_count}. It is not this run's "
            "plan — most likely a post-flip `dry-run` with no --source "
            "regenerated it FROM THE SHADOW, which would make every check "
            "below compare the shadow with itself. Restore the original "
            "plan artifact (or re-run the dry run against "
            f"{recorded_source!r}) before verifying."
        )


def _load_plan(idmap_path: str, mapping: dict[str, str] | None = None) -> MigrationPlan:
    path = Path(plan_path(idmap_path))
    if not path.exists():
        raise MigrationRefused(
            f"no dry-run plan at {path}. The dry run is mandatory and its "
            "report is reviewed before execution (spec D10 step 3)."
        )
    return MigrationPlan.from_dict(json.loads(path.read_text(encoding="utf-8")), mapping)


# ---------------------------------------------------------------------------
# Step: the shadow copy
# ---------------------------------------------------------------------------

#: `CollectionParams` fields carried onto `create_collection`. The shadow
#: inherits the SOURCE's shape, never the env's: `EMBEDDING_DIM` drifting from
#: what the live collection actually holds would produce a shadow that accepts
#: no vectors, discovered mid-copy inside a freeze.
_PARAM_TO_KWARG = {
    "vectors": "vectors_config",
    "sparse_vectors": "sparse_vectors_config",
    "shard_number": "shard_number",
    "sharding_method": "sharding_method",
    "replication_factor": "replication_factor",
    "write_consistency_factor": "write_consistency_factor",
    "on_disk_payload": "on_disk_payload",
}


def _params_fingerprint(params: Any) -> dict:
    """The carried fields, as comparable values."""
    out = {}
    for name in _PARAM_TO_KWARG:
        value = getattr(params, name, None)
        out[name] = value.model_dump() if hasattr(value, "model_dump") else value
    return out


async def _create_shadow(client, source: str, shadow: str) -> None:
    info = await client.get_collection(source)
    params = info.config.params
    kwargs = {}
    for name, kwarg in _PARAM_TO_KWARG.items():
        value = getattr(params, name, None)
        if value is not None:
            kwargs[kwarg] = value
    await client.create_collection(collection_name=shadow, **kwargs)
    logger.info("created shadow collection %s from %s's params", shadow, source)


async def _create_indexes(client, shadow: str) -> None:
    for field_name in PAYLOAD_INDEX_FIELDS:
        try:
            await client.create_payload_index(
                collection_name=shadow,
                field_name=field_name,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        except Exception as exc:  # noqa: BLE001 — an existing index is not a failure
            logger.warning("payload index %s on %s: %s", field_name, shadow, exc)


async def _set_indexing_threshold(client, collection: str, value: int | None) -> bool:
    """Best-effort optimizer tuning; a refusal costs speed, never correctness."""
    if value is None:
        return False
    try:
        applied = await client.update_collection(
            collection_name=collection,
            optimizers_config=OptimizersConfigDiff(indexing_threshold=value),
        )
        return bool(applied)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "could not set indexing_threshold=%s on %s (%s); the copy is "
            "correct either way, only slower", value, collection, exc,
        )
        return False


async def execute(
    client,
    redis_client,
    *,
    settings: Settings,
    idmap_path: str = DEFAULT_IDMAP_PATH,
    source: str | None = None,
    shadow: str = SHADOW_COLLECTION,
    batch_size: int = 256,
    _resuming: bool = False,
) -> dict:
    """Copy the source collection into the shadow, re-keyed.

    Refuses unless the freeze is on, a reviewed dry-run plan exists, the live
    store still matches that plan, and no occupancy conflict was found. Writes
    the map before it writes any point, so a crash leaves a complete map beside
    a partial shadow rather than the reverse.
    """
    _require_freeze(settings)
    state = await read_state(redis_client)

    if state and not _resuming:
        if _is_complete(state, STEP_COPY):
            raise MigrationRefused(
                "the copy step is already complete for this run — use 'verify', "
                "or 'resume' if it was interrupted"
            )
        raise MigrationRefused(
            f"a run is already in progress (step={state.get('step')}, "
            f"status={state.get('status')}) — use 'resume'"
        )

    source = state.get("source_collection") or source or settings.QDRANT_COLLECTION
    shadow = state.get("shadow_collection") or shadow
    _assert_writable(idmap_path)

    reviewed = _load_plan(idmap_path)
    live_count = await _require_fingerprint(client, state, source)
    if not state and reviewed.source_points_count != live_count:
        raise MigrationRefused(
            f"source fingerprint mismatch: the dry run saw "
            f"{reviewed.source_points_count} points, {source} holds "
            f"{live_count} now. Re-run the dry run and review it again."
        )

    # Pass 1. Deterministic, read-only, and re-derived on every entry
    # (including a resume) so the copy pass never depends on state that a
    # crash could have left half-written.
    plan = await scan_plan(client, source, batch_size=batch_size)
    reviewed_rekeyed = (reviewed.counts[BUCKET_MIGRATABLE]
                        + reviewed.counts[BUCKET_REPAIRED])
    if len(plan.mapping) != reviewed_rekeyed:
        raise MigrationRefused(
            f"the map holds {len(plan.mapping)} entries but the reviewed dry run "
            f"found {reviewed_rekeyed} migratable points — the store changed"
        )
    if plan.counts != reviewed.counts:
        raise MigrationRefused(
            f"the store no longer classifies the way the reviewed dry run did "
            f"(dry run {reviewed.counts}, now {plan.counts})"
        )
    if plan.conflicts:
        raise MigrationRefused(
            "occupancy conflict: a predicted v2 id is already held by a point "
            f"that is not a v2 twin, so merging would destroy it — {plan.conflicts}"
        )

    if not state and await _collection_exists(client, shadow):
        # A shadow with no state is the leftover of an abandoned attempt. The
        # runbook's pre-flip rollback is "delete the shadow and unfreeze"; if
        # that was skipped, copying into it would merge this run's points with
        # a previous run's and the count reconciliation would be the first
        # thing to notice — after the freeze had already been spent.
        raise MigrationRefused(
            f"{shadow} already exists but no run is recorded. It is the residue "
            "of an abandoned attempt: delete it (the runbook's pre-flip "
            "rollback) before starting a new one."
        )

    started = state.get("started_at") or datetime.now(timezone.utc).isoformat()
    run_id = state.get("run_id") or uuid.uuid4().hex
    await _write_state(
        redis_client,
        run_id=run_id,
        source_collection=source,
        shadow_collection=shadow,
        source_points_count_at_start=live_count,
        step=STEP_COPY,
        status=STATUS_IN_PROGRESS,
        cursor=state.get("cursor") or "",
        started_at=started,
    )

    write_idmap(idmap_path, plan.mapping)
    await _mirror_idmap(redis_client, plan.mapping)

    if not await _collection_exists(client, shadow):
        await _create_shadow(client, source, shadow)
    await _create_indexes(client, shadow)

    source_info = await client.get_collection(source)
    restore_threshold = getattr(
        getattr(source_info.config, "optimizer_config", None), "indexing_threshold", None
    )
    await _set_indexing_threshold(client, shadow, 0)

    copied = await _copy_points(
        client, redis_client, plan, source=source, shadow=shadow,
        batch_size=batch_size, cursor=state.get("cursor") or None,
    )

    await _set_indexing_threshold(client, shadow, restore_threshold)
    await _write_state(redis_client, step=STEP_COPY, status=STATUS_COMPLETE,
                       cursor=CURSOR_DONE)
    logger.info("copy complete: %s points written into %s", copied, shadow)
    return {"step": STEP_COPY, "written": copied, "shadow": shadow,
            "expected_total": plan.expected_shadow_total}


async def _collection_exists(client, name: str) -> bool:
    return bool(await client.collection_exists(name))


async def _mirror_idmap(redis_client, mapping: dict[str, str]) -> None:
    """Mirror the file into Redis in bounded batches (a cache, not the truth)."""
    items = list(mapping.items())
    for start in range(0, len(items), _REDIS_BATCH):
        await redis_client.hset(
            IDMAP_REDIS_KEY, mapping=dict(items[start:start + _REDIS_BATCH]))


async def _copy_points(client, redis_client, plan: MigrationPlan, *, source: str,
                       shadow: str, batch_size: int, cursor: Any = None) -> int:
    """The two write phases: the straight copy, then the group merges.

    The cursor is persisted AFTER each batch lands, so a crash re-does at most
    one batch — and re-doing a batch is harmless, because every write is an
    upsert at a deterministic id.
    """
    grouped = {m for members in plan.target_groups.values() for m in members}
    written = 0

    if cursor != CURSOR_GROUPS and cursor != CURSOR_DONE:
        async for batch, next_offset in _scroll(
            client, source, batch_size=batch_size, offset=cursor, with_vectors=True
        ):
            structs = []
            for record in batch:
                verdict = classify(record)
                if verdict.old_id in grouped:
                    continue  # written once, by the group phase below
                structs.append(PointStruct(
                    id=verdict.new_id,
                    vector=record.vector,
                    payload=prepared_payload(
                        verdict.bucket, record.payload or {}, plan.mapping),
                ))
            if structs:
                await client.upsert(collection_name=shadow, points=structs)
                written += len(structs)
            # `is None`, not falsiness: exhaustion is the only thing that ends
            # the scroll, and a falsy-but-real offset would be read as the end.
            await _write_state(redis_client, cursor=(
                CURSOR_GROUPS if next_offset is None else next_offset))
        cursor = CURSOR_GROUPS
        await _write_state(redis_client, cursor=CURSOR_GROUPS)

    if cursor != CURSOR_DONE:
        for target, members in sorted(plan.target_groups.items()):
            records = await client.retrieve(
                collection_name=source, ids=list(members),
                with_payload=True, with_vectors=True,
            )
            if not records:
                continue
            merged = merge_target_group(target, records, plan.mapping)
            await client.upsert(collection_name=shadow, points=[PointStruct(
                id=merged.id, vector=merged.vector, payload=merged.payload)])
            written += 1
    return written


async def resume(client, redis_client, *, settings: Settings,
                 idmap_path: str = DEFAULT_IDMAP_PATH, batch_size: int = 256) -> dict:
    """Continue an interrupted run from the recorded cursor.

    Refuses on a fingerprint mismatch: a source that moved while the copy was
    down means the freeze leaked, and continuing would silently produce a
    shadow that is neither the old store nor the new one.
    """
    _require_freeze(settings)
    state = await read_state(redis_client)
    if not state:
        raise MigrationRefused("no run to resume — start with 'execute'")

    source = state["source_collection"]
    await _require_fingerprint(client, state, source)

    step = state.get("step")
    if step == STEP_COPY and state.get("status") != STATUS_COMPLETE:
        return await execute(client, redis_client, settings=settings,
                             idmap_path=idmap_path, source=source,
                             shadow=state.get("shadow_collection", SHADOW_COLLECTION),
                             batch_size=batch_size, _resuming=True)
    logger.info("nothing to resume: step=%s status=%s", step, state.get("status"))
    return {"step": step, "status": state.get("status"), "resumed": False}


# ---------------------------------------------------------------------------
# Step: the flip (recorded, not performed)
# ---------------------------------------------------------------------------


async def mark_flipped(redis_client, *, settings: Settings) -> dict:
    """Record that the operator pointed ``QDRANT_COLLECTION`` at the shadow.

    This tool cannot perform the flip — it is an env change plus a container
    recreate. What it CAN do is refuse to believe it happened: the process
    running this command was itself recreated with the new env, so its own
    settings are the evidence. Without this check an operator who forgot the
    env change would proceed to a graph remap that rewrites every row to point
    at a collection the live client is not reading.
    """
    state = await _require_step_complete(redis_client, STEP_FLIPPED)
    shadow = state.get("shadow_collection", SHADOW_COLLECTION)
    if settings.QDRANT_COLLECTION != shadow:
        raise MigrationRefused(
            f"QDRANT_COLLECTION is still {settings.QDRANT_COLLECTION!r}; the flip "
            f"to {shadow!r} has not happened in this process's environment. "
            "Update the deployment env and recreate the cortex containers first."
        )
    await _write_state(redis_client, step=STEP_FLIPPED, status=STATUS_COMPLETE)
    return {"step": STEP_FLIPPED, "collection": shadow}


# ---------------------------------------------------------------------------
# Step: the graph remap
# ---------------------------------------------------------------------------


def _content_hash(text: str, length: int) -> str:
    """`Neo4jClient._content_hash`, duplicated to avoid a settings-bound client.

    The graph client needs a live driver to construct usefully; this pass only
    needs the pure hash, and takes the length as a parameter so the caller
    supplies the deployment's own ``CONTENT_HASH_LENGTH``.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


_CHAIN_NODE_READ = """
MATCH (n)
WHERE n:Action OR n:Outcome OR n:Resolution
RETURN elementId(n) AS eid, n.id AS id, n.description AS description,
       coalesce(n.memory_ids, []) AS memory_ids
ORDER BY eid
SKIP $skip LIMIT $limit
"""

_CHAIN_NODE_IDS_WRITE = """
UNWIND $rows AS row
MATCH (n) WHERE elementId(n) = row.eid
SET n.memory_ids = row.memory_ids
"""

_CHAIN_NODE_STAMP_WRITE = """
UNWIND $rows AS row
MATCH (n) WHERE elementId(n) = row.eid
SET n.legacy_unscoped = true
"""

_MEMORY_REF_READ = """
MATCH (m:MemoryRef)
RETURN m.vector_id AS vector_id
ORDER BY vector_id
SKIP $skip LIMIT $limit
"""

_MEMORY_REF_REKEY = """
UNWIND $pairs AS p
MATCH (m:MemoryRef {vector_id: p.old})
SET m.vector_id = p.new
"""

_MEMORY_REF_DELETE_OLD = """
UNWIND $pairs AS p
MATCH (old:MemoryRef {vector_id: p.old}), (keep:MemoryRef {vector_id: p.new})
WHERE elementId(old) <> elementId(keep)
DETACH DELETE old
"""

_MEMORY_REF_CONSTRAINT = """
CREATE CONSTRAINT memory_ref_vector_id IF NOT EXISTS
FOR (m:MemoryRef) REQUIRE m.vector_id IS UNIQUE
"""


def _rel_merge_query(rel_type: str, outgoing: bool) -> str:
    """Move one relationship type off a colliding MemoryRef onto the keeper.

    Plain Cypher cannot parameterise a relationship type and this deployment
    ships no APOC, so the three types a MemoryRef can hold are enumerated in
    both directions rather than discovered.
    """
    pattern = (
        f"(old)-[:{rel_type}]->(x)" if outgoing else f"(x)-[:{rel_type}]->(old)"
    )
    merge = (
        f"MERGE (keep)-[:{rel_type}]->(x)" if outgoing
        else f"MERGE (x)-[:{rel_type}]->(keep)"
    )
    return f"""
UNWIND $pairs AS p
MATCH (old:MemoryRef {{vector_id: p.old}}), (keep:MemoryRef {{vector_id: p.new}})
WHERE elementId(old) <> elementId(keep)
MATCH {pattern}
{merge}
"""


async def _read_paged(graph, query: str, page: int = 1000) -> list[dict]:
    rows: list[dict] = []
    skip = 0
    while True:
        batch = await graph._execute_read(query, {"skip": skip, "limit": page})
        rows.extend(batch)
        if len(batch) < page:
            return rows
        skip += page


async def graph_remap(graph, mapping: dict[str, str], *,
                      content_hash_length: int) -> dict:
    """Rewrite graph references through the map and quarantine legacy nodes.

    Three things happen, in this order:

    1. **Chain-node ``memory_ids``** are translated. Unmapped ids pass through
       untouched (corpus, dream, skill and already-v2 ids are never in the
       map), and the list is de-duplicated afterwards because a collapse folds
       two source ids into one target — which would otherwise leave the same
       memory named twice on one node.
    2. **``legacy_unscoped`` is stamped** on every chain node whose id is a
       bare ``_content_hash(description)`` — the pre-D4 key, which MERGEd
       identical text from different workspaces onto ONE node. Those nodes are
       not re-keyed: splitting them would require the ownership facts the
       MERGE already destroyed. ``_scope_verdict`` denies them permanently
       instead, which is why the four read-path RETURN clauses select the flag.
    3. **``MemoryRef.vector_id``** is rewritten, colliding refs are merged into
       the survivor (relationships moved, then the duplicate deleted), and a
       uniqueness constraint is added LAST — after de-duplication, because the
       constraint would reject the very state the remap is fixing.
    """
    report = {
        "memory_ids_rewritten": 0,
        "legacy_unscoped_stamped": 0,
        "memory_ref_rekeyed": 0,
        "memory_ref_rounds": 0,
        "memory_ref_collisions": 0,
        "memory_ref_cycles": 0,
    }

    nodes = await _read_paged(graph, _CHAIN_NODE_READ)

    id_rows: list[dict] = []
    stamp_rows: list[dict] = []
    for node in nodes:
        original = list(node.get("memory_ids") or [])
        translated: list[str] = []
        for mid in original:
            mapped = mapping.get(mid, mid)
            if mapped not in translated:
                translated.append(mapped)
        if translated != original:
            id_rows.append({"eid": node["eid"], "memory_ids": translated})
        description = node.get("description")
        if isinstance(description, str) and description and \
                node.get("id") == _content_hash(description, content_hash_length):
            stamp_rows.append({"eid": node["eid"]})

    if id_rows:
        await graph._execute_write(_CHAIN_NODE_IDS_WRITE, {"rows": id_rows})
        report["memory_ids_rewritten"] = len(id_rows)
    if stamp_rows:
        await graph._execute_write(_CHAIN_NODE_STAMP_WRITE, {"rows": stamp_rows})
        report["legacy_unscoped_stamped"] = len(stamp_rows)

    existing = {row["vector_id"] for row in await _read_paged(graph, _MEMORY_REF_READ)
                if row.get("vector_id")}
    rounds, collisions, cycles = _plan_memory_ref_rewrites(existing, mapping)

    # Rewrites go out in ROUNDS, and the rounds are the whole point. A target
    # can be occupied by a node that is itself moving: A->B where B->C. Treating
    # that as a collision strands A — the straight pass moves B out first, the
    # collision MERGE then matches no keeper at B, A stays at its old id, and
    # the collision counter claims it was handled. Each round moves only pairs
    # whose target is free right now, which frees the next round's targets.
    for pairs in rounds:
        await graph._execute_write(_MEMORY_REF_REKEY, {"pairs": pairs})
        report["memory_ref_rekeyed"] += len(pairs)
    report["memory_ref_rounds"] = len(rounds)

    if collisions:
        for rel_type in _MEMORY_REF_REL_TYPES:
            for outgoing in (True, False):
                await graph._execute_write(
                    _rel_merge_query(rel_type, outgoing), {"pairs": collisions})
        await graph._execute_write(_MEMORY_REF_DELETE_OLD, {"pairs": collisions})
        report["memory_ref_collisions"] = len(collisions)
    if cycles:
        # Two refs whose targets are each other. Unreachable at uuid5 scale, and
        # LEFT ALONE rather than guessed at: the collision path would DETACH
        # DELETE both sides of the cycle, which loses data to fix a tie.
        report["memory_ref_cycles"] = len(cycles)
        logger.warning(
            "MemoryRef rewrite cycle(s) left untouched: %s", cycles[:5])

    await graph._execute_write(_MEMORY_REF_CONSTRAINT, {})
    return report


def _plan_memory_ref_rewrites(
    existing: set[str], mapping: dict[str, str]
) -> tuple[list[list[dict]], list[dict], list[dict]]:
    """Split MemoryRef rewrites into ordered rounds, collisions and cycles.

    Pure, so the ordering rule is testable without a graph. A pair may move as
    soon as its target id is unoccupied; moving it frees the id it leaves, which
    may unblock another pair. Iterating to a fixed point resolves chains of any
    length. What remains when no round makes progress is either a genuine
    collision (the target is held by something that is NOT moving — the D5 twin
    case) or a cycle (each side waiting on the other).
    """
    pending = {old: new for old, new in mapping.items() if old in existing}
    occupied = set(existing)
    rounds: list[list[dict]] = []

    while pending:
        movable = [old for old in sorted(pending) if pending[old] not in occupied]
        if not movable:
            break
        for old in movable:
            new = pending.pop(old)
            occupied.discard(old)
            occupied.add(new)
        rounds.append([{"old": old, "new": mapping[old]} for old in movable])

    collisions: list[dict] = []
    cycles: list[dict] = []
    for old, new in sorted(pending.items()):
        (cycles if new in pending else collisions).append({"old": old, "new": new})
    return rounds, collisions, cycles


async def graph_remap_step(graph, redis_client, *, settings: Settings,
                           idmap_path: str = DEFAULT_IDMAP_PATH) -> dict:
    """The state-machine wrapper: refuses before the flip, records completion.

    Requires the freeze like every other write step: the remap rewrites graph
    rows to name ids the map defines, and a sleep-cycle or memory_agent pass
    running alongside would MERGE new chain nodes carrying untranslated ids
    behind it.
    """
    _require_freeze(settings)
    await _require_step_complete(redis_client, STEP_GRAPH_REMAP)
    mapping = read_idmap(idmap_path)
    await _write_state(redis_client, step=STEP_GRAPH_REMAP, status=STATUS_IN_PROGRESS)
    report = await graph_remap(
        graph, mapping, content_hash_length=settings.CONTENT_HASH_LENGTH)
    await _write_state(redis_client, step=STEP_GRAPH_REMAP, status=STATUS_COMPLETE)
    logger.info("graph remap: %s", report)
    return report


# ---------------------------------------------------------------------------
# Step: the Redis hash folds
# ---------------------------------------------------------------------------


def _fold_value(key: str, existing: str | None, incoming: str) -> str:
    """Combine two fields that folded onto one id.

    Access counts SUM (both halves of a collapsed twin were genuinely
    recalled); last-recalled takes the LATER timestamp (ISO-8601 UTC strings
    compare lexicographically, which is why they are stored that way).
    """
    if existing is None:
        return incoming
    if key == ACCESS_COUNTS_KEY:
        try:
            return str(int(existing) + int(incoming))
        except (TypeError, ValueError):
            return incoming
    return max(existing, incoming)


async def fold_redis_hashes(redis_client, mapping: dict[str, str], client, *,
                            shadow: str, batch_size: int = 256) -> dict:
    """Translate the recall-bookkeeping hashes through the map.

    Skill ids pass through unmapped by design — skills keep their ``SKILL_NS``
    identity (D9 follow-up), so a field naming one is correct as it stands and
    is not counted as loss. The residual IS the measured loss: fields naming an
    id that no longer exists anywhere in the shadow.

    Refuses while a ``:flushing`` key holds anything. ``memory_agent`` rotates
    the live hash to that key and drains it into Qdrant; a drain landing after
    this fold would write the counts straight back under the OLD ids, silently
    undoing the translation for exactly the memories that were being recalled
    most.
    """
    report: dict[str, dict[str, int]] = {"translated": {}, "residual": {},
                                         "unprobed": {}, "fields": {}}
    for key in (ACCESS_COUNTS_KEY, LAST_RECALLED_KEY):
        flushing = f"{key}:flushing"
        if await redis_client.hlen(flushing):
            raise MigrationRefused(
                f"{flushing} is not empty — a flush is mid-flight or crashed. "
                "Stop the workers and let memory_agent drain it before folding."
            )

        raw = await redis_client.hgetall(key)
        entries = {_text(k): _text(v) for k, v in (raw or {}).items()}
        folded: dict[str, str] = {}
        moved = 0
        for field_name in sorted(entries):
            target = mapping.get(field_name, field_name)
            if target != field_name:
                moved += 1
            folded[target] = _fold_value(key, folded.get(target), entries[field_name])

        # Batched: at this store's scale both sides are six figures of fields,
        # and one HDEL/HSET carrying all of them is a single command large
        # enough to stall the instance the rest of the freeze depends on.
        stale = [f for f in entries if f not in folded]
        for start in range(0, len(stale), _REDIS_BATCH):
            await redis_client.hdel(key, *stale[start:start + _REDIS_BATCH])
        items = list(folded.items())
        for start in range(0, len(items), _REDIS_BATCH):
            await redis_client.hset(
                key, mapping=dict(items[start:start + _REDIS_BATCH]))

        # Every point id in this store is a uuid5, so a field that is not one
        # cannot name a point and is loss without asking Qdrant. Filtering them
        # out first also keeps one malformed field from making the probe reject
        # the whole batch it travelled in.
        probeable = [f for f in folded if _is_point_id(f)]
        malformed = len(folded) - len(probeable)
        present, unprobed = await _present_ids(client, shadow, probeable, batch_size)
        report["translated"][key] = moved
        report["residual"][key] = malformed + len(
            [f for f in probeable if f not in present and f not in unprobed])
        report["unprobed"][key] = len(unprobed)
        report["fields"][key] = len(folded)
    logger.info("redis hash folds: %s", report)
    return report


def _is_point_id(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


async def _present_ids(client, collection: str, ids: list[str],
                       batch_size: int = 256) -> tuple[set[str], set[str]]:
    """Which of ``ids`` are points in ``collection``, and which could not be asked.

    The two are returned separately on purpose: the residual this feeds is
    reported to an operator as MEASURED LOSS, and folding "the probe failed"
    into "the memory is gone" would overstate it.
    """
    present: set[str] = set()
    unprobed: set[str] = set()
    for start in range(0, len(ids), batch_size):
        chunk = ids[start:start + batch_size]
        try:
            records = await client.retrieve(
                collection_name=collection, ids=chunk, with_payload=False)
        except Exception as exc:  # noqa: BLE001 — a probe reports, it does not abort
            logger.warning("presence probe failed for %d ids: %s", len(chunk), exc)
            unprobed.update(chunk)
            continue
        present.update(str(r.id) for r in records)
    return present, unprobed


async def fold_hashes_step(redis_client, client, *, settings: Settings,
                           idmap_path: str = DEFAULT_IDMAP_PATH) -> dict:
    """The state-machine wrapper around ``fold_redis_hashes``.

    Requires the freeze: the fold reads a hash, rewrites its fields and deletes
    the old ones. Every live ``/memory/recall`` HINCRBYs into that same hash, so
    without the write gate a bump landing between the read and the delete is
    lost, and one landing after it resurrects an old id.
    """
    _require_freeze(settings)
    state = await _require_step_complete(redis_client, STEP_HASH_FOLD)
    mapping = read_idmap(idmap_path)
    shadow = state.get("shadow_collection", SHADOW_COLLECTION)
    await _write_state(redis_client, step=STEP_HASH_FOLD, status=STATUS_IN_PROGRESS)
    report = await fold_redis_hashes(redis_client, mapping, client, shadow=shadow)
    await _write_state(redis_client, step=STEP_HASH_FOLD, status=STATUS_COMPLETE)
    return report


# ---------------------------------------------------------------------------
# Step: verify -- exact and fatal
# ---------------------------------------------------------------------------

#: Payload keys the migration deliberately rewrites, excluded from the
#: field-by-field fidelity comparison. `namespace` is compared separately,
#: modulo normalization, rather than skipped.
_FIDELITY_EXCLUDED = frozenset(REFERENCE_FIELDS)

#: Cosine scores are computed over float32 storage on both sides, so parity
#: differences below this are quantisation, not a copy defect.
_SCORE_TOLERANCE = 1e-4
_VECTOR_TOLERANCE = 1e-6


@dataclass
class VerifyReport:
    ok: bool
    checks: dict[str, bool]
    failures: list[str]
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checks": self.checks, "failures": self.failures,
                "detail": self.detail}


async def verify(client, redis_client, *, settings: Settings,
                 idmap_path: str = DEFAULT_IDMAP_PATH,
                 batch_size: int = 256) -> VerifyReport:
    """Prove the shadow is the source, re-keyed — exactly, with no tolerance.

    Only the freeze makes this possible: with writes stopped, "the counts must
    reconcile exactly" is a statement about a fixed store rather than a race,
    so every check below is fatal rather than advisory. A pass writes the
    completion marker ``owm.py`` reads; a failure writes nothing.
    """
    _require_freeze(settings)
    state = await _require_step_complete(redis_client, STEP_VERIFY)
    source = state["source_collection"]
    shadow = state.get("shadow_collection", SHADOW_COLLECTION)
    await _require_fingerprint(client, state, source)

    mapping = read_idmap(idmap_path)
    plan = _load_plan(idmap_path, mapping)
    _require_plan_is_this_runs(plan, state)
    await _write_state(redis_client, step=STEP_VERIFY, status=STATUS_IN_PROGRESS)

    checks: dict[str, bool] = {}
    failures: list[str] = []
    detail: dict[str, Any] = {}

    def record(name: str, ok: bool, message: str = "") -> None:
        checks[name] = ok
        if not ok:
            failures.append(f"{name}: {message}")

    # One streaming pass over the shadow answers three of the checks. It keeps
    # ids and counters, never payloads: at this store's scale materialising the
    # collection to verify it would cost more memory than the copy did.
    census = {bucket: 0 for bucket in ALL_BUCKETS}
    shadow_ids: set[str] = set()
    shadow_refs: list[tuple[str, str, str]] = []
    survivors: list[str] = []
    async for batch, _ in _scroll(client, shadow, batch_size=batch_size):
        for point in batch:
            verdict = classify(point)
            payload = point.payload or {}
            census[verdict.bucket] += 1
            shadow_ids.add(str(point.id))
            for fld in REFERENCE_FIELDS:
                for ref in _reference_ids(payload.get(fld)):
                    shadow_refs.append((str(point.id), fld, ref))
            text = payload.get("text")
            if verdict.bucket in MEMORY_SCHEME_BUCKETS and isinstance(text, str) \
                    and text and str(point.id) == _v1_point_id(text):
                survivors.append(str(point.id))

    # 1. Exact per-bucket counts.
    detail["shadow_counts"] = census
    detail["shadow_total"] = sum(census.values())
    detail["expected_counts"] = plan.expected_shadow_counts
    detail["expected_total"] = plan.expected_shadow_total
    record("bucket_counts", census == plan.expected_shadow_counts,
           f"expected {plan.expected_shadow_counts}, got {census}")

    # 2. Fidelity sample: vector and payload, field by field. Vectors are
    # fetched only for the sample -- the one place they are actually compared.
    fidelity_failures: list[str] = []
    sampled = 0
    for start in range(0, len(plan.fidelity_sample_ids), batch_size):
        chunk = plan.fidelity_sample_ids[start:start + batch_size]
        targets = [mapping[old] for old in chunk if old in mapping]
        src_by_id = {str(r.id): r for r in await client.retrieve(
            collection_name=source, ids=chunk,
            with_payload=True, with_vectors=True)}
        dst_by_id = {str(r.id): r for r in await client.retrieve(
            collection_name=shadow, ids=targets,
            with_payload=True, with_vectors=True)}
        for old_id in chunk:
            new_id = mapping.get(old_id)
            src, dst = src_by_id.get(old_id), dst_by_id.get(new_id or "")
            if src is None or dst is None:
                fidelity_failures.append(f"{old_id} -> {new_id}: missing in shadow")
                continue
            sampled += 1
            fidelity_failures.extend(_compare_point(old_id, new_id, src, dst))
    # A sample of zero is not a pass. Without this floor, any plan whose
    # fidelity_sample_ids came out empty — including one regenerated from the
    # shadow, where the map is empty by construction — reports "no failures"
    # over nothing compared at all.
    if mapping and sampled == 0:
        fidelity_failures.append(
            f"nothing was sampled while the map holds {len(mapping)} entries — "
            "the plan's fidelity sample is empty, so this check verified nothing"
        )
    detail["fidelity_sampled"] = sampled
    detail["fidelity_requested"] = len(plan.fidelity_sample_ids)
    record("fidelity_sample", not fidelity_failures,
           "; ".join(fidelity_failures[:5]))

    # 3. Collection params, inherited from the source rather than the env.
    src_params = _params_fingerprint((await client.get_collection(source)).config.params)
    dst_params = _params_fingerprint((await client.get_collection(shadow)).config.params)
    detail["params"] = {"source": src_params, "shadow": dst_params}
    record("config_params", src_params == dst_params,
           f"source {src_params} vs shadow {dst_params}")

    # 4. Payload indexes.
    shadow_info = await client.get_collection(shadow)
    schema = set(shadow_info.payload_schema or {})
    missing = [f for f in PAYLOAD_INDEX_FIELDS if f not in schema]
    detail["payload_indexes"] = sorted(schema)
    record("payload_indexes", not missing, f"missing {missing}")

    # Reported, not asserted: `execute` sets indexing_threshold=0 for the bulk
    # load and restores it afterwards, but that restore's success is a boolean
    # it discards — a server that refused it leaves the shadow permanently
    # unindexed, which is a performance cliff nothing else here would notice.
    detail["shadow_indexing_threshold"] = getattr(
        getattr(shadow_info.config, "optimizer_config", None),
        "indexing_threshold", None)
    detail["source_indexing_threshold"] = getattr(
        getattr((await client.get_collection(source)).config, "optimizer_config",
                None), "indexing_threshold", None)

    # 5. Search parity.
    parity_ok, parity_detail = await _check_search_parity(
        client, plan, mapping, source=source, shadow=shadow)
    detail["search_parity"] = parity_detail
    record("search_parity", parity_ok, str(parity_detail.get("mismatches", ""))[:400])

    # 6. Dangling references: no NEW break beyond the pre-existing baseline.
    baseline = {
        (mapping.get(owner, owner), fld, mapping.get(ref, ref))
        for owner, fld, ref in plan.dangling_baseline
    }
    now_dangling = {
        (owner, fld, ref) for owner, fld, ref in shadow_refs
        if ref not in shadow_ids
    }
    new_breaks = sorted(now_dangling - baseline)
    detail["dangling_new"] = new_breaks[:10]
    detail["dangling_baseline"] = len(baseline)
    record("dangling_references", not new_breaks,
           f"{len(new_breaks)} reference(s) broken by the migration")

    # 7. No v1-keyed memory survives outside quarantine (counted in the pass
    # above; quarantine, corpus, dream, profile and skill points keep their ids
    # by design and are excluded by bucket).
    detail["v1_survivors"] = survivors[:10]
    record("no_v1_ids_outside_quarantine", not survivors,
           f"{len(survivors)} point(s) still keyed by uuid5(text)")

    report = VerifyReport(ok=not failures, checks=checks, failures=failures,
                          detail=detail)
    if report.ok:
        await redis_client.set(MIGRATION_COMPLETE_KEY,
                               json.dumps({"run_id": state.get("run_id"),
                                           "collection": shadow,
                                           "completed_at": datetime.now(
                                               timezone.utc).isoformat()}))
        await _write_state(redis_client, step=STEP_VERIFY, status=STATUS_COMPLETE)
        logger.info("verify passed; migration marker written")
    else:
        logger.error("verify FAILED: %s", failures)
    return report


def _compare_point(old_id: str, new_id: str, src: Any, dst: Any) -> list[str]:
    """Field-by-field equality for one migrated point."""
    problems: list[str] = []
    src_vec, dst_vec = src.vector, dst.vector
    if isinstance(src_vec, dict) or isinstance(dst_vec, dict):
        # Named vectors: this deployment has one unnamed vector per point, but
        # comparing elementwise on a dict would raise inside the verify pass
        # rather than report, so fall back to plain equality.
        if src_vec != dst_vec:
            problems.append(f"{old_id} -> {new_id}: named vectors differ")
    elif src_vec is None or dst_vec is None or len(src_vec) != len(dst_vec) or any(
            abs(a - b) > _VECTOR_TOLERANCE for a, b in zip(src_vec, dst_vec)):
        problems.append(f"{old_id} -> {new_id}: vector differs")

    src_payload = src.payload or {}
    dst_payload = dst.payload or {}
    for key in sorted(set(src_payload) | set(dst_payload)):
        if key in _FIDELITY_EXCLUDED:
            continue
        before, after = src_payload.get(key), dst_payload.get(key)
        if key == "namespace":
            # The one field the migration rewrites on purpose: the id is
            # minted over the normalized namespace, so the payload has to
            # carry the normalized value for the id to stay derivable.
            if after != normalize_namespace(before or "default"):
                problems.append(f"{old_id}: namespace {before!r} -> {after!r}")
            continue
        if before != after:
            problems.append(f"{old_id}: {key} {before!r} -> {after!r}")
    return problems


async def _check_search_parity(client, plan: MigrationPlan, mapping: dict[str, str],
                               *, source: str, shadow: str,
                               limit: int = 10) -> tuple[bool, dict]:
    """Query both collections with the same stored vectors; compare the answers.

    ``SearchParams(exact=True)`` on BOTH sides, and that is not a detail. The
    source has been live for months and is HNSW-indexed; the shadow was just
    bulk-loaded with ``indexing_threshold=0`` and answers by brute force. HNSW
    is approximate — the same query against the same vectors can return a
    displaced neighbour at position 7 purely because of graph structure. Under
    a fatal position-by-position comparison that is a FALSE FATAL, delivered to
    an operator mid-freeze who must then decide whether their store is corrupt.
    Exact search on both sides removes the only difference that is not evidence.

    A collapsed target legitimately changes a neighbourhood — two source points
    became one, and the surviving vector is the v2 twin's — so a difference is
    tolerated only at a position involving a collapsed id. Everything else is a
    copy defect.

    The probe FLOOR exists because every ``continue`` below silently shrinks
    the sample: an unretrievable source vector, or a plan carrying no probe ids
    at all, would otherwise report "no mismatches" over nothing at all and read
    as a pass.
    """
    collapsed = {m for members in plan.target_groups.values() for m in members}
    collapsed |= set(plan.target_groups)
    mismatches: list[str] = []
    requested = len(plan.parity_probe_ids)
    probed = 0
    truncated = 0

    for probe_id in plan.parity_probe_ids:
        records = await client.retrieve(collection_name=source, ids=[probe_id],
                                        with_payload=False, with_vectors=True)
        if not records or records[0].vector is None:
            continue
        vector = records[0].vector
        probed += 1
        exact = SearchParams(exact=True)
        src_hits = (await client.query_points(
            collection_name=source, query=vector, limit=limit,
            search_params=exact)).points
        dst_hits = (await client.query_points(
            collection_name=shadow, query=vector, limit=limit,
            search_params=exact)).points

        expected: list[tuple[str, float]] = []
        seen: set[str] = set()
        for hit in src_hits:
            mapped = mapping.get(str(hit.id), str(hit.id))
            if mapped in seen:
                continue
            seen.add(mapped)
            expected.append((mapped, float(hit.score)))
        actual = [(str(h.id), float(h.score)) for h in dst_hits]
        if len(expected) != len(actual):
            # Expected when a collapse removed a neighbour; counted rather than
            # ignored, because the comparison below only reaches the shorter of
            # the two and a growing count means the sample is thinning.
            truncated += 1

        for position, (want, got) in enumerate(zip(expected, actual)):
            if want[0] == got[0] and abs(want[1] - got[1]) <= _SCORE_TOLERANCE:
                continue
            if want[0] in collapsed or got[0] in collapsed:
                break  # a merged neighbour legitimately reorders what follows
            mismatches.append(
                f"probe {probe_id} position {position}: expected {want}, got {got}")
            break

    detail = {"requested": requested, "probed": probed, "truncated": truncated,
              "mismatches": mismatches[:5]}
    if requested == 0 and plan.source_points_count > 0:
        detail["floor"] = "the plan carries no parity probes for a non-empty store"
        return False, detail
    if probed * 2 < requested:
        detail["floor"] = (
            f"only {probed} of {requested} probe vectors could be retrieved — "
            "the parity sample is too thin to mean anything"
        )
        return False, detail
    return not mismatches, detail


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The seven subcommands, in the order the runbook uses them."""
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.memory_identity_migration",
        description=(
            "Identity-v2 freeze migration (spec D6). Run inside a maintenance "
            "freeze, on an explicit user go, after a reviewed dry run."
        ),
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--idmap-path", default=DEFAULT_IDMAP_PATH,
        help=(f"durable old->new id map, JSONL (default: {DEFAULT_IDMAP_PATH}; "
              "the container's ./backups mount is read-only by default)"),
    )
    common.add_argument("--source", default=None,
                        help="source collection (default: QDRANT_COLLECTION)")
    common.add_argument("--batch-size", type=int, default=256)

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("dry-run", parents=[common],
                   help=("classify everything and write the plan artifact; the "
                         "STORE is untouched. Pass --source explicitly once the "
                         "flip has happened — the default now names the shadow"))
    sub.add_parser("execute", parents=[common],
                   help="copy the source into the shadow, re-keyed (needs the freeze)")
    sub.add_parser("resume", parents=[common],
                   help="continue an interrupted copy from its cursor")
    sub.add_parser("mark-flipped", parents=[common],
                   help="record that QDRANT_COLLECTION now names the shadow")
    sub.add_parser("graph-remap", parents=[common],
                   help="rewrite Neo4j references; stamp legacy_unscoped nodes")
    sub.add_parser("fold-hashes", parents=[common],
                   help="translate the recall-bookkeeping Redis hashes")
    sub.add_parser("verify", parents=[common],
                   help="exact reconciliation; writes the completion marker")
    return parser


async def _dispatch(args, settings: Settings) -> dict:
    from qdrant_client import AsyncQdrantClient
    import redis.asyncio

    client = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
    redis_client = redis.asyncio.from_url(settings.REDIS_URL, decode_responses=True)
    graph = None
    try:
        if args.command == "dry-run":
            source = args.source or settings.QDRANT_COLLECTION
            # redis_client is passed so the dry run can see whether a run is
            # recorded: post-flip, the default source IS the shadow, and a plan
            # built from it would disarm every check verify makes.
            plan = await dry_run(client, source=source, idmap_path=args.idmap_path,
                                 batch_size=args.batch_size,
                                 redis_client=redis_client)
            return plan.to_dict()
        if args.command == "execute":
            return await execute(client, redis_client, settings=settings,
                                 idmap_path=args.idmap_path, source=args.source,
                                 batch_size=args.batch_size)
        if args.command == "resume":
            return await resume(client, redis_client, settings=settings,
                                idmap_path=args.idmap_path,
                                batch_size=args.batch_size)
        if args.command == "mark-flipped":
            return await mark_flipped(redis_client, settings=settings)
        if args.command == "graph-remap":
            from app.db.graph import Neo4jClient

            graph = Neo4jClient(settings)
            await graph.connect()
            return await graph_remap_step(graph, redis_client, settings=settings,
                                          idmap_path=args.idmap_path)
        if args.command == "fold-hashes":
            return await fold_hashes_step(redis_client, client, settings=settings,
                                          idmap_path=args.idmap_path)
        if args.command == "verify":
            report = await verify(client, redis_client, settings=settings,
                                  idmap_path=args.idmap_path,
                                  batch_size=args.batch_size)
            return report.to_dict()
        raise MigrationRefused(f"unknown command {args.command!r}")
    finally:
        if graph is not None:
            await graph.close()
        await redis_client.aclose()
        await client.close()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    settings = get_settings()
    try:
        result = asyncio.run(_dispatch(args, settings))
    except MigrationRefused as exc:
        print(json.dumps({"refused": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main())
