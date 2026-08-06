"""Everything written to the shared collection must carry the writer's tenancy.

WHY THESE EXIST. `VectorClient.search` applies `workspace_id` as a hard Qdrant
`must` filter, so a point stored with `workspace_id=null` is not "unscoped" —
it is UNREACHABLE from every recall path. Three write paths did exactly that:
`POST /corpus/ingest`, `POST /knowledge/ingest`, and `POST /skills`.

Measured against the live store. The ingested chunk scrolled back as
`{"source": "corpus", "workspace_id": null, "member_id": null}` while all five
PRE-EXISTING corpus chunks carried a real workspace. Same query, "how do I
rotate the sweep probe token": with `workspace_id=None` the chunk was the TOP
hit at 0.8193; with the caller's real workspace it was absent entirely. A probe
skill scored 0.877 at rank 1 unfiltered and vanished under the filter. The
`corpus_ingest` tool docstring promises "every chunk is searchable via
memory_recall"; CLAUDE.md says corpus chunks "appear in regular memory recall".

The startup `migrate_single_workspace` backfill healed orphans, which is why
this looked intermittent rather than broken: the damage window was "until
someone restarts cortex-api" — 24h on that box. A migration is not a substitute
for stamping at write time.
"""

from __future__ import annotations

import pytest

from corpus.models import Chunk, ChunkMetadata
from corpus.store import store_chunks


class _RecordingVector:
    def __init__(self):
        self.calls = []

    async def upsert(self, text, metadata, **kwargs):
        self.calls.append(metadata)
        return "point-id"


def _chunk(i=0):
    return Chunk(
        content=f"chunk {i}",
        metadata=ChunkMetadata(
            source_name="Runbook", source_type="wiki",
            chunk_index=i, total_chunks=1,
        ),
    )


class TestCorpusChunkTenancy:
    @pytest.mark.asyncio
    async def test_workspace_and_member_reach_the_chunk_payload(self):
        """The stamp `upsert` promotes to a top-level payload key.

        Without it the chunk is stored and then filtered out of every recall.
        """
        vector = _RecordingVector()
        await store_chunks(
            [_chunk()], vector,
            workspace_id="workspace-abc", member_id="member-1",
        )
        assert vector.calls[0]["workspace_id"] == "workspace-abc"
        assert vector.calls[0]["member_id"] == "member-1"

    @pytest.mark.asyncio
    async def test_unknown_tenancy_writes_no_key_rather_than_an_explicit_null(self):
        """Emitting `workspace_id=None` would be worse than omitting it.

        `upsert` promotes these keys to the top level, so an explicit null on a
        RE-INGEST would overwrite whatever the startup migration had backfilled
        — turning a healed chunk back into an unreachable one.
        """
        vector = _RecordingVector()
        await store_chunks([_chunk()], vector)
        assert "workspace_id" not in vector.calls[0]
        assert "member_id" not in vector.calls[0]

    @pytest.mark.asyncio
    async def test_pipeline_threads_tenancy_through_to_the_chunks(self):
        """`ingest_document` had NO workspace parameter at all — the reason
        every corpus and knowledge ingest wrote null."""
        from corpus import pipeline

        vector = _RecordingVector()

        async def _noop(*args, **kwargs):
            return None

        # Isolate the chunk write: the generation swap and Redis tracking are
        # not what this test is about.
        import corpus.pipeline as p
        orig_delete, orig_track = p.delete_source_chunks, p.track_source
        p.delete_source_chunks, p.track_source = _noop, _noop
        try:
            await pipeline.ingest_document(
                content="some body text that will chunk",
                source_name="Runbook", source_type="wiki",
                vector_client=vector, redis_client=None,
                workspace_id="workspace-abc", member_id="member-1",
            )
        finally:
            p.delete_source_chunks, p.track_source = orig_delete, orig_track

        assert vector.calls
        assert all(c["workspace_id"] == "workspace-abc" for c in vector.calls)


class TestDraftSkillTenancy:
    @pytest.mark.asyncio
    async def test_doc_draft_payload_carries_tenancy(self):
        """A draft skill with workspace_id=null is not "awaiting review" —
        it is unfindable by `skill_recall`, the briefing and memory_recall."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.skills.synthesizer import SkillSynthesizer

        settings = MagicMock()
        settings.QDRANT_COLLECTION = "c"
        synth = SkillSynthesizer(settings)
        stored = {}

        async def _store(content, payload, skill_id=None):
            stored.update(payload)
            return skill_id or "id"

        card = (
            "trigger: the widget queue wedges\n"
            "symptoms: backlog grows\n"
            "domain: ops\n"
            "verified_on: firekeep/2026-08\n"
            "---\n"
            "## Steps\n1. drain it\n"
        )
        with (
            patch.object(synth, "_call_llm_doc", new=AsyncMock(return_value=card)),
            patch.object(synth, "_store", new=_store),
            patch("app.skills.synthesizer.AsyncQdrantClient") as aq,
        ):
            aq.return_value.retrieve = AsyncMock(return_value=[])
            aq.return_value.close = AsyncMock()
            result = await synth.synthesize_from_document(
                source_name="Runbook", procedure_title="Drain the queue",
                workspace_id="workspace-abc", member_id="member-1",
                doc_content="body",
            )

        assert result["status"] == "drafted"
        assert stored["workspace_id"] == "workspace-abc"
        assert stored["member_id"] == "member-1"

    @pytest.mark.asyncio
    async def test_unknown_tenancy_omits_the_keys(self):
        """Same reasoning as the corpus case: never overwrite a backfill with
        an explicit null on re-draft (the point id is deterministic, so a
        re-ingest targets the SAME point)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.skills.synthesizer import SkillSynthesizer

        settings = MagicMock()
        settings.QDRANT_COLLECTION = "c"
        synth = SkillSynthesizer(settings)
        stored = {}

        async def _store(content, payload, skill_id=None):
            stored.update(payload)
            return skill_id or "id"

        card = (
            "trigger: t\nsymptoms: s\ndomain: d\nverified_on: v\n"
            "---\n## Steps\n1. x\n"
        )
        with (
            patch.object(synth, "_call_llm_doc", new=AsyncMock(return_value=card)),
            patch.object(synth, "_store", new=_store),
            patch("app.skills.synthesizer.AsyncQdrantClient") as aq,
        ):
            aq.return_value.retrieve = AsyncMock(return_value=[])
            aq.return_value.close = AsyncMock()
            await synth.synthesize_from_document(
                source_name="R", procedure_title="P", doc_content="body",
            )

        assert "workspace_id" not in stored
        assert "member_id" not in stored
