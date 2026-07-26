import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.models import SkillResponse
from app.skills.api import create_skills_router


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


def _make_app(mock_vector, mock_settings):
    app = FastAPI()
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
