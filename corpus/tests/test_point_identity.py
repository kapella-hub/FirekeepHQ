"""Spec §4.2 — the worst reviewed bug. uuid5(text) collapsed identical
text ACROSS MEMBERS into one point; deleting Alice's source deleted
Bob's chunk. Corpus points are now source-scoped."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from corpus.store import corpus_point_id, store_chunks
from corpus.models import Chunk, ChunkMetadata


def _chunk(text="same words", name="docdex:aa:bb"):
    return Chunk(content=text, metadata=ChunkMetadata(
        source_name=name, source_type="document",
        chunk_index=0, total_chunks=1))


def test_identical_text_two_sources_two_points():
    a = corpus_point_id("ws1", "docdex:alice:f1", "run1", 0)
    b = corpus_point_id("ws1", "docdex:bob:f1", "run1", 0)
    assert a != b


def test_same_source_same_run_is_deterministic():
    assert corpus_point_id("ws1", "s", "r", 3) == corpus_point_id("ws1", "s", "r", 3)


@pytest.mark.asyncio
async def test_store_chunks_passes_scoped_point_id(fake_vector):
    # fake_vector: the module's recording fake (corpus/tests/conftest.py);
    # it records upsert kwargs.
    await store_chunks([_chunk()], fake_vector, ingest_id="r1",
                       workspace_id="ws1", member_id="m1")
    (call,) = fake_vector.upserts
    assert call["point_id"] == corpus_point_id("ws1", "docdex:aa:bb", "r1", 0)


def test_uuid_namespace_pinned_to_cortex():
    """corpus is a shared lib and cannot import cortex, so store.py
    redeclares FIREKEEP_UUID_NAMESPACE locally; the two copies MUST stay
    equal or corpus and memory point identity silently fork within the
    shared collection."""
    cortex_dir = Path(__file__).resolve().parents[2] / "cortex"
    sys.path.insert(0, str(cortex_dir))
    try:
        vector = importlib.import_module("app.db.vector")
    except Exception:
        pytest.skip("cortex not importable from the corpus test env")
    finally:
        sys.path.remove(str(cortex_dir))
    from corpus import store
    assert store.FIREKEEP_UUID_NAMESPACE == vector.FIREKEEP_UUID_NAMESPACE
