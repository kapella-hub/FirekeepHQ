"""The freeze-migration tool (identity-v2 D6), against a seeded local Qdrant.

WHY A REAL STORE AND NOT A MOCK. This module will one day rewrite the user's
entire live memory collection, and the failure it exists to prevent — a point
silently landing at the wrong id, or not landing at all — is invisible to a
mock that returns whatever the test told it to. `qdrant_client` ships a local
mode (`AsyncQdrantClient(":memory:")`) whose scroll cursors, upsert semantics
and cosine scoring are the real implementation, so every copy/verify test here
runs against an actual store. Two local-mode limits are worked around
explicitly rather than papered over: payload indexes have no effect locally
(a recording wrapper reports what was created), and `update_collection`
returns False instead of applying the optimizer tweak (the tool treats that as
a warning, which is exactly what it must do against a server that refuses it).

The classification/plan layer is pure and is tested without any store at all.
"""

from __future__ import annotations

import json
import random
import uuid
from pathlib import Path
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.config import Settings
from app.db.vector import (
    FIREKEEP_UUID_NAMESPACE,
    _v1_point_id,
    memory_point_id,
)
from app.workspace_migration import QUARANTINE_WORKSPACE

from app.workers import memory_identity_migration as mig

WS = "ws-alpha"
NS = "engineering"
DIM = 8


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _vec(seed: int) -> list[float]:
    """A deterministic vector per seed, with no ties.

    Seeded rather than arbitrary because the search-parity check compares
    ranked neighbours between two collections: two points at an identical
    cosine score could order differently in each and read as a copy defect.
    """
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(DIM)]


def _mem(text: str, *, ws: str | None = WS, ns: str | None = NS, **extra) -> dict:
    """A memory-shaped payload, matching VectorClient.upsert's write shape."""
    payload = {
        "text": text,
        "source": extra.pop("source", "agent"),
        "tags": extra.pop("tags", ["t"]),
        "domain": "general",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
        "memory_type": "episodic",
        "agent_id": "a1",
        "session_id": "s1",
        "project": "firekeep",
        "member_id": "m1",
        "status": "active",
        "confirmed_count": 0,
        "contradicted_count": 0,
        "last_confirmed_at": None,
        "superseded_by": None,
        "metadata": {},
    }
    if ns is not None:
        payload["namespace"] = ns
    if ws is not None:
        payload["workspace_id"] = ws
    payload.update(extra)
    return payload


def _corpus_id(source_name: str, ingest_id: str, idx: int) -> str:
    raw = f"corpus|{WS}|{source_name}|{ingest_id}|{idx}"
    return str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, raw))


# Texts used by the seed, named so assertions read.
T_PLAIN_A = "the deploy key lives in the vault"
T_PLAIN_B = "restart the collector after a config change"
T_NO_NS = "a memory written before the namespace field existed"
T_REPAIRED = "the café build is green"
T_REPAIRED_OLD = "the cafÃ© build is green"  # the pre-repair mojibake text
T_TWIN = "the freeze is the restore point"
T_V2_ONLY = "aliases cannot collide with a collection name"
T_CORPUS_LEGACY = "a legacy corpus chunk keyed by bare uuid5 of its text"
T_REF_OWNER = "this memory was superseded"
T_CONTESTED = "this memory is contested"
T_DANGLER = "this memory points at nothing"


def seed_points() -> list[PointStruct]:
    """The mixed store every copy/verify test runs against.

    Deliberately contains one of each thing the spec's classification section
    names, including the two cases that DEFEAT the id predicate: a legacy
    corpus chunk whose id IS uuid5(text), and a mojibake-repaired memory whose
    id matches neither scheme.
    """
    pts: list[PointStruct] = []
    n = [0]

    def add(pid: str, payload: dict) -> PointStruct:
        n[0] += 1
        p = PointStruct(id=pid, vector=_vec(n[0]), payload=payload)
        pts.append(p)
        return p

    # --- v1 migratable ---------------------------------------------------
    add(_v1_point_id(T_PLAIN_A), _mem(T_PLAIN_A))
    add(_v1_point_id(T_PLAIN_B), _mem(T_PLAIN_B))
    # absent `namespace` key -> reads as "default" (namespace_condition's
    # legacy semantics), so the v2 id is minted over "default".
    add(_v1_point_id(T_NO_NS), _mem(T_NO_NS, ns=None))
    # --- repaired text (id matches NEITHER scheme) -----------------------
    add(_v1_point_id(T_REPAIRED_OLD), _mem(T_REPAIRED))
    # --- the D5 twin: a v1 point and its post-deploy v2 relearn ----------
    add(_v1_point_id(T_TWIN), _mem(T_TWIN, status="archived", confirmed_count=3,
                                   archived_at="2026-02-02T00:00:00+00:00"))
    add(memory_point_id(WS, NS, T_TWIN), _mem(T_TWIN, confirmed_count=1))
    # --- a plain v2 point ------------------------------------------------
    add(memory_point_id(WS, NS, T_V2_ONLY), _mem(T_V2_ONLY))
    # --- corpus: legacy (id-predicate TRUE) and modern -------------------
    add(_v1_point_id(T_CORPUS_LEGACY),
        _mem(T_CORPUS_LEGACY, source="corpus", committed=True, visibility="workspace"))
    add(_corpus_id("runbook.md", "ing-1", 0),
        _mem("a modern corpus chunk", source="corpus", committed=True))
    # --- dream / profile / skill ----------------------------------------
    add(str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, "dream::c1::0")),
        _mem("a consolidated insight", source="dream", memory_type="procedural"))
    add(str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, "profile::m1::ws-alpha")),
        _mem("a person profile", source="dream_profile", memory_type="reference"))
    add(str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, "skill::x")),
        _mem("a skill body", memory_type="skill", source="skill_synthesis"))
    # --- quarantine: no workspace at all, and an explicit None -----------
    add(_v1_point_id("an unattributable memory"), _mem("an unattributable memory", ws=None))
    add(_v1_point_id("an explicitly null-workspace memory"),
        _mem("an explicitly null-workspace memory", ws=None, workspace_id=None))
    # --- reference-carrying points ---------------------------------------
    add(_v1_point_id(T_REF_OWNER),
        _mem(T_REF_OWNER, status="superseded", superseded_by=_v1_point_id(T_PLAIN_A)))
    add(_v1_point_id(T_CONTESTED),
        _mem(T_CONTESTED, contested=True, contested_with=_v1_point_id(T_PLAIN_B)))
    # --- a PRE-EXISTING dangling reference (the verify baseline) ---------
    add(_v1_point_id(T_DANGLER),
        _mem(T_DANGLER, superseded_by=_v1_point_id("a memory that was hard-deleted")))
    return pts


async def _seeded(points: list[PointStruct] | None = None,
                  collection: str = "firekeep_memory") -> AsyncQdrantClient:
    client = AsyncQdrantClient(":memory:")
    await client.create_collection(
        collection_name=collection,
        vectors_config=VectorParams(size=DIM, distance=Distance.COSINE),
    )
    await client.upsert(collection_name=collection,
                        points=points if points is not None else seed_points())
    return client


class _IndexRecording:
    """Local Qdrant ignores payload indexes; this reports what was asked for.

    `get_collection` is narrowed to the three attributes the tool reads
    (`config`, `points_count`, `payload_schema`) so the substitution cannot
    accidentally satisfy a check by carrying something real Qdrant would not.
    """

    def __init__(self, inner: AsyncQdrantClient) -> None:
        self._inner = inner
        self.indexes: dict[str, set[str]] = {}

    def __getattr__(self, name):  # pragma: no cover - passthrough
        return getattr(self._inner, name)

    async def create_payload_index(self, collection_name, field_name, field_schema=None, **kw):
        self.indexes.setdefault(collection_name, set()).add(field_name)
        return None

    async def get_collection(self, collection_name, **kw):
        info = await self._inner.get_collection(collection_name, **kw)
        return SimpleNamespace(
            config=info.config,
            points_count=info.points_count,
            payload_schema={f: object() for f in self.indexes.get(collection_name, set())},
        )


class _CrashAfter:
    """Kills the process at the Nth upsert — the planted mid-copy crash."""

    def __init__(self, inner, after: int) -> None:
        self._inner = inner
        self._after = after
        self.calls = 0

    def __getattr__(self, name):  # pragma: no cover - passthrough
        return getattr(self._inner, name)

    async def upsert(self, *args, **kwargs):
        if self.calls >= self._after:
            raise RuntimeError("planted crash mid-copy")
        self.calls += 1
        return await self._inner.upsert(*args, **kwargs)


@pytest.fixture()
def redis_client():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture()
def frozen() -> Settings:
    return Settings(MIGRATION_FREEZE=True, QDRANT_COLLECTION="firekeep_memory")


@pytest.fixture()
def idmap(tmp_path: Path) -> str:
    return str(tmp_path / "mem-idmap-v2.jsonl")


async def _dump(client, collection: str) -> dict[str, PointStruct]:
    """Every point of a collection, id -> record (payload + vector)."""
    out: dict[str, PointStruct] = {}
    offset = None
    while True:
        recs, offset = await client.scroll(
            collection_name=collection, limit=64, offset=offset,
            with_payload=True, with_vectors=True,
        )
        for r in recs:
            out[str(r.id)] = r
        if offset is None:
            break
    return out


async def _run_full(client, redis, settings, idmap_path, *, source="firekeep_memory"):
    """dry-run -> execute -> mark-flipped, the operator sequence up to verify."""
    plan = await mig.dry_run(client, source=source, idmap_path=idmap_path)
    await mig.execute(client, redis, settings=settings, idmap_path=idmap_path,
                      source=source, batch_size=4)
    return plan


# ---------------------------------------------------------------------------
# Classification — pure, provenance-first
# ---------------------------------------------------------------------------


def _p(pid: str, payload: dict):
    return SimpleNamespace(id=pid, payload=payload)


class TestClassify:
    def test_corpus_wins_over_a_true_id_predicate(self):
        """The legacy corpus chunk: id IS uuid5(text), source IS corpus.

        This is the case that forced classification to be provenance-first.
        Re-keying it would break corpus's source-scoped identity contract.
        """
        text = T_CORPUS_LEGACY
        c = mig.classify(_p(_v1_point_id(text), _mem(text, source="corpus")))
        assert c.bucket == mig.BUCKET_CORPUS
        assert c.new_id == c.old_id

    def test_modern_corpus_chunk(self):
        c = mig.classify(_p(_corpus_id("r.md", "i", 0), _mem("x", source="corpus")))
        assert c.bucket == mig.BUCKET_CORPUS

    def test_dream_profile_and_skill_are_untouched(self):
        d = mig.classify(_p("id-d", _mem("x", source="dream", memory_type="procedural")))
        p = mig.classify(_p("id-p", _mem("x", source="dream_profile", memory_type="reference")))
        s = mig.classify(_p("id-s", _mem("x", memory_type="skill")))
        assert (d.bucket, p.bucket, s.bucket) == (
            mig.BUCKET_DREAM, mig.BUCKET_PROFILE, mig.BUCKET_SKILL)
        assert d.new_id == "id-d" and p.new_id == "id-p" and s.new_id == "id-s"

    def test_v2_point_recognised_by_its_own_payload(self):
        c = mig.classify(_p(memory_point_id(WS, NS, T_V2_ONLY), _mem(T_V2_ONLY)))
        assert c.bucket == mig.BUCKET_V2
        assert c.new_id == c.old_id

    def test_v1_migratable_is_rekeyed(self):
        c = mig.classify(_p(_v1_point_id(T_PLAIN_A), _mem(T_PLAIN_A)))
        assert c.bucket == mig.BUCKET_MIGRATABLE
        assert c.new_id == memory_point_id(WS, NS, T_PLAIN_A)

    def test_absent_namespace_key_reads_as_default(self):
        """`namespace_condition`'s legacy semantics: a point written before the
        field existed belongs to "default", not to no namespace at all."""
        c = mig.classify(_p(_v1_point_id(T_NO_NS), _mem(T_NO_NS, ns=None)))
        assert c.bucket == mig.BUCKET_MIGRATABLE
        assert c.new_id == memory_point_id(WS, "default", T_NO_NS)

    def test_repaired_text_is_rehomed_from_the_payload(self):
        """The mojibake repairs: the id matches neither scheme because the text
        was edited after minting. The payload is the authority."""
        c = mig.classify(_p(_v1_point_id(T_REPAIRED_OLD), _mem(T_REPAIRED)))
        assert c.bucket == mig.BUCKET_REPAIRED
        assert c.new_id == memory_point_id(WS, NS, T_REPAIRED)

    def test_falsy_workspace_quarantines_at_the_same_id(self):
        for payload in (_mem("x", ws=None), _mem("x", ws=None, workspace_id=None),
                        _mem("x", ws=None, workspace_id="")):
            c = mig.classify(_p("keep-me", payload))
            assert c.bucket == mig.BUCKET_QUARANTINE
            assert c.new_id == "keep-me"

    def test_a_shape_nobody_predicted_is_copied_verbatim_not_dropped(self):
        c = mig.classify(_p("weird", {"source": "agent", "workspace_id": WS}))
        assert c.bucket == mig.BUCKET_UNCLASSIFIED
        assert c.new_id == "weird"

    def test_namespace_is_normalized_into_the_seed(self):
        """D1's invariant: the payload namespace must be derivable from the id,
        so the mint normalizes once and the copy stores the normalized value."""
        c = mig.classify(_p(_v1_point_id(T_PLAIN_A), _mem(T_PLAIN_A, ns="Engineering")))
        assert c.new_id == memory_point_id(WS, "engineering", T_PLAIN_A)

    def test_a_quarantined_point_reclassifies_as_quarantine_not_migratable(self):
        """Verify re-classifies the SHADOW, so classification has to be stable
        on an already-migrated store. A quarantine copy keeps its v1-shaped id
        and carries a TRUTHY (sentinel) workspace_id — read in the wrong order
        that is indistinguishable from an ordinary migratable point, and the
        verify pass would demand it be re-keyed forever."""
        c = mig.classify(_p(_v1_point_id("x"), _mem(
            "x", ws=QUARANTINE_WORKSPACE, legacy_unscoped=True)))
        assert c.bucket == mig.BUCKET_QUARANTINE
        assert c.new_id == c.old_id

    def test_either_quarantine_stamp_alone_is_enough(self):
        """Mirrors `workspace_migration._is_quarantined`: a hand-repaired point
        carrying one stamp but not the other must still quarantine."""
        sentinel_only = mig.classify(_p("a", _mem("x", ws=QUARANTINE_WORKSPACE)))
        flag_only = mig.classify(_p("b", _mem("x", legacy_unscoped=True)))
        assert sentinel_only.bucket == flag_only.bucket == mig.BUCKET_QUARANTINE


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


class TestBuildPlan:
    @pytest.fixture()
    def plan(self):
        pts = seed_points()
        return mig.build_plan(pts, source_collection="firekeep_memory",
                              source_points_count=len(pts))

    def test_counts_cover_every_point_exactly_once(self, plan):
        assert sum(plan.counts.values()) == plan.source_points_count == 17

    def test_bucket_census(self, plan):
        assert plan.counts[mig.BUCKET_CORPUS] == 2
        assert plan.counts[mig.BUCKET_DREAM] == 1
        assert plan.counts[mig.BUCKET_PROFILE] == 1
        assert plan.counts[mig.BUCKET_SKILL] == 1
        assert plan.counts[mig.BUCKET_V2] == 2
        assert plan.counts[mig.BUCKET_QUARANTINE] == 2
        assert plan.counts[mig.BUCKET_REPAIRED] == 1
        assert plan.counts[mig.BUCKET_MIGRATABLE] == 7

    def test_mapping_holds_only_rekeyed_points(self, plan):
        assert len(plan.mapping) == 8  # 7 migratable + 1 repaired
        assert plan.mapping[_v1_point_id(T_PLAIN_A)] == memory_point_id(WS, NS, T_PLAIN_A)
        assert _v1_point_id(T_CORPUS_LEGACY) not in plan.mapping

    def test_occupancy_finds_the_d5_twin(self, plan):
        twin_target = memory_point_id(WS, NS, T_TWIN)
        assert plan.occupied_targets == [twin_target]
        assert sorted(plan.target_groups[twin_target]) == sorted(
            [_v1_point_id(T_TWIN), twin_target])

    def test_dangling_baseline_records_the_pre_existing_break_only(self, plan):
        owners = {row[0] for row in plan.dangling_baseline}
        assert owners == {_v1_point_id(T_DANGLER)}

    def test_expected_shadow_total_accounts_for_the_collapse(self, plan):
        assert plan.collapsed == 1
        assert plan.expected_shadow_total == plan.source_points_count - 1

    def test_plan_round_trips_through_json(self, plan):
        again = mig.MigrationPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
        assert again.counts == plan.counts
        assert again.expected_shadow_total == plan.expected_shadow_total
        assert again.occupied_targets == plan.occupied_targets

    def test_sampling_is_deterministic_across_rebuilds(self):
        """`execute` re-derives the plan and cross-checks it against the dry
        run's. A random sample would make that comparison flap."""
        pts = seed_points()
        a = mig.build_plan(pts, source_collection="c", source_points_count=len(pts))
        b = mig.build_plan(list(reversed(pts)), source_collection="c",
                           source_points_count=len(pts))
        assert a.parity_probe_ids == b.parity_probe_ids
        assert a.fidelity_sample_ids == b.fidelity_sample_ids
        assert a.mapping == b.mapping


class TestOccupancy:
    """The three ways a predicted v2 id can already be taken."""

    def _plan(self, points):
        return mig.build_plan(points, source_collection="c",
                              source_points_count=len(points))

    def test_a_seed_shaped_text_vacates_its_target_rather_than_merging(self):
        """The contrived residual D1 names: a v1 memory whose literal TEXT is
        a v2 seed. Its bare uuid5(text) then equals some OTHER memory's
        predicted v2 id — but it is itself being re-keyed away, so the id is
        vacated and there is nothing to merge. Treating it as a twin would
        fold two unrelated memories into one."""
        from app.db.vector import mem2_seed

        victim_text = "the collision victim"
        trap_text = mem2_seed(WS, NS, victim_text)
        target = memory_point_id(WS, NS, victim_text)
        assert _v1_point_id(trap_text) == target  # the collision is real

        plan = self._plan([
            _p(_v1_point_id(victim_text), _mem(victim_text)),
            _p(target, _mem(trap_text)),
        ])
        assert plan.occupied_targets == [target]  # reported...
        assert plan.conflicts == []               # ...but not a conflict...
        assert plan.target_groups == {}           # ...and nothing is merged
        assert plan.collapsed == 0
        assert plan.expected_shadow_total == 2

    def test_a_non_twin_tenant_at_a_predicted_id_is_a_conflict(self):
        """Anything that is NOT a v2 twin sitting at a predicted id would be
        destroyed by a merge, so the plan refuses instead of guessing."""
        victim_text = "the collision victim"
        target = memory_point_id(WS, NS, victim_text)
        plan = self._plan([
            _p(_v1_point_id(victim_text), _mem(victim_text)),
            _p(target, _mem("a corpus chunk", source="corpus")),
        ])
        assert [c["target"] for c in plan.conflicts] == [target]
        assert plan.conflicts[0]["tenant_bucket"] == mig.BUCKET_CORPUS
        assert plan.target_groups == {}

    async def test_execute_refuses_on_a_conflict(self, redis_client, frozen, idmap):
        victim_text = "the collision victim"
        target = memory_point_id(WS, NS, victim_text)
        client = await _seeded([
            PointStruct(id=_v1_point_id(victim_text), vector=_vec(1),
                        payload=_mem(victim_text)),
            PointStruct(id=target, vector=_vec(2),
                        payload=_mem("a corpus chunk", source="corpus")),
        ])
        await mig.dry_run(client, source="firekeep_memory", idmap_path=idmap)
        with pytest.raises(mig.MigrationRefused, match="conflict"):
            await mig.execute(client, redis_client, settings=frozen,
                              idmap_path=idmap, source="firekeep_memory")
        assert not await client.collection_exists(mig.SHADOW_COLLECTION)


class TestReferenceRewrite:
    def test_scalar_and_list_reference_fields_are_mapped(self):
        old_a, old_b = _v1_point_id(T_PLAIN_A), _v1_point_id(T_PLAIN_B)
        mapping = {old_a: "NEW-A", old_b: "NEW-B"}
        out = mig.rewrite_references(
            {"superseded_by": old_a, "contested_with": [old_b, "untouched"]}, mapping)
        assert out["superseded_by"] == "NEW-A"
        assert out["contested_with"] == ["NEW-B", "untouched"]

    def test_unmapped_references_pass_through(self):
        payload = {"superseded_by": "corpus-id", "contested_with": None}
        assert mig.rewrite_references(payload, {}) == payload

    def test_the_input_payload_is_not_mutated(self):
        payload = {"superseded_by": "a"}
        mig.rewrite_references(payload, {"a": "b"})
        assert payload["superseded_by"] == "a"

    def test_a_quarantined_point_still_gets_its_references_rewritten(self):
        """Quarantine is about who OWNS the point, not about whether the ids it
        names are still correct — a quarantined memory superseded by a migrated
        one must follow that memory to its new id."""
        out = mig.prepared_payload(
            mig.BUCKET_QUARANTINE, {"superseded_by": "old"}, {"old": "new"})
        assert out["superseded_by"] == "new"
        assert out["workspace_id"] == QUARANTINE_WORKSPACE
        assert out["legacy_unscoped"] is True


class TestTwinMerge:
    """The occupied-v2-id case. v2 text and vector win; lifecycle survives."""

    def _members(self):
        v1_id = _v1_point_id(T_TWIN)
        v2_id = memory_point_id(WS, NS, T_TWIN)
        v1 = SimpleNamespace(id=v1_id, vector=_vec(5),
                             payload=_mem(T_TWIN, status="archived", confirmed_count=3,
                                          archived_at="2026-02-02T00:00:00+00:00",
                                          created_at="2025-01-01T00:00:00+00:00"))
        v2 = SimpleNamespace(id=v2_id, vector=_vec(6),
                             payload=_mem(T_TWIN, confirmed_count=1))
        return v1, v2, v2_id

    def test_v2_text_and_vector_win_lifecycle_survives(self):
        v1, v2, target = self._members()
        merged = mig.merge_target_group(target, [v1, v2], mapping={})
        assert merged.vector == v2.vector
        assert merged.payload["text"] == T_TWIN
        # _merge_lifecycle's invariants, reached through the migration.
        assert merged.payload["status"] == "archived"
        assert merged.payload["confirmed_count"] == 3
        assert merged.payload["archived_at"] == "2026-02-02T00:00:00+00:00"
        assert merged.payload["created_at"] == "2025-01-01T00:00:00+00:00"

    def test_result_is_identical_regardless_of_order(self):
        v1, v2, target = self._members()
        a = mig.merge_target_group(target, [v1, v2], mapping={})
        b = mig.merge_target_group(target, [v2, v1], mapping={})
        assert a.payload == b.payload
        assert a.vector == b.vector
        assert a.id == b.id == target


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    async def test_is_read_only_and_writes_the_plan_artifact(self, idmap):
        client = await _seeded()
        before = await _dump(client, "firekeep_memory")
        plan = await mig.dry_run(client, source="firekeep_memory", idmap_path=idmap)
        after = await _dump(client, "firekeep_memory")
        assert {k: (v.payload, v.vector) for k, v in before.items()} == \
               {k: (v.payload, v.vector) for k, v in after.items()}
        assert not await client.collection_exists(mig.SHADOW_COLLECTION)
        assert Path(mig.plan_path(idmap)).exists()
        assert plan.source_points_count == 17

    async def test_runs_without_the_freeze(self, idmap):
        """Mandatory before the freeze window, so an operator can plan."""
        client = await _seeded()
        plan = await mig.dry_run(client, source="firekeep_memory", idmap_path=idmap)
        assert plan.counts[mig.BUCKET_MIGRATABLE] == 7


# ---------------------------------------------------------------------------
# execute — the guards
# ---------------------------------------------------------------------------


class TestExecuteRefusals:
    async def test_refuses_without_the_freeze(self, redis_client, idmap):
        client = await _seeded()
        await mig.dry_run(client, source="firekeep_memory", idmap_path=idmap)
        with pytest.raises(mig.MigrationRefused, match="MIGRATION_FREEZE"):
            await mig.execute(client, redis_client,
                              settings=Settings(MIGRATION_FREEZE=False),
                              idmap_path=idmap, source="firekeep_memory")
        assert not await client.collection_exists(mig.SHADOW_COLLECTION)

    async def test_refuses_without_a_reviewed_dry_run(self, redis_client, frozen, idmap):
        client = await _seeded()
        with pytest.raises(mig.MigrationRefused, match="dry.run"):
            await mig.execute(client, redis_client, settings=frozen,
                              idmap_path=idmap, source="firekeep_memory")

    async def test_refuses_when_the_store_moved_since_the_dry_run(
            self, redis_client, frozen, idmap):
        client = await _seeded()
        await mig.dry_run(client, source="firekeep_memory", idmap_path=idmap)
        await client.upsert("firekeep_memory", points=[PointStruct(
            id=_v1_point_id("a write that slipped past the freeze"),
            vector=_vec(99),
            payload=_mem("a write that slipped past the freeze"))])
        with pytest.raises(mig.MigrationRefused, match="fingerprint|points_count"):
            await mig.execute(client, redis_client, settings=frozen,
                              idmap_path=idmap, source="firekeep_memory")

    async def test_resume_refuses_on_a_fingerprint_mismatch(
            self, redis_client, frozen, idmap):
        client = await _seeded()
        await mig.dry_run(client, source="firekeep_memory", idmap_path=idmap)
        crashy = _CrashAfter(client, after=2)
        with pytest.raises(RuntimeError, match="planted crash"):
            await mig.execute(crashy, redis_client, settings=frozen,
                              idmap_path=idmap, source="firekeep_memory", batch_size=2)
        # A write lands while the migration is half-done.
        await client.upsert("firekeep_memory", points=[PointStruct(
            id=_v1_point_id("post-crash write"), vector=_vec(98),
            payload=_mem("post-crash write"))])
        with pytest.raises(mig.MigrationRefused, match="fingerprint|points_count"):
            await mig.resume(client, redis_client, settings=frozen, idmap_path=idmap)

    async def test_refuses_up_front_when_the_map_cannot_be_written(
            self, redis_client, frozen, tmp_path, idmap):
        """The container's ./backups mount is READ-ONLY. Discovering that after
        writing half a collection strands an operator inside a freeze with a
        partial shadow and no map; discovering it here costs nothing."""
        client = await _seeded()
        await mig.dry_run(client, source="firekeep_memory", idmap_path=idmap)
        blocker = tmp_path / "read-only-mount"
        blocker.write_text("", encoding="utf-8")
        unwritable = str(blocker / "sub" / "mem-idmap-v2.jsonl")
        with pytest.raises(mig.MigrationRefused, match="not writable"):
            await mig.execute(client, redis_client, settings=frozen,
                              idmap_path=unwritable, source="firekeep_memory")
        assert not await client.collection_exists(mig.SHADOW_COLLECTION)
        assert not await mig.read_state(redis_client)

    async def test_a_leftover_shadow_with_no_run_recorded_refuses(
            self, redis_client, frozen, idmap):
        """The runbook's pre-flip rollback is "delete the shadow and unfreeze".
        If that was skipped, a fresh run would merge into the abandoned
        attempt's points and only the count reconciliation would notice — after
        the freeze had been spent."""
        client = await _seeded()
        await mig.dry_run(client, source="firekeep_memory", idmap_path=idmap)
        await client.create_collection(
            collection_name=mig.SHADOW_COLLECTION,
            vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
        with pytest.raises(mig.MigrationRefused, match="abandoned attempt"):
            await mig.execute(client, redis_client, settings=frozen,
                              idmap_path=idmap, source="firekeep_memory")

    async def test_a_finished_copy_refuses_a_second_execute(
            self, redis_client, frozen, idmap):
        client = await _seeded()
        await _run_full(client, redis_client, frozen, idmap)
        with pytest.raises(mig.MigrationRefused, match="already complete"):
            await mig.execute(client, redis_client, settings=frozen,
                              idmap_path=idmap, source="firekeep_memory")

    async def test_a_later_step_refuses_before_its_predecessor_completes(
            self, redis_client, frozen, idmap):
        client = await _seeded()
        await mig.dry_run(client, source="firekeep_memory", idmap_path=idmap)
        with pytest.raises(mig.MigrationRefused, match="step"):
            await mig.mark_flipped(redis_client, settings=frozen)

    async def test_mark_flipped_refuses_until_the_env_actually_flipped(
            self, redis_client, frozen, idmap):
        client = await _seeded()
        await _run_full(client, redis_client, frozen, idmap)
        with pytest.raises(mig.MigrationRefused, match="QDRANT_COLLECTION"):
            await mig.mark_flipped(redis_client, settings=frozen)  # still the old name
        flipped = Settings(MIGRATION_FREEZE=True, QDRANT_COLLECTION=mig.SHADOW_COLLECTION)
        await mig.mark_flipped(redis_client, settings=flipped)
        state = await mig.read_state(redis_client)
        assert state["step"] == mig.STEP_FLIPPED

    async def test_graph_remap_refuses_before_the_flip(self, redis_client, frozen, idmap):
        """Spec D6.5: remapped rows before the flip point at ids the live
        collection lacks, and _scope_verdict's resolve-fail path empties the
        graph leg."""
        client = await _seeded()
        await _run_full(client, redis_client, frozen, idmap)
        with pytest.raises(mig.MigrationRefused, match="step"):
            await mig.graph_remap_step(_FakeGraph(), redis_client, settings=frozen,
                                       idmap_path=idmap)


# ---------------------------------------------------------------------------
# execute — the copy
# ---------------------------------------------------------------------------


class TestExecuteCopy:
    @pytest.fixture()
    async def migrated(self, redis_client, frozen, idmap):
        client = _IndexRecording(await _seeded())
        plan = await _run_full(client, redis_client, frozen, idmap)
        shadow = await _dump(client, mig.SHADOW_COLLECTION)
        return SimpleNamespace(client=client, plan=plan, shadow=shadow,
                               redis=redis_client, idmap=idmap, settings=frozen)

    async def test_every_point_lands_and_the_collapse_is_the_only_loss(self, migrated):
        assert len(migrated.shadow) == migrated.plan.expected_shadow_total == 16

    async def test_migratable_points_are_rekeyed_and_carry_their_payload(self, migrated):
        new_id = memory_point_id(WS, NS, T_PLAIN_A)
        assert _v1_point_id(T_PLAIN_A) not in migrated.shadow
        assert migrated.shadow[new_id].payload["text"] == T_PLAIN_A
        assert migrated.shadow[new_id].payload["workspace_id"] == WS

    async def test_the_absent_namespace_point_is_stamped_default(self, migrated):
        new_id = memory_point_id(WS, "default", T_NO_NS)
        assert migrated.shadow[new_id].payload["namespace"] == "default"

    async def test_repaired_text_is_rehomed(self, migrated):
        assert memory_point_id(WS, NS, T_REPAIRED) in migrated.shadow
        assert _v1_point_id(T_REPAIRED_OLD) not in migrated.shadow

    async def test_corpus_dream_profile_skill_keep_their_ids(self, migrated):
        for pid in (_v1_point_id(T_CORPUS_LEGACY),
                    _corpus_id("runbook.md", "ing-1", 0),
                    str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, "dream::c1::0")),
                    str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, "profile::m1::ws-alpha")),
                    str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, "skill::x"))):
            assert pid in migrated.shadow

    async def test_the_twin_merged_once_with_v2_text_and_v1_lifecycle(self, migrated):
        target = memory_point_id(WS, NS, T_TWIN)
        assert _v1_point_id(T_TWIN) not in migrated.shadow
        merged = migrated.shadow[target]
        assert merged.payload["status"] == "archived"
        assert merged.payload["confirmed_count"] == 3

    async def test_quarantine_keeps_its_id_and_gains_the_sentinel(self, migrated):
        pid = _v1_point_id("an unattributable memory")
        payload = migrated.shadow[pid].payload
        assert payload["workspace_id"] == QUARANTINE_WORKSPACE
        assert payload["legacy_unscoped"] is True

    async def test_references_are_rewritten_through_the_map(self, migrated):
        owner = migrated.shadow[memory_point_id(WS, NS, T_REF_OWNER)]
        assert owner.payload["superseded_by"] == memory_point_id(WS, NS, T_PLAIN_A)
        contested = migrated.shadow[memory_point_id(WS, NS, T_CONTESTED)]
        assert contested.payload["contested_with"] == memory_point_id(WS, NS, T_PLAIN_B)

    async def test_a_pre_existing_dangling_reference_stays_dangling(self, migrated):
        """It must not be invented into something, and it must not become a
        NEW break the verify pass blames on the migration."""
        dangler = migrated.shadow[memory_point_id(WS, NS, T_DANGLER)]
        assert dangler.payload["superseded_by"] not in migrated.shadow

    async def test_vectors_are_carried_bit_for_bit(self, migrated):
        source = await _dump(migrated.client, "firekeep_memory")
        assert source[_v1_point_id(T_PLAIN_A)].vector == \
               migrated.shadow[memory_point_id(WS, NS, T_PLAIN_A)].vector

    async def test_shadow_inherits_the_source_collection_params(self, migrated):
        src = await migrated.client.get_collection("firekeep_memory")
        dst = await migrated.client.get_collection(mig.SHADOW_COLLECTION)
        assert dst.config.params.vectors == src.config.params.vectors

    async def test_the_three_payload_indexes_are_created(self, migrated):
        assert migrated.client.indexes[mig.SHADOW_COLLECTION] == {
            "tags", "namespace", "workspace_id"}

    async def test_the_idmap_artifact_and_its_redis_mirror_agree(self, migrated):
        lines = [json.loads(x) for x in
                 Path(migrated.idmap).read_text(encoding="utf-8").splitlines() if x]
        on_disk = {row["old"]: row["new"] for row in lines}
        assert on_disk == migrated.plan.mapping
        in_redis = await migrated.redis.hgetall(mig.IDMAP_REDIS_KEY)
        assert in_redis == on_disk

    async def test_the_source_collection_is_untouched(self, migrated):
        source = await _dump(migrated.client, "firekeep_memory")
        assert len(source) == 17
        assert _v1_point_id(T_PLAIN_A) in source


class TestQuarantineIsInvisible:
    """Both recall legs, not one: the sentinel is a workspace no principal holds,
    and `legacy_unscoped` denies the graph row that names the same memory."""

    async def test_a_real_workspace_filtered_search_cannot_see_it(
            self, redis_client, frozen, idmap):
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        client = await _seeded()
        await _run_full(client, redis_client, frozen, idmap)
        hits = await client.query_points(
            collection_name=mig.SHADOW_COLLECTION,
            query=_vec(13), limit=50,
            query_filter=Filter(must=[FieldCondition(
                key="workspace_id", match=MatchValue(value=WS))]),
        )
        quarantined = {_v1_point_id("an unattributable memory"),
                       _v1_point_id("an explicitly null-workspace memory")}
        assert quarantined.isdisjoint({str(p.id) for p in hits.points})

    async def test_the_sentinel_value_is_the_shared_constant(self):
        assert mig.QUARANTINE_WORKSPACE == QUARANTINE_WORKSPACE == "__quarantine__"


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


class TestResume:
    async def test_a_planted_crash_resumes_to_the_uncrashed_result(
            self, redis_client, frozen, idmap, tmp_path):
        clean = _IndexRecording(await _seeded())
        clean_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        clean_map = str(tmp_path / "clean.jsonl")
        await _run_full(clean, clean_redis, frozen, clean_map)
        expected = await _dump(clean, mig.SHADOW_COLLECTION)

        client = _IndexRecording(await _seeded())
        await mig.dry_run(client, source="firekeep_memory", idmap_path=idmap)
        crashy = _CrashAfter(client, after=2)
        with pytest.raises(RuntimeError, match="planted crash"):
            await mig.execute(crashy, redis_client, settings=frozen,
                              idmap_path=idmap, source="firekeep_memory", batch_size=2)
        partial = await _dump(client, mig.SHADOW_COLLECTION)
        assert 0 < len(partial) < len(expected)  # a genuinely half-done copy

        state = await mig.read_state(redis_client)
        assert state["step"] == mig.STEP_COPY
        assert state["status"] == mig.STATUS_IN_PROGRESS

        await mig.resume(client, redis_client, settings=frozen,
                         idmap_path=idmap, batch_size=2)
        after = await _dump(client, mig.SHADOW_COLLECTION)
        assert {k: (v.payload, v.vector) for k, v in after.items()} == \
               {k: (v.payload, v.vector) for k, v in expected.items()}

    async def test_resume_on_a_finished_copy_is_a_no_op(
            self, redis_client, frozen, idmap):
        client = _IndexRecording(await _seeded())
        await _run_full(client, redis_client, frozen, idmap)
        before = await _dump(client, mig.SHADOW_COLLECTION)
        await mig.resume(client, redis_client, settings=frozen, idmap_path=idmap)
        after = await _dump(client, mig.SHADOW_COLLECTION)
        assert {k: (v.payload, v.vector) for k, v in after.items()} == \
               {k: (v.payload, v.vector) for k, v in before.items()}


# ---------------------------------------------------------------------------
# verify — exact and fatal
# ---------------------------------------------------------------------------


class TestVerify:
    @pytest.fixture()
    async def ready(self, redis_client, frozen, idmap):
        client = _IndexRecording(await _seeded())
        plan = await _run_full(client, redis_client, frozen, idmap)
        flipped = Settings(MIGRATION_FREEZE=True, QDRANT_COLLECTION=mig.SHADOW_COLLECTION)
        await mig.mark_flipped(redis_client, settings=flipped)
        await mig.graph_remap_step(_FakeGraph(), redis_client, settings=flipped,
                                   idmap_path=idmap)
        await mig.fold_hashes_step(redis_client, client, settings=flipped,
                                   idmap_path=idmap)
        return SimpleNamespace(client=client, plan=plan, redis=redis_client,
                               idmap=idmap, settings=flipped)

    async def test_a_faithful_migration_passes_every_check(self, ready):
        report = await mig.verify(ready.client, ready.redis, settings=ready.settings,
                                  idmap_path=ready.idmap)
        assert report.ok, report.failures
        assert report.checks["bucket_counts"] is True
        assert report.checks["fidelity_sample"] is True
        assert report.checks["config_params"] is True
        assert report.checks["payload_indexes"] is True
        assert report.checks["search_parity"] is True
        assert report.checks["dangling_references"] is True
        assert report.checks["no_v1_ids_outside_quarantine"] is True

    async def test_planted_count_drift_is_fatal(self, ready):
        await ready.client.delete(
            collection_name=mig.SHADOW_COLLECTION,
            points_selector=[memory_point_id(WS, NS, T_PLAIN_A)])
        report = await mig.verify(ready.client, ready.redis, settings=ready.settings,
                                  idmap_path=ready.idmap)
        assert not report.ok
        assert report.checks["bucket_counts"] is False

    async def test_a_planted_vector_mutation_is_fatal(self, ready):
        target = memory_point_id(WS, NS, T_PLAIN_A)
        rec = (await ready.client.retrieve(mig.SHADOW_COLLECTION, [target],
                                           with_payload=True, with_vectors=True))[0]
        await ready.client.upsert(mig.SHADOW_COLLECTION, points=[PointStruct(
            id=target, vector=[v + 0.5 for v in rec.vector], payload=rec.payload)])
        report = await mig.verify(ready.client, ready.redis, settings=ready.settings,
                                  idmap_path=ready.idmap)
        assert not report.ok
        assert report.checks["fidelity_sample"] is False

    async def test_a_planted_payload_mutation_is_fatal(self, ready):
        target = memory_point_id(WS, NS, T_PLAIN_B)
        rec = (await ready.client.retrieve(mig.SHADOW_COLLECTION, [target],
                                           with_payload=True, with_vectors=True))[0]
        payload = dict(rec.payload)
        payload["project"] = "somebody-elses-project"
        await ready.client.upsert(mig.SHADOW_COLLECTION, points=[PointStruct(
            id=target, vector=rec.vector, payload=payload)])
        report = await mig.verify(ready.client, ready.redis, settings=ready.settings,
                                  idmap_path=ready.idmap)
        assert not report.ok
        assert report.checks["fidelity_sample"] is False

    async def test_a_planted_dangling_reference_is_fatal(self, ready):
        target = memory_point_id(WS, NS, T_REF_OWNER)
        rec = (await ready.client.retrieve(mig.SHADOW_COLLECTION, [target],
                                           with_payload=True, with_vectors=True))[0]
        payload = dict(rec.payload)
        payload["superseded_by"] = str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, "nowhere"))
        await ready.client.upsert(mig.SHADOW_COLLECTION, points=[PointStruct(
            id=target, vector=rec.vector, payload=payload)])
        report = await mig.verify(ready.client, ready.redis, settings=ready.settings,
                                  idmap_path=ready.idmap)
        assert not report.ok
        assert report.checks["dangling_references"] is False

    async def test_a_v1_id_surviving_outside_quarantine_is_fatal(self, ready):
        text = "a memory that was never re-keyed"
        await ready.client.upsert(mig.SHADOW_COLLECTION, points=[PointStruct(
            id=_v1_point_id(text), vector=_vec(77), payload=_mem(text))])
        report = await mig.verify(ready.client, ready.redis, settings=ready.settings,
                                  idmap_path=ready.idmap)
        assert not report.ok
        assert report.checks["no_v1_ids_outside_quarantine"] is False

    async def test_a_missing_payload_index_is_fatal(self, ready):
        ready.client.indexes[mig.SHADOW_COLLECTION].discard("workspace_id")
        report = await mig.verify(ready.client, ready.redis, settings=ready.settings,
                                  idmap_path=ready.idmap)
        assert not report.ok
        assert report.checks["payload_indexes"] is False

    async def test_a_source_that_moved_since_the_freeze_aborts(self, ready):
        await ready.client.upsert("firekeep_memory", points=[PointStruct(
            id=_v1_point_id("a thaw-window write"), vector=_vec(66),
            payload=_mem("a thaw-window write"))])
        with pytest.raises(mig.MigrationRefused, match="fingerprint|points_count"):
            await mig.verify(ready.client, ready.redis, settings=ready.settings,
                             idmap_path=ready.idmap)


# ---------------------------------------------------------------------------
# graph remap
# ---------------------------------------------------------------------------


class _FakeGraph:
    """Records Cypher and answers the two reads `graph_remap` performs.

    Not a Neo4j substitute: it exists so the remap's *decisions* — which nodes
    are stamped, which memory_ids change, that the constraint is created — are
    asserted on the parameters actually sent, rather than on a mock's echo.
    """

    def __init__(self, chain_nodes=None, memory_ref_ids=None):
        self._chain_nodes = chain_nodes or []
        self._memory_ref_ids = memory_ref_ids or []
        self.reads: list[tuple[str, dict]] = []
        self.writes: list[tuple[str, dict]] = []

    async def _execute_read(self, query, params):
        self.reads.append((query, params))
        if "MemoryRef" in query:
            return [{"vector_id": v} for v in self._memory_ref_ids]
        return list(self._chain_nodes)

    async def _execute_write(self, query, params):
        self.writes.append((query, params))
        return []

    def cypher(self) -> str:
        return "\n".join(q for q, _ in self.writes)

    def params_for(self, needle: str) -> list[dict]:
        return [p for q, p in self.writes if needle in q]


class TestGraphRemap:
    def _hash(self, text: str, length: int = 32) -> str:
        import hashlib
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]

    async def test_chain_node_memory_ids_are_translated_and_unmapped_pass_through(self):
        old, new = _v1_point_id(T_PLAIN_A), memory_point_id(WS, NS, T_PLAIN_A)
        corpus_id = _corpus_id("r.md", "i", 0)
        graph = _FakeGraph(chain_nodes=[
            {"eid": "n1", "id": "abc", "description": "did a thing",
             "memory_ids": [old, corpus_id]},
        ])
        await mig.graph_remap(graph, {old: new}, content_hash_length=32)
        writes = graph.params_for("memory_ids")
        assert writes, "no memory_ids write issued"
        rows = writes[0]["rows"]
        assert rows[0]["memory_ids"] == [new, corpus_id]

    async def test_a_node_whose_ids_do_not_change_is_not_rewritten(self):
        graph = _FakeGraph(chain_nodes=[
            {"eid": "n1", "id": "abc", "description": "d", "memory_ids": ["untouched"]},
        ])
        await mig.graph_remap(graph, {"other": "x"}, content_hash_length=32)
        assert not graph.params_for("memory_ids")

    async def test_a_collapse_deduplicates_the_memory_ids_list(self):
        """Two v1 ids folding into one v2 id must not leave a doubled entry."""
        a, b = _v1_point_id("a"), _v1_point_id("b")
        graph = _FakeGraph(chain_nodes=[
            {"eid": "n1", "id": "abc", "description": "d", "memory_ids": [a, b]},
        ])
        await mig.graph_remap(graph, {a: "SAME", b: "SAME"}, content_hash_length=32)
        assert graph.params_for("memory_ids")[0]["rows"][0]["memory_ids"] == ["SAME"]

    async def test_legacy_unscoped_is_stamped_on_v1_keyed_chain_nodes_only(self):
        legacy_text = "ran the migration"
        graph = _FakeGraph(chain_nodes=[
            {"eid": "legacy", "id": self._hash(legacy_text),
             "description": legacy_text, "memory_ids": []},
            {"eid": "scoped", "id": self._hash("anything else"),
             "description": "a v2-keyed node", "memory_ids": []},
        ])
        await mig.graph_remap(graph, {}, content_hash_length=32)
        stamped = graph.params_for("legacy_unscoped")
        assert stamped, "nothing stamped"
        assert [r["eid"] for r in stamped[0]["rows"]] == ["legacy"]

    async def test_the_hash_length_setting_is_honoured(self):
        text = "ran the migration"
        graph = _FakeGraph(chain_nodes=[
            {"eid": "legacy", "id": self._hash(text, 16), "description": text,
             "memory_ids": []},
        ])
        await mig.graph_remap(graph, {}, content_hash_length=16)
        assert [r["eid"] for r in graph.params_for("legacy_unscoped")[0]["rows"]] == \
               ["legacy"]

    async def test_memory_refs_are_rewritten_and_collisions_merged(self):
        old, new = _v1_point_id(T_PLAIN_A), memory_point_id(WS, NS, T_PLAIN_A)
        graph = _FakeGraph(memory_ref_ids=[old, new])
        report = await mig.graph_remap(graph, {old: new}, content_hash_length=32)
        assert report["memory_ref_collisions"] == 1
        assert "MemoryRef" in graph.cypher()

    async def test_the_uniqueness_constraint_is_created_last(self):
        graph = _FakeGraph()
        await mig.graph_remap(graph, {}, content_hash_length=32)
        constraint = [q for q, _ in graph.writes if "CONSTRAINT" in q]
        assert constraint
        assert "IS UNIQUE" in constraint[-1]
        assert graph.writes[-1][0] == constraint[-1]


# ---------------------------------------------------------------------------
# redis hash folds
# ---------------------------------------------------------------------------


class TestFoldHashes:
    async def test_fields_are_translated_and_skill_ids_pass_through(
            self, redis_client, frozen, idmap):
        client = await _seeded()
        plan = await _run_full(client, redis_client, frozen, idmap)
        old, new = _v1_point_id(T_PLAIN_A), memory_point_id(WS, NS, T_PLAIN_A)
        skill_id = str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, "skill::x"))
        await redis_client.hset(mig.ACCESS_COUNTS_KEY, mapping={old: "4", skill_id: "2"})
        await redis_client.hset(mig.LAST_RECALLED_KEY,
                                mapping={old: "2026-01-01T00:00:00+00:00"})

        report = await mig.fold_redis_hashes(redis_client, plan.mapping, client,
                                             shadow=mig.SHADOW_COLLECTION)
        counts = await redis_client.hgetall(mig.ACCESS_COUNTS_KEY)
        assert counts == {new: "4", skill_id: "2"}
        assert old not in await redis_client.hgetall(mig.LAST_RECALLED_KEY)
        assert report["translated"][mig.ACCESS_COUNTS_KEY] == 1
        assert report["residual"][mig.ACCESS_COUNTS_KEY] == 0

    async def test_a_collapse_sums_counts_and_keeps_the_latest_recall(
            self, redis_client, frozen, idmap):
        client = await _seeded()
        plan = await _run_full(client, redis_client, frozen, idmap)
        v1_twin = _v1_point_id(T_TWIN)
        target = memory_point_id(WS, NS, T_TWIN)
        await redis_client.hset(mig.ACCESS_COUNTS_KEY,
                                mapping={v1_twin: "5", target: "3"})
        await redis_client.hset(mig.LAST_RECALLED_KEY, mapping={
            v1_twin: "2026-03-03T00:00:00+00:00",
            target: "2026-01-01T00:00:00+00:00"})
        await mig.fold_redis_hashes(redis_client, plan.mapping, client,
                                    shadow=mig.SHADOW_COLLECTION)
        assert await redis_client.hget(mig.ACCESS_COUNTS_KEY, target) == "8"
        assert await redis_client.hget(mig.LAST_RECALLED_KEY, target) == \
            "2026-03-03T00:00:00+00:00"

    async def test_a_field_naming_nothing_is_reported_as_the_measured_loss(
            self, redis_client, frozen, idmap):
        client = await _seeded()
        plan = await _run_full(client, redis_client, frozen, idmap)
        gone = str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, "hard-deleted long ago"))
        await redis_client.hset(mig.ACCESS_COUNTS_KEY, mapping={gone: "9"})
        report = await mig.fold_redis_hashes(redis_client, plan.mapping, client,
                                             shadow=mig.SHADOW_COLLECTION)
        assert report["residual"][mig.ACCESS_COUNTS_KEY] == 1
        assert report["unprobed"][mig.ACCESS_COUNTS_KEY] == 0

    async def test_a_malformed_field_is_loss_without_asking_qdrant(
            self, redis_client, frozen, idmap):
        """A field that is not a uuid cannot name a point in this store, and it
        must not be allowed to make the probe reject the batch it travelled in
        — that would report every id beside it as lost too."""
        client = await _seeded()
        plan = await _run_full(client, redis_client, frozen, idmap)
        new = memory_point_id(WS, NS, T_PLAIN_A)
        await redis_client.hset(mig.ACCESS_COUNTS_KEY,
                                mapping={"not-a-uuid": "9",
                                         _v1_point_id(T_PLAIN_A): "3"})
        report = await mig.fold_redis_hashes(redis_client, plan.mapping, client,
                                             shadow=mig.SHADOW_COLLECTION)
        assert report["residual"][mig.ACCESS_COUNTS_KEY] == 1
        assert await redis_client.hget(mig.ACCESS_COUNTS_KEY, new) == "3"

    async def test_a_non_empty_flushing_key_refuses(self, redis_client, frozen, idmap):
        """A live flush would write the counts back under the OLD ids after the
        fold — the fold has to be fenced against a worker that isn't stopped."""
        client = await _seeded()
        plan = await _run_full(client, redis_client, frozen, idmap)
        await redis_client.hset(f"{mig.ACCESS_COUNTS_KEY}:flushing", "x", "1")
        with pytest.raises(mig.MigrationRefused, match="flushing"):
            await mig.fold_redis_hashes(redis_client, plan.mapping, client,
                                        shadow=mig.SHADOW_COLLECTION)

    async def test_a_byte_returning_redis_client_is_handled(
            self, frozen, idmap):
        """`app.state.redis_client` is built WITHOUT decode_responses
        (main.py:692), so the hash comes back as bytes in production."""
        raw = fakeredis.aioredis.FakeRedis(decode_responses=False)
        client = await _seeded()
        plan = await _run_full(client, raw, frozen, idmap)
        old, new = _v1_point_id(T_PLAIN_A), memory_point_id(WS, NS, T_PLAIN_A)
        await raw.hset(mig.ACCESS_COUNTS_KEY, mapping={old: "4"})
        await mig.fold_redis_hashes(raw, plan.mapping, client,
                                    shadow=mig.SHADOW_COLLECTION)
        assert await raw.hget(mig.ACCESS_COUNTS_KEY, new) == b"4"


# ---------------------------------------------------------------------------
# The completion marker + inertness
# ---------------------------------------------------------------------------


class TestCompletionAndInertness:
    async def test_verify_writes_the_marker_task_8_reads(self, redis_client, frozen, idmap):
        client = _IndexRecording(await _seeded())
        await _run_full(client, redis_client, frozen, idmap)
        flipped = Settings(MIGRATION_FREEZE=True, QDRANT_COLLECTION=mig.SHADOW_COLLECTION)
        await mig.mark_flipped(redis_client, settings=flipped)
        await mig.graph_remap_step(_FakeGraph(), redis_client, settings=flipped,
                                   idmap_path=idmap)
        await mig.fold_hashes_step(redis_client, client, settings=flipped,
                                   idmap_path=idmap)
        assert not await redis_client.exists(mig.MIGRATION_COMPLETE_KEY)
        report = await mig.verify(client, redis_client, settings=flipped, idmap_path=idmap)
        assert report.ok
        assert await redis_client.exists(mig.MIGRATION_COMPLETE_KEY)

    async def test_a_failed_verify_does_not_write_the_marker(
            self, redis_client, frozen, idmap):
        client = _IndexRecording(await _seeded())
        await _run_full(client, redis_client, frozen, idmap)
        flipped = Settings(MIGRATION_FREEZE=True, QDRANT_COLLECTION=mig.SHADOW_COLLECTION)
        await mig.mark_flipped(redis_client, settings=flipped)
        await mig.graph_remap_step(_FakeGraph(), redis_client, settings=flipped,
                                   idmap_path=idmap)
        await mig.fold_hashes_step(redis_client, client, settings=flipped,
                                   idmap_path=idmap)
        await client.delete(collection_name=mig.SHADOW_COLLECTION,
                            points_selector=[memory_point_id(WS, NS, T_PLAIN_A)])
        report = await mig.verify(client, redis_client, settings=flipped, idmap_path=idmap)
        assert not report.ok
        assert not await redis_client.exists(mig.MIGRATION_COMPLETE_KEY)

    def test_importing_the_module_runs_nothing(self):
        """It ships INERT: no celery task, no beat entry, no import-time work."""
        import inspect

        src = inspect.getsource(mig)
        assert "@celery_app.task" not in src
        assert "shared_task" not in src
        assert "beat_schedule" not in src

    def test_it_is_not_registered_with_celery_beat(self):
        from app.workers import sleep_cycle

        schedule = getattr(sleep_cycle.celery_app.conf, "beat_schedule", {}) or {}
        assert not any("memory_identity_migration" in str(v) for v in schedule.values())

    def test_the_cli_exposes_exactly_the_seven_subcommands(self):
        parser = mig.build_parser()
        actions = [a for a in parser._actions if a.dest == "command"]
        assert actions and set(actions[0].choices) == {
            "dry-run", "execute", "mark-flipped", "graph-remap", "fold-hashes",
            "verify", "resume"}

    def test_the_idmap_default_is_overridable(self):
        parser = mig.build_parser()
        assert parser.parse_args(["dry-run"]).idmap_path == mig.DEFAULT_IDMAP_PATH
        assert parser.parse_args(["dry-run", "--idmap-path", "/x.jsonl"]).idmap_path \
            == "/x.jsonl"
