"""Spec §4.5 option (a) — the committed-generation gate.

The staged re-ingest was never atomic to recall: new chunks are upserted
one by one before the old generation is deleted, so mixed generations were
recallable mid-swap and a mid-ingest failure left a partial generation
recallable until the next successful sweep. Chunks are now written
``committed: False`` and flipped live by ONE ``set_payload`` at swap
completion; recall's GENERATION_GUARD (cortex/app/db/visibility.py, wired
at egress in Task 6) excludes anything still ``False``.
"""

from __future__ import annotations

import pytest
from qdrant_client.models import FieldCondition, Filter

from corpus.models import Chunk, ChunkMetadata
from corpus.pipeline import ingest_document
from corpus.store import commit_generation, store_chunks

SOURCE = "docdex:m1:f1"
CONTENT_V1 = "CSG handles billing. Provisioning triggers CSG. " * 20
CONTENT_V2 = "Billing moved to Zuora. Provisioning now triggers Zuora. " * 20
CONTENT_V3 = "Zuora was replaced by Stripe last quarter. " * 20


def _chunk(text="body text", name=SOURCE):
    return Chunk(content=text, metadata=ChunkMetadata(
        source_name=name, source_type="document",
        chunk_index=0, total_chunks=1))


# ---------------------------------------------------------------------------
# A stateful fake that honors filters — a fake that ignores them proves
# nothing (the search-skill lesson).
# ---------------------------------------------------------------------------


def _get_path(payload: dict, dotted: str):
    cur = payload
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _matches(payload: dict, flt: Filter | None) -> bool:
    """Real Qdrant filter semantics for the conditions the pipeline builds."""
    if flt is None:
        return True

    def hit(cond) -> bool:
        return (isinstance(cond, FieldCondition)
                and _get_path(payload, cond.key) == cond.match.value)

    return (all(hit(c) for c in (flt.must or []))
            and not any(hit(c) for c in (flt.must_not or [])))


class _RawQdrantFake:
    """The slice of the raw AsyncQdrantClient ``commit_generation`` touches."""

    def __init__(self, store: "StatefulVectorFake") -> None:
        self._store = store
        self.fail_next_set_payload = False
        self.set_payload_calls: list[dict] = []

    async def set_payload(self, *, collection_name, payload, points, **_kw):
        assert collection_name == self._store._collection
        self._store.call_order.append("set_payload")
        self.set_payload_calls.append({"payload": payload, "points": points})
        if self.fail_next_set_payload:
            self.fail_next_set_payload = False
            raise RuntimeError("set_payload died")
        assert isinstance(points, Filter), "commit must flip by FILTER, not ids"
        for pl in self._store.points.values():
            if _matches(pl, points):
                pl.update(payload)


class StatefulVectorFake:
    """VectorClient stand-in with real generation state.

    ``upsert`` mirrors the real payload shape — the metadata dict lands
    NESTED (vector.py's upsert does not promote ``committed`` to the top
    level today) — while ``set_payload`` writes top-level keys the way the
    raw client does. The gate's observable contract is therefore: committed
    generations carry top-level ``committed: True``; uncommitted ones do
    not.
    """

    def __init__(self) -> None:
        self._collection = "firekeep_memory"
        self._client = _RawQdrantFake(self)
        self.points: dict[str, dict] = {}
        self.call_order: list[str] = []

    async def upsert(self, *, text, metadata, point_id=None, **_kw) -> str:
        self.call_order.append("upsert")
        pid = point_id or text
        self.points[pid] = {
            "text": text,
            "source": metadata.get("source"),
            "metadata": dict(metadata),
        }
        return pid

    async def delete_by_filter(self, flt) -> None:
        self.call_order.append("delete")
        self.points = {
            pid: pl for pid, pl in self.points.items() if not _matches(pl, flt)
        }


async def _ingest(fake, content, name=SOURCE):
    return await ingest_document(
        content=content, source_name=name, source_type="document",
        vector_client=fake, redis_client=None,
        workspace_id="ws1", member_id="m1",
    )


def _source_points(fake, name=SOURCE) -> list[dict]:
    return [pl for pl in fake.points.values()
            if pl["metadata"].get("source_name") == name]


# ---------------------------------------------------------------------------
# Written gated
# ---------------------------------------------------------------------------


class TestChunksWrittenUncommitted:
    @pytest.mark.asyncio
    async def test_store_chunks_stamps_committed_false(self, fake_vector):
        await store_chunks([_chunk()], fake_vector, ingest_id="r1",
                           workspace_id="ws1", member_id="m1")
        (call,) = fake_vector.upserts
        assert call["metadata"]["committed"] is False


# ---------------------------------------------------------------------------
# The commit point
# ---------------------------------------------------------------------------


class TestCommitFlipsTheGeneration:
    @pytest.mark.asyncio
    async def test_successful_ingest_leaves_every_chunk_committed(self):
        fake = StatefulVectorFake()
        await _ingest(fake, CONTENT_V1)
        pts = _source_points(fake)
        assert pts
        assert all(pl.get("committed") is True for pl in pts)

    @pytest.mark.asyncio
    async def test_commit_runs_after_every_upsert_and_before_the_sweep(self):
        fake = StatefulVectorFake()
        await _ingest(fake, CONTENT_V1)
        last_upsert = max(
            i for i, c in enumerate(fake.call_order) if c == "upsert")
        commit = fake.call_order.index("set_payload")
        first_delete = min(
            i for i, c in enumerate(fake.call_order) if c == "delete")
        assert last_upsert < commit < first_delete

    @pytest.mark.asyncio
    async def test_commit_filter_pins_source_and_run(self):
        """ONE set_payload scoped by (source_name, ingest_id): scoping by
        source alone would resurrect an earlier run's orphaned generation."""
        fake = StatefulVectorFake()
        await _ingest(fake, CONTENT_V1)
        (call,) = fake._client.set_payload_calls
        assert call["payload"] == {"committed": True}
        conds = {c.key: c.match.value for c in call["points"].must
                 if isinstance(c, FieldCondition)}
        run_id = _source_points(fake)[0]["metadata"]["ingest_id"]
        assert conds["metadata.source_name"] == SOURCE
        assert conds["metadata.ingest_id"] == run_id


# ---------------------------------------------------------------------------
# Failure between store and commit
# ---------------------------------------------------------------------------


class TestFailureBetweenStoreAndCommit:
    @pytest.mark.asyncio
    async def test_partial_generation_stays_gated_and_old_stays_live(self):
        fake = StatefulVectorFake()
        await _ingest(fake, CONTENT_V1)
        gen1 = {pid for pid, pl in fake.points.items()
                if pl["metadata"].get("source_name") == SOURCE}
        deletes_before = fake.call_order.count("delete")

        fake._client.fail_next_set_payload = True
        with pytest.raises(RuntimeError, match="set_payload died"):
            await _ingest(fake, CONTENT_V2)

        # The previous generation is untouched: present, committed, unswept.
        assert fake.call_order.count("delete") == deletes_before
        for pid in gen1:
            assert fake.points[pid].get("committed") is True
        # The partial new generation is written but never went live.
        orphans = [pl for pid, pl in fake.points.items()
                   if pid not in gen1
                   and pl["metadata"].get("source_name") == SOURCE]
        assert orphans
        for pl in orphans:
            assert pl.get("committed") is not True
            assert pl["metadata"]["committed"] is False


# ---------------------------------------------------------------------------
# The next successful ingest sweeps the orphans (the existing sweep)
# ---------------------------------------------------------------------------


class TestOrphanSweep:
    @pytest.mark.asyncio
    async def test_next_successful_ingest_sweeps_the_orphaned_generation(self):
        fake = StatefulVectorFake()
        await _ingest(fake, CONTENT_V1)
        fake._client.fail_next_set_payload = True
        with pytest.raises(RuntimeError):
            await _ingest(fake, CONTENT_V2)
        orphan_ids = {pid for pid, pl in fake.points.items()
                      if pl["metadata"].get("source_name") == SOURCE
                      and pl.get("committed") is not True}
        assert orphan_ids  # the failure above left a gated partial generation

        await _ingest(fake, CONTENT_V3)

        assert not orphan_ids & set(fake.points), "orphaned generation swept"
        pts = _source_points(fake)
        assert pts
        assert all(pl.get("committed") is True for pl in pts)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class TestCommitGenerationTransport:
    @pytest.mark.asyncio
    async def test_stand_in_without_raw_client_is_a_gated_noop(self):
        """cortex's tenancy tests drive the real pipeline with a bare
        recording fake; a vector client without the raw Qdrant handle gets
        no commit — its chunks stay gated, failing CLOSED at recall."""

        class Bare:
            async def upsert(self, **_kw):
                return "p"

        await commit_generation(Bare(), SOURCE, "r1")  # must not raise
