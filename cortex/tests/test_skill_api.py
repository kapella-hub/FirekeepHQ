import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.models import SkillResponse
from app.skills.api import create_skills_router
from replay.config import ReplaySettings
from replay.reader import get_session_timeline


def _make_mock_point(skill_id="abc", trigger="Fix X", status="active"):
    p = MagicMock()
    p.id = skill_id
    p.payload = {
        "memory_type": "skill", "skill_status": status,
        "trigger": trigger, "symptoms": "Error Y",
        "content": f"trigger: {trigger}\nsymptoms: Error Y\ndomain: neo4j\nverified_on: test\n---\nbody",
        "domain": "neo4j", "skill_score": 0.75,
        "source_session_id": "s1", "project": "myproject",
        "agent_id": "me", "namespace": "default",
        "timestamp": "2026-05-23T00:00:00+00:00",
    }
    return p


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.QDRANT_COLLECTION = "firekeep_memory"
    s.QDRANT_HOST = "localhost"
    s.QDRANT_PORT = 6333
    s.LLM_BASE_URL = "http://ollama:11434/v1"
    s.LLM_MODEL = "qwen2.5:7b"
    s.EMBEDDING_MODEL = "nomic-embed-text"
    s.LLM_API_KEY = ""
    s.SKILL_SYNTHESIS_ENABLED = True
    return s


@pytest.fixture
def mock_vector(mock_settings):
    v = MagicMock()
    v._client = AsyncMock()
    v._embed = AsyncMock(return_value=[0.1] * 768)
    return v


def _make_app(mock_vector, mock_settings, redis_client=None):
    app = FastAPI()
    if redis_client is not None:
        app.state.redis_client = redis_client
    router = create_skills_router(lambda: mock_settings)
    app.include_router(router)
    from app.main import get_vector
    app.dependency_overrides[get_vector] = lambda: mock_vector
    return app


def test_list_skills_empty(mock_vector, mock_settings):
    mock_vector._client.scroll = AsyncMock(return_value=([], None))
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.get("/skills")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_skills_returns_active(mock_vector, mock_settings):
    point = _make_mock_point()
    mock_vector._client.scroll = AsyncMock(return_value=([point], None))
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.get("/skills?status=active")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["trigger"] == "Fix X"


def test_explicit_skill_recall_records_final_result_ids(
    mock_vector, mock_settings, mock_redis, monkeypatch
):
    kept = _make_mock_point(skill_id="kept", trigger="Fix X")
    discarded = _make_mock_point(skill_id="discarded", trigger="Other")

    async def _legacy_results(*_args, **_kwargs):
        # The non-semantic branch is narrowed by the endpoint after this helper
        # returns. Usage must be recorded from that FINAL response, not from the
        # wider candidate page.
        return [kept, discarded], False

    monkeypatch.setattr("app.skills.api.search_skill_points", _legacy_results)
    client = TestClient(_make_app(mock_vector, mock_settings, mock_redis))
    resp = client.get("/skills", params={"q": "fix x", "record_recall": True})

    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()] == ["kept"]
    mock_redis._pipeline.hincrby.assert_called_once_with(
        "memory:access_counts", "kept", 1
    )
    hsets = mock_redis._pipeline.hset.call_args_list
    assert len(hsets) == 1
    assert hsets[0].args[:2] == ("memory:last_recalled", "kept")
    mock_redis._pipeline.execute.assert_awaited_once()


def test_skill_listing_does_not_record_recall_usage(
    mock_vector, mock_settings, mock_redis
):
    point = _make_mock_point()
    mock_vector._client.scroll = AsyncMock(return_value=([point], None))
    client = TestClient(_make_app(mock_vector, mock_settings, mock_redis))

    resp = client.get("/skills?status=active")

    assert resp.status_code == 200
    mock_redis.pipeline.assert_not_called()


def test_skill_recall_usage_failure_is_best_effort(
    mock_vector, mock_settings, mock_redis
):
    point = _make_mock_point()
    mock_vector._client.scroll = AsyncMock(return_value=([point], None))
    mock_redis._pipeline.execute = AsyncMock(side_effect=RuntimeError("redis down"))
    client = TestClient(_make_app(mock_vector, mock_settings, mock_redis))

    resp = client.get("/skills", params={"record_recall": True})

    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()] == ["abc"]


def test_get_skill_not_found(mock_vector, mock_settings):
    mock_vector._client.retrieve = AsyncMock(return_value=[])
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.get("/skills/nonexistent-id")
    assert resp.status_code == 404


def test_patch_skill_status(mock_vector, mock_settings):
    point = _make_mock_point()
    mock_vector._client.retrieve = AsyncMock(return_value=[point])
    mock_vector._client.set_payload = AsyncMock()
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.patch("/skills/abc", json={"skill_status": "deprecated"})
    assert resp.status_code == 200
    mock_vector._client.set_payload.assert_called_once()
    mock_vector._embed.assert_not_awaited()
    mock_vector._client.upsert.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content", "A newly verified repair procedure"),
        ("trigger", "Use when the repaired service stalls"),
        ("symptoms", "Repeated timeout and health-check failures"),
    ],
)
def test_patch_semantic_fields_reembed_full_point_atomically(
    mock_vector, mock_settings, field, value
):
    point = _make_mock_point()
    point.payload["future_payload_field"] = {"must": "survive"}

    async def _upsert(*, collection_name, points):
        assert collection_name == "firekeep_memory"
        point.payload = dict(points[0].payload)

    mock_vector._client.retrieve = AsyncMock(return_value=[point])
    mock_vector._client.upsert = AsyncMock(side_effect=_upsert)
    mock_vector._client.set_payload = AsyncMock()
    client = TestClient(_make_app(mock_vector, mock_settings))

    resp = client.patch("/skills/abc", json={field: value})

    assert resp.status_code == 200
    assert resp.json()[field] == value
    mock_vector._embed.assert_awaited_once()
    embedded_text = mock_vector._embed.await_args.args[0]
    assert value in embedded_text
    mock_vector._client.upsert.assert_awaited_once()
    written = mock_vector._client.upsert.await_args.kwargs["points"][0]
    assert str(written.id) == "abc"
    assert written.payload[field] == value
    assert written.payload["future_payload_field"] == {"must": "survive"}
    assert written.payload["source_session_id"] == "s1"
    assert written.vector == [0.1] * 768
    mock_vector._client.set_payload.assert_not_awaited()


def test_patch_semantic_embed_failure_makes_no_write(mock_vector, mock_settings):
    point = _make_mock_point()
    mock_vector._client.retrieve = AsyncMock(return_value=[point])
    mock_vector._embed = AsyncMock(side_effect=RuntimeError("embeddings down"))
    mock_vector._client.set_payload = AsyncMock()
    mock_vector._client.upsert = AsyncMock()
    client = TestClient(
        _make_app(mock_vector, mock_settings), raise_server_exceptions=False
    )

    resp = client.patch("/skills/abc", json={"trigger": "New trigger"})

    assert resp.status_code == 500
    mock_vector._client.set_payload.assert_not_awaited()
    mock_vector._client.upsert.assert_not_awaited()


def test_patch_clears_needs_rereview(mock_vector, mock_settings):
    # a point exists with needs_rereview True; PATCH sets it False
    point = _make_mock_document_draft_point(skill_id="abc")
    mock_vector._client.retrieve = AsyncMock(return_value=[point])
    mock_vector._client.set_payload = AsyncMock()
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.patch("/skills/abc", json={"needs_rereview": False})
    assert resp.status_code == 200
    call = mock_vector._client.set_payload.await_args
    assert call.kwargs["payload"].get("needs_rereview") is False


def test_delete_skill(mock_vector, mock_settings):
    mock_vector._client.delete = AsyncMock()
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.delete("/skills/abc")
    assert resp.status_code == 204
    mock_vector._client.delete.assert_called_once()


# ---------------------------------------------------------------------------
# Provenance fields (source_type / content_class / source_doc /
# procedure_title / needs_rereview) — SP2 Task 1
# ---------------------------------------------------------------------------


def _make_mock_document_draft_point(skill_id="doc1"):
    p = MagicMock()
    p.id = skill_id
    p.payload = {
        "memory_type": "skill", "skill_status": "draft",
        "trigger": "Restart the widget", "symptoms": "Widget hangs",
        "content": "steps...", "domain": "ops", "skill_score": 0.0,
        "source_session_id": None, "project": "myproject", "agent_id": None,
        "namespace": "default", "timestamp": "2026-07-10T00:00:00+00:00",
        "source_type": "document", "content_class": "procedural",
        "source_doc": "wiki-runbook", "procedure_title": "Restart the widget",
        "needs_rereview": True,
    }
    return p


def test_skill_response_provenance_defaults():
    """SkillResponse round-trips the 5 new fields with safe defaults when omitted."""
    resp = SkillResponse(
        id="abc", trigger="t", symptoms="s", content="c", skill_status="active",
    )
    assert resp.source_type == "session"
    assert resp.content_class is None
    assert resp.source_doc is None
    assert resp.procedure_title is None
    assert resp.needs_rereview is False


def test_skill_response_provenance_roundtrip():
    """SkillResponse round-trips explicit provenance values (document-draft shape)."""
    resp = SkillResponse(
        id="abc", trigger="t", symptoms="s", content="c", skill_status="draft",
        source_type="document", content_class="procedural",
        source_doc="wiki-runbook", procedure_title="Restart the widget",
        needs_rereview=True,
    )
    assert resp.source_type == "document"
    assert resp.content_class == "procedural"
    assert resp.source_doc == "wiki-runbook"
    assert resp.procedure_title == "Restart the widget"
    assert resp.needs_rereview is True


def test_get_skill_document_draft_provenance(mock_vector, mock_settings):
    """A stored document-draft skill payload deserializes via _point_to_response."""
    point = _make_mock_document_draft_point()
    mock_vector._client.retrieve = AsyncMock(return_value=[point])
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.get("/skills/doc1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source_type"] == "document"
    assert data["content_class"] == "procedural"
    assert data["source_doc"] == "wiki-runbook"
    assert data["procedure_title"] == "Restart the widget"
    assert data["needs_rereview"] is True


def test_get_skill_session_provenance_defaults(mock_vector, mock_settings):
    """A pre-existing session-drafted skill (no provenance fields in payload)
    still deserializes — with backward-compatible defaults."""
    point = _make_mock_point()
    mock_vector._client.retrieve = AsyncMock(return_value=[point])
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.get("/skills/abc")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source_type"] == "session"
    assert data["content_class"] is None
    assert data["source_doc"] is None
    assert data["procedure_title"] is None
    assert data["needs_rereview"] is False


def _filtering_scroll(points):
    """A scroll fake that actually evaluates the Qdrant `must` FieldConditions,
    unlike the other fixtures in this file which return a canned list regardless
    of the filter. Needed so the draft-leak tests below observe the real effect
    of list_skills' status filter instead of just echoing back a fixed payload.
    """
    async def _scroll(*, scroll_filter, limit, **_kwargs):
        conditions = {c.key: c.match.value for c in (scroll_filter.must or [])}
        matched = [
            p for p in points
            if all((p.payload or {}).get(k) == v for k, v in conditions.items())
        ]
        return matched[:limit], None
    return _scroll


# ---------------------------------------------------------------------------
# Draft leak closure — SP2 Task 2 (THE load-bearing safety property).
# GET /skills with no `status` param must default to active-only.
# ---------------------------------------------------------------------------


def test_list_skills_no_status_defaults_to_active_excludes_drafts(mock_vector, mock_settings):
    """A draft (source_type=document, from Task 1's doc-ingest pipeline) must
    never surface when a caller hits GET /skills without an explicit status."""
    draft = _make_mock_document_draft_point(skill_id="doc1")
    active = _make_mock_point(skill_id="abc", status="active")
    mock_vector._client.scroll = _filtering_scroll([draft, active])
    client = TestClient(_make_app(mock_vector, mock_settings))

    resp = client.get("/skills")  # NO status param
    assert resp.status_code == 200
    data = resp.json()
    ids = {d["id"] for d in data}
    assert "doc1" not in ids
    assert ids == {"abc"}
    assert all(d["skill_status"] == "active" for d in data)


def test_list_skills_explicit_status_draft_returns_draft(mock_vector, mock_settings):
    """The human review queue (?status=draft) must still be able to see drafts."""
    draft = _make_mock_document_draft_point(skill_id="doc1")
    active = _make_mock_point(skill_id="abc", status="active")
    mock_vector._client.scroll = _filtering_scroll([draft, active])
    client = TestClient(_make_app(mock_vector, mock_settings))

    resp = client.get("/skills?status=draft")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "doc1"
    assert data[0]["skill_status"] == "draft"


def test_list_skills_empty_status_param_is_not_an_all_statuses_escape_hatch(
    mock_vector, mock_settings
):
    """`?status=` (explicit empty string) must not become a raw-call escape
    hatch that bypasses the active-only default and leaks the draft."""
    draft = _make_mock_document_draft_point(skill_id="doc1")
    active = _make_mock_point(skill_id="abc", status="active")
    mock_vector._client.scroll = _filtering_scroll([draft, active])
    client = TestClient(_make_app(mock_vector, mock_settings))

    resp = client.get("/skills?status=")
    assert resp.status_code == 200
    data = resp.json()
    ids = {d["id"] for d in data}
    assert "doc1" not in ids
    assert ids == {"abc"}


def test_skill_recall_query_excludes_draft_until_approved(mock_vector, mock_settings):
    """Mirrors what mcp_server.skill_recall actually queries (GET /skills with
    status=active): a document draft stays invisible until a human PATCHes it
    to active — the approval gate skill_recall depends on."""
    draft = _make_mock_document_draft_point(skill_id="doc1")
    mock_vector._client.scroll = _filtering_scroll([draft])
    mock_vector._client.retrieve = AsyncMock(return_value=[draft])

    async def _set_payload(*, collection_name, payload, points):
        draft.payload.update(payload)
    mock_vector._client.set_payload = _set_payload

    client = TestClient(_make_app(mock_vector, mock_settings))

    # Absent while still a draft.
    resp = client.get("/skills?status=active")
    assert resp.status_code == 200
    assert resp.json() == []

    # Human approves it via PATCH.
    resp = client.patch("/skills/doc1", json={"skill_status": "active"})
    assert resp.status_code == 200

    # Now present.
    resp = client.get("/skills?status=active")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "doc1"
    assert data[0]["skill_status"] == "active"


def test_create_skill_provenance_source_type_manual(mock_vector, mock_settings):
    """Manually-created skills (POST /skills) are tagged source_type=manual."""
    mock_vector._client.upsert = AsyncMock()
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.post("/skills", json={
        "trigger": "Fix Y", "symptoms": "Error Z", "steps": "do this", "gotchas": "",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["source_type"] == "manual"

    _, kwargs = mock_vector._client.upsert.call_args
    written_payload = kwargs["points"][0].payload
    assert written_payload["source_type"] == "manual"


def test_create_skill_defaults_to_active(mock_vector, mock_settings):
    """Backward-compatible default: no status → active (existing skill_create)."""
    mock_vector._client.upsert = AsyncMock()
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.post("/skills", json={
        "trigger": "t", "symptoms": "s", "steps": "x", "gotchas": "",
    })
    assert resp.status_code == 201
    assert resp.json()["skill_status"] == "active"


def test_create_skill_draft_status_for_review_queue(mock_vector, mock_settings):
    """A client-authored knowledge-ingest skill can be created as draft, so it
    lands in the same review queue as server-drafted skills (excluded from
    recall until a human approves it)."""
    mock_vector._client.upsert = AsyncMock()
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.post("/skills", json={
        "trigger": "t", "symptoms": "s", "steps": "x", "gotchas": "", "status": "draft",
    })
    assert resp.status_code == 201
    assert resp.json()["skill_status"] == "draft"
    written = mock_vector._client.upsert.call_args.kwargs["points"][0].payload
    assert written["skill_status"] == "draft"


def test_create_skill_rejects_invalid_status(mock_vector, mock_settings):
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.post("/skills", json={
        "trigger": "t", "symptoms": "s", "steps": "x", "status": "bogus",
    })
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Skill staleness surface — response fields + ?stale filter (Task #4)
# ---------------------------------------------------------------------------


def test_skill_response_staleness_defaults():
    resp = SkillResponse(
        id="abc", trigger="t", symptoms="s", content="c", skill_status="active",
    )
    assert resp.stale is False
    assert resp.last_recalled_at is None
    assert resp.stale_detected_at is None


def test_point_to_response_maps_staleness_fields(mock_vector, mock_settings):
    point = _make_mock_point()
    point.payload["stale"] = True
    point.payload["stale_detected_at"] = "2026-07-16T00:00:00+00:00"
    point.payload["last_recalled_at"] = "2026-01-01T00:00:00+00:00"
    mock_vector._client.retrieve = AsyncMock(return_value=[point])
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.get("/skills/abc")
    data = resp.json()
    assert data["stale"] is True
    assert data["stale_detected_at"] == "2026-07-16T00:00:00+00:00"
    assert data["last_recalled_at"] == "2026-01-01T00:00:00+00:00"


def test_list_skills_stale_filter(mock_vector, mock_settings):
    """?status=active&stale=true returns only stale-flagged active skills."""
    stale = _make_mock_point(skill_id="old", status="active")
    stale.payload["stale"] = True
    fresh = _make_mock_point(skill_id="new", status="active")
    fresh.payload["stale"] = False
    mock_vector._client.scroll = _filtering_scroll([stale, fresh])
    client = TestClient(_make_app(mock_vector, mock_settings))

    resp = client.get("/skills?status=active&stale=true")
    assert resp.status_code == 200
    ids = {d["id"] for d in resp.json()}
    assert ids == {"old"}


def test_list_skills_no_stale_filter_returns_all_active(mock_vector, mock_settings):
    stale = _make_mock_point(skill_id="old", status="active")
    stale.payload["stale"] = True
    fresh = _make_mock_point(skill_id="new", status="active")
    fresh.payload["stale"] = False
    mock_vector._client.scroll = _filtering_scroll([stale, fresh])
    client = TestClient(_make_app(mock_vector, mock_settings))

    resp = client.get("/skills?status=active")  # no stale param
    assert {d["id"] for d in resp.json()} == {"old", "new"}


def test_patch_skill_clears_stale_and_stamps_reviewed(mock_vector, mock_settings):
    point = _make_mock_point()
    point.payload["stale"] = True
    mock_vector._client.retrieve = AsyncMock(return_value=[point])
    mock_vector._client.set_payload = AsyncMock()
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.patch("/skills/abc", json={"stale": False})
    assert resp.status_code == 200
    payload = mock_vector._client.set_payload.await_args.kwargs["payload"]
    assert payload.get("stale") is False
    # the human ack stamps a reviewed marker the sweep honors as freshness, so
    # the "Still valid" click survives the next staleness cycle (durability fix)
    assert "stale_reviewed_at" in payload


def test_activating_skill_stamps_freshness_so_it_is_not_instantly_stale(mock_vector, mock_settings):
    """Docs→Skills × staleness interaction: promoting a draft to active must
    stamp a freshness marker, or a draft that aged >90d in the review queue is
    flagged STALE on the very next sweep despite the human just approving it."""
    point = _make_mock_document_draft_point(skill_id="doc1")  # old synthesis timestamp
    mock_vector._client.retrieve = AsyncMock(return_value=[point])
    mock_vector._client.set_payload = AsyncMock()
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.patch("/skills/doc1", json={"skill_status": "active"})
    assert resp.status_code == 200
    payload = mock_vector._client.set_payload.await_args.kwargs["payload"]
    assert payload["skill_status"] == "active"
    assert "stale_reviewed_at" in payload  # activation resets the staleness clock


def test_create_skill_persists_identity_headers(mock_vector, mock_settings):
    """Night Shift (client 0.1.23) attributes draft skills to the ORIGINAL
    session via X-Agent-Id / X-Session-Id — previously POST /skills hardcoded
    agent_id=None/source_session_id=None and the provenance silently vanished
    (wf_02954176 review)."""
    mock_vector._client.upsert = AsyncMock()
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.post("/skills", json={
        "trigger": "t", "symptoms": "s", "steps": "x", "gotchas": "",
    }, headers={"X-Agent-Id": "mogan", "X-Session-Id": "sess-42"})
    assert resp.status_code == 201
    _, kwargs = mock_vector._client.upsert.call_args
    payload = kwargs["points"][0].payload
    assert payload["agent_id"] == "mogan"
    assert payload["source_session_id"] == "sess-42"


def test_create_skill_without_headers_keeps_null_provenance(mock_vector, mock_settings):
    mock_vector._client.upsert = AsyncMock()
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.post("/skills", json={
        "trigger": "t", "symptoms": "s", "steps": "x", "gotchas": "",
    })
    assert resp.status_code == 201
    _, kwargs = mock_vector._client.upsert.call_args
    payload = kwargs["points"][0].payload
    assert payload["agent_id"] is None
    assert payload["source_session_id"] is None


# ---------------------------------------------------------------------------
# skill_recall replay receipt (Outcome Truth PR3, D1) — the dedicated
# skill_recall path (record_recall=true) must emit ONE `memory_read` replay
# event carrying the served skill ids, so a skill exposure can be joined to
# its session's eventual outcome. Reuses `memory_read` (no new event type),
# mirroring the streaming recall receipt in test_streaming.py.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture()
def wired_replay_emitter(monkeypatch, fake_redis):
    """Point the real replay emitter at `fake_redis` for this test only.

    Same dodge as test_streaming.py's fixture of the same name: patches
    `app.main._replay_initialized` + `replay.emitter`'s module globals directly
    so `_replay_emit` (called via the deferred `from app.main import
    _replay_emit` inside the record_recall branch) and `get_session_timeline`
    read/write the exact same fake_redis instance the endpoint's own
    access-count bumps land in.
    """
    import app.main as main_mod
    import replay.emitter as emitter_mod

    monkeypatch.setattr(main_mod, "_replay_initialized", True)
    monkeypatch.setattr(emitter_mod, "_redis", fake_redis)
    monkeypatch.setattr(emitter_mod, "_settings", ReplaySettings(ENABLED=True))
    return fake_redis


def _asgi_client(app):
    """httpx.AsyncClient over ASGITransport, not fastapi.testclient.TestClient:
    TestClient runs the app in its own thread with its own event loop, so a
    fakeredis instance touched there gets bound to that loop and becomes
    unusable from the test coroutine's own loop afterward. ASGITransport runs
    the app in-process on the caller's own loop instead."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_explicit_skill_recall_emits_memory_read_receipt(
    mock_vector, mock_settings, wired_replay_emitter, monkeypatch
):
    kept = _make_mock_point(skill_id="s1", trigger="Fix X")
    other = _make_mock_point(skill_id="s2", trigger="Fix Y")

    async def _results(*_args, **_kwargs):
        return [kept, other], False

    monkeypatch.setattr("app.skills.api.search_skill_points", _results)
    app = _make_app(mock_vector, mock_settings, wired_replay_emitter)

    async with _asgi_client(app) as client:
        resp = await client.get(
            "/skills",
            params={"record_recall": True},
            headers={"X-Session-Id": "sess-skill-1", "X-Agent-Id": "agent-a"},
        )
    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()] == ["s1", "s2"]

    timeline = await get_session_timeline(
        wired_replay_emitter, "sess-skill-1", event_type="memory_read"
    )
    events = timeline["events"]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["memory_ids"] == ["s1", "s2"]
    assert payload["result_count"] == 2
    assert payload["trigger"] == "skill_recall"
    assert "top_score" not in payload


@pytest.mark.asyncio
async def test_skill_listing_record_recall_false_emits_no_replay_event(
    mock_vector, mock_settings, wired_replay_emitter
):
    """Dashboard browsing (record_recall unset/false) must not look like an
    intentional skill_recall — no receipt, same as it records no usage stamp."""
    point = _make_mock_point()
    mock_vector._client.scroll = AsyncMock(return_value=([point], None))
    app = _make_app(mock_vector, mock_settings, wired_replay_emitter)

    async with _asgi_client(app) as client:
        resp = await client.get(
            "/skills",
            params={"status": "active"},
            headers={"X-Session-Id": "sess-skill-2", "X-Agent-Id": "agent-a"},
        )
    assert resp.status_code == 200

    timeline = await get_session_timeline(
        wired_replay_emitter, "sess-skill-2", event_type="memory_read"
    )
    assert timeline["events"] == []


@pytest.mark.asyncio
async def test_briefing_skills_section_emits_no_replay_event(
    mock_vector, mock_settings, wired_replay_emitter
):
    """The briefing's `skills_section` never calls `list_skills` (it calls
    `search_skill_points` directly and never touches `record_recall` or
    `_record_skill_usage`), so it is structurally incapable of reaching the new
    receipt branch. Proved here by exercising the real function against a
    replay stream wired to fakeredis and asserting the stream stays empty --
    not scoped to any one session_id, since skills_section has no Request/
    session context to stamp one with in the first place."""
    from app.briefing import sections as S

    point = _make_mock_point()
    mock_vector._client.scroll = AsyncMock(return_value=([point], None))

    sec = await S.skills_section(mock_vector, mock_settings, goal="", project=None)

    assert sec["status"] == "ok"
    assert await wired_replay_emitter.xlen("rp:events") == 0
