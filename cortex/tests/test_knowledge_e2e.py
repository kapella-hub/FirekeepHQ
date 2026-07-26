"""End-to-end: docs -> skills (ingest -> classify -> approve -> recall).

The spec's exit criterion (SP2), automated and preserved under the SP2.1 ASYNC
pipeline: a runbook is ingested through the real `POST /knowledge/ingest`
endpoint, which (SP2.1) now corpus-ingests synchronously (mocked at the same
seam as test_knowledge_api.py -- corpus internals have their own suite) and
enqueues a SINGLE `classify_and_draft_from_doc.delay(...)`; classification and
per-procedure skill drafting happen out-of-band in the worker.

There is no `task_always_eager` in this repo, so a naive "call the worker" would
merely re-enqueue the nested `draft_skill_from_doc.delay` calls (they would NOT
run), nothing would land in Qdrant, and the draft-exclusion assertions would
pass VACUOUSLY. To avoid that, the async path is driven with a two-stage capture
ONE LAYER DEEPER:

  * the ingest request's single `classify_and_draft_from_doc.delay(...)` is
    captured (worker NOT run in-request);
  * the captured args are replayed through the real `_run_classify_and_draft`
    (classifier mocked to the two procedure titles), which performs the real
    per-title `draft_skill_from_doc.delay(...)` calls -- those are captured too;
  * each captured per-title tuple is replayed through the real
    `_run_doc_synthesis` (the real `SkillSynthesizer.synthesize_from_document`),
    with only the doc-LLM call, the embedding HTTP call, and the Qdrant wire
    client mocked -- so two draft skills actually LAND in a single SHARED fake
    Qdrant store (exactly as the old sync harness did one layer up).

That same shared store backs the FastAPI-injected `vector._client` used by
both `GET /skills` (skills router) and `skills_section` (briefing), so the
draft-exclusion assertions are non-vacuous: the fake evaluates the real
Qdrant `must`/`must_not` FieldConditions built by production code, rather
than returning a canned list regardless of the filter (mirrors the
`_filtering_scroll` fakes in test_skill_api.py / test_briefing_sections_inprocess.py
and the `_filtering_query_points` fake in test_vector.py).

Flow:
  1. POST /knowledge/ingest -> corpus ingested + ONE classify_and_draft task
     enqueued (captured).
  2. Replay classify_and_draft (classifier -> 2 procedures) -> 2 per-title
     draft_skill_from_doc.delay calls captured.
  3. Replay each draft inline via _run_doc_synthesis -> 2 drafts stored
     (source_type=document, skill_status=draft, deterministic per-procedure ids).
  4. Drafts absent from skills_section() and GET /skills?status=active
     (and, for extra rigor, from VectorClient.search() -- Task 2b's
     must_not skill_status=draft path).
  5. PATCH /skills/{id} {skill_status: active} on ONE draft.
  6. That skill now appears in skills_section() for a matching goal; the
     other, still-draft, stays excluded everywhere.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.briefing import sections as S
from app.config import Settings
from app.db.vector import VectorClient
from app.knowledge.api import create_knowledge_router
from app.skills.api import create_skills_router
from app.skills.synthesizer import SKILL_NS, SkillSynthesizer
from app.workers.skill_synthesis import _run_classify_and_draft, _run_doc_synthesis

# ---------------------------------------------------------------------------
# Shared filter-evaluating fake Qdrant store
# ---------------------------------------------------------------------------

_MISSING = object()


def _field_matches(cond, payload: dict) -> bool:
    """Evaluate a single Qdrant FieldCondition against a payload dict (same
    approach as test_skill_api.py / test_briefing_sections_inprocess.py /
    test_vector.py's filter-aware fakes)."""
    value = payload.get(cond.key, _MISSING)
    match = cond.match
    if hasattr(match, "value"):
        return value == match.value
    if hasattr(match, "any"):
        if value is _MISSING:
            return False
        if isinstance(value, list):
            return any(v in match.any for v in value)
        return value in match.any
    return False


class _Point:
    def __init__(self, id_: str, payload: dict):
        self.id = id_
        self.payload = payload
        self.score = 0.9


class FakeQdrantStore:
    """A single in-memory Qdrant stand-in shared by BOTH the FastAPI
    dependency-injected `vector._client` (knowledge/skills routers, briefing
    section) AND the directly-constructed `AsyncQdrantClient` inside
    SkillSynthesizer.synthesize_from_document -- so a skill drafted by the
    real synthesis path is genuinely visible to the recall/list paths, and a
    PATCH genuinely flips what recall sees. Evaluates must/must_not
    FieldConditions for real instead of returning a canned list.
    """

    def __init__(self):
        self._data: dict[str, dict] = {}

    async def scroll(self, *, collection_name=None, scroll_filter=None,
                      limit=50, with_payload=True, with_vectors=False, **_kw):
        must = (scroll_filter.must or []) if scroll_filter else []
        must_not = (scroll_filter.must_not or []) if scroll_filter else []
        matched = [
            _Point(pid, dict(p)) for pid, p in self._data.items()
            if all(_field_matches(c, p) for c in must)
            and not any(_field_matches(c, p) for c in must_not)
        ]
        return matched[:limit], None

    async def retrieve(self, *, collection_name=None, ids, with_payload=True,
                        with_vectors=False, **_kw):
        return [_Point(str(i), dict(self._data[str(i)])) for i in ids if str(i) in self._data]

    async def upsert(self, *, collection_name=None, points, **_kw):
        for pt in points:
            self._data[str(pt.id)] = dict(pt.payload)

    async def set_payload(self, *, collection_name=None, payload, points, **_kw):
        for pid in points:
            key = str(pid)
            if key in self._data:
                self._data[key].update(payload)

    async def query_points(self, *, collection_name=None, query=None,
                            query_filter=None, limit=5, with_payload=True,
                            score_threshold=None, **_kw):
        must = (query_filter.must or []) if query_filter else []
        must_not = (query_filter.must_not or []) if query_filter else []
        matched = [
            _Point(pid, dict(p)) for pid, p in self._data.items()
            if all(_field_matches(c, p) for c in must)
            and not any(_field_matches(c, p) for c in must_not)
        ]
        result = MagicMock()
        result.points = matched[:limit]
        return result

    async def delete(self, *, collection_name=None, points_selector=None, **_kw):
        pass

    async def close(self):
        pass


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

RUNBOOK_CONTENT = (
    "# Ops Runbook\n\n"
    "## Restart the widget service\n"
    "1. SSH into the widget host.\n2. Run systemctl restart widget.\n3. Verify health check.\n\n"
    "## Rotate the API key\n"
    "1. Generate a new key in vault.\n2. Update consumers.\n3. Revoke the old key.\n"
)

PROCEDURE_TITLES = ["Restart the widget service", "Rotate the API key"]

_DOC_RAW_BY_TITLE = {
    "Restart the widget service": (
        "trigger: Restart the widget service when it hangs\n"
        "symptoms: Widget stuck in degraded state, health check failing\n"
        "domain: widgets\n"
        "verified_on: e2e/2026-07\n"
        "---\n## What's happening\nWidget wedged after a bad deploy.\n\n"
        "## Steps\n1. Restart the widget service.\n\n"
        "## Gotchas\n- Don't just reboot the host.\n\n"
        "## Example\nsystemctl restart widget"
    ),
    "Rotate the API key": (
        "trigger: Rotate the API key on schedule\n"
        "symptoms: API key nearing expiry\n"
        "domain: auth\n"
        "verified_on: e2e/2026-07\n"
        "---\n## What's happening\nKey is about to expire.\n\n"
        "## Steps\n1. Generate and roll the key.\n\n"
        "## Gotchas\n- Update all consumers before revoking the old key.\n\n"
        "## Example\nvault rotate-key"
    ),
}


def _mock_embed_client():
    embed_response = MagicMock()
    embed_response.status_code = 200
    embed_response.json = MagicMock(return_value={"data": [{"embedding": [0.1] * 768}]})
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = AsyncMock(return_value=embed_response)
    return mock_http


def _make_app(mock_vector, mock_redis, skills_settings):
    app = FastAPI()
    app.include_router(create_knowledge_router())
    app.include_router(create_skills_router(lambda: skills_settings))
    from app.main import get_redis, get_vector
    app.dependency_overrides[get_vector] = lambda: mock_vector
    app.dependency_overrides[get_redis] = lambda: mock_redis
    return app


# ---------------------------------------------------------------------------
# The end-to-end test
# ---------------------------------------------------------------------------


def test_docs_to_skills_end_to_end_ingest_classify_approve_recall():
    shared_store = FakeQdrantStore()
    mock_vector = MagicMock()
    mock_vector._client = shared_store
    mock_redis = AsyncMock()

    skills_settings = MagicMock()
    skills_settings.QDRANT_COLLECTION = "firekeep_memory"

    client = TestClient(_make_app(mock_vector, mock_redis, skills_settings))

    classify_result = {
        "primary_type": "procedural",
        "procedure_titles": PROCEDURE_TITLES,
        "ok": True,
        "note": "",
    }
    corpus_result = {
        "source_name": "Ops Runbook", "chunks_stored": 2,
        "entities_extracted": 0, "relationships_extracted": 0,
        "entity_types_discovered": [], "extraction_status": "skipped",
    }

    # Deterministic per-procedure ids -- computed up front (from uuid5, not from
    # the synthesis results) so the non-vacuity guard below stands on its own even
    # if the replay block is removed.
    restart_id = str(uuid.uuid5(SKILL_NS, "Ops Runbook::Restart the widget service"))
    rotate_id = str(uuid.uuid5(SKILL_NS, "Ops Runbook::Rotate the API key"))
    expected_ids = {restart_id, rotate_id}

    # --- Step 1: real POST /knowledge/ingest -> corpus ingested + ONE
    # classify_and_draft task enqueued. The worker is NOT run in-request; the
    # single .delay(...) call is captured so we can drive it one layer deeper.
    # api.py delegates to the ingest_core module (SP3 Task 1); set_ingest_status
    # is patched OUT at the ingest_core-module reference (status is not what
    # this test asserts; the source-level patch on app.knowledge.status is
    # inert because ingest_core.py bound its own name at import time).
    with (
        patch("app.knowledge.ingest_core.corpus_ingest_document",
              new=AsyncMock(return_value=corpus_result)) as mock_corpus,
        patch("app.knowledge.status.set_ingest_status", new=AsyncMock(return_value=None)),
        patch("app.knowledge.status.get_ingest_status", new=AsyncMock(return_value=None)),
        patch("app.knowledge.ingest_core.set_ingest_status", new=AsyncMock(return_value=None)),
        patch("app.knowledge.ingest_core.classify_and_draft_from_doc.delay") as mock_cad_delay,
    ):
        resp = client.post(
            "/knowledge/ingest",
            json={"content": RUNBOOK_CONTENT, "source_name": "Ops Runbook", "source_type": "wiki"},
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data["corpus_source"] == "Ops Runbook"
    assert data["status"] == "queued"
    mock_corpus.assert_awaited_once()
    mock_cad_delay.assert_called_once()
    ingest_args = mock_cad_delay.call_args.args
    ingest_kwargs = mock_cad_delay.call_args.kwargs

    # --- Step 2: replay the enqueued classify_and_draft one layer deeper. The
    # REAL _run_classify_and_draft runs with the classifier mocked to the two
    # procedure titles; its own per-title draft_skill_from_doc.delay(...) calls
    # are captured here (NOT run) so we can land them in Step 3. The worker's own
    # set_ingest_status reference is patched to a no-op -- it is load-bearing:
    # _run_classify_and_draft calls it (against its own real redis client) BEFORE
    # the draft-enqueue loop, and a live-redis failure there is swallowed and
    # would silently skip the loop, capturing zero drafts.
    with (
        patch("app.workers.skill_synthesis.classify_document", new=AsyncMock(return_value=classify_result)),
        patch("app.workers.skill_synthesis.set_ingest_status", new=AsyncMock(return_value=None)),
        patch("app.workers.skill_synthesis.draft_skill_from_doc.delay") as mock_draft_delay,
    ):
        cad_result = asyncio.run(_run_classify_and_draft(*ingest_args, **ingest_kwargs))

    assert cad_result["status"] == "classified"
    assert cad_result["skills_queued"] == 2
    assert mock_draft_delay.call_count == 2
    queued_calls = [(c.args, c.kwargs) for c in mock_draft_delay.call_args_list]

    # --- Step 3 (SABOTAGE POINT): replay each captured per-title draft INLINE
    # through the real _run_doc_synthesis + real SkillSynthesizer.synthesize_from_document,
    # with only the doc-LLM call, the embedding HTTP call, and the Qdrant wire
    # client mocked -- so two draft skills actually LAND in the shared fake store.
    # Commenting out this whole block leaves the non-vacuity guard below (draft
    # listing == the two ids) failing loudly, proving the test is non-vacuous.
    async def _fake_call_llm_doc(source_name, title, doc_content):
        return _DOC_RAW_BY_TITLE[title]

    async def _land_drafts():
        with (
            patch("app.skills.synthesizer.httpx.AsyncClient") as mock_http_cls,
            patch("app.skills.synthesizer.AsyncQdrantClient") as mock_qdrant_cls,
            patch.object(SkillSynthesizer, "_call_llm_doc", AsyncMock(side_effect=_fake_call_llm_doc)),
        ):
            mock_http_cls.side_effect = lambda *a, **k: _mock_embed_client()
            mock_qdrant_cls.return_value = shared_store  # same store the API routers see
            out = []
            for args, kwargs in queued_calls:
                out.append(await _run_doc_synthesis(*args, **kwargs))
            return out

    results = asyncio.run(_land_drafts())
    assert [r["status"] for r in results] == ["drafted", "drafted"]
    assert {r["id"] for r in results} == expected_ids  # deterministic ids, both distinct
    # --- end replay block ---------------------------------------------------

    # Confirm the drafts genuinely exist in the shared store -- otherwise the
    # exclusion assertions below would be vacuously true. This is the non-vacuity
    # guard: without landed drafts it fails loudly instead of passing vacuously.
    resp = client.get("/skills?status=draft")
    assert resp.status_code == 200
    assert {d["id"] for d in resp.json()} == {restart_id, rotate_id}

    # --- Step 4: drafts absent from recall for a matching goal ---------------
    goal = "restart the widget"

    sec = asyncio.run(S.skills_section(mock_vector, skills_settings, goal=goal, project=None))
    assert sec["status"] == "empty"
    assert sec["data"]["skills"] == []

    resp = client.get("/skills?status=active")
    assert resp.status_code == 200
    assert resp.json() == []

    resp = client.get("/skills")  # no status param -> defaults to active-only
    assert resp.status_code == 200
    assert resp.json() == []

    # Task 2b: also excluded from the memory_recall/search() path (real
    # VectorClient.search(), only _embed mocked -- same shared store).
    settings = Settings(
        QDRANT_HOST="localhost", QDRANT_PORT=6333, QDRANT_COLLECTION="firekeep_memory",
        EMBEDDING_DIM=768, LLM_BASE_URL="http://localhost:11434/v1",
        LLM_API_KEY="test-api-key", EMBEDDING_MODEL="test-embed",
    )
    vc = VectorClient(settings)
    vc._client = shared_store
    with patch.object(vc, "_embed", new=AsyncMock(return_value=[0.1] * 768)):
        recall_results = asyncio.run(vc.search(goal, top_k=10))
    recall_ids = {r["id"] for r in recall_results}
    assert restart_id not in recall_ids
    assert rotate_id not in recall_ids

    # --- Step 5: approve ONE draft (PATCH /skills/{id}, existing endpoint) ---
    resp = client.patch(f"/skills/{restart_id}", json={"skill_status": "active"})
    assert resp.status_code == 200
    assert resp.json()["skill_status"] == "active"

    # --- Step 6: approved skill now recalled; the other stays excluded -------
    sec = asyncio.run(S.skills_section(mock_vector, skills_settings, goal=goal, project=None))
    assert sec["status"] == "ok"
    ids = {s["id"] for s in sec["data"]["skills"]}
    assert restart_id in ids
    assert rotate_id not in ids

    resp = client.get("/skills?status=active")
    assert resp.status_code == 200
    active_ids = {d["id"] for d in resp.json()}
    assert active_ids == {restart_id}

    resp = client.get("/skills?status=draft")
    assert resp.status_code == 200
    draft_ids_after = {d["id"] for d in resp.json()}
    assert draft_ids_after == {rotate_id}  # only the un-approved one remains a draft
