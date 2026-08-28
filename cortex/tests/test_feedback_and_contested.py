"""Knowledge Autopilot round 1: feedback-weighted recall + contested memories.

Three properties under guard:

1. Feedback ACCUMULATES. The original set_feedback wrote three flat fields, so
   a second thumb overwrote the first — the signal existed but was
   structurally unusable, and nothing consumed it. Counters + the Beta-shrunk
   recall multiplier are what make the dashboard's thumbs (and the
   memory_feedback MCP tool) actually change ranking.
2. Neutral-by-construction. Zero feedback must rank bit-identically to
   pre-feedback — the same contract OWM shipped under, and the reason turning
   the feature on is safe.
3. Contested is visible, not decisive. A contested memory keeps full score and
   gains an annotation; resolution is an accountable verdict through
   /memory/contested/resolve, never a silent pick.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.engine.rag import RAGEngine
from app.lifecycle import create_lifecycle_router
from app.owm import compute_efficacy


@pytest.fixture()
def mock_graph() -> AsyncMock:
    g = AsyncMock()
    g.query_related = AsyncMock(return_value=[])
    g.get_supersession_history = AsyncMock(
        return_value={"supersedes": [], "superseded_by": None}
    )
    return g


@pytest.fixture()
def mock_vector() -> AsyncMock:
    v = AsyncMock()
    v.search = AsyncMock(return_value=[])
    v.update_status = AsyncMock()
    v.confirm_memory = AsyncMock(return_value=True)
    v.get_memory = AsyncMock(return_value=None)
    v._client = AsyncMock()
    return v


@pytest.fixture()
def engine(mock_graph, mock_vector) -> RAGEngine:
    return RAGEngine(graph=mock_graph, vector=mock_vector)


@pytest.fixture()
def lifecycle_client(mock_graph, mock_vector) -> TestClient:
    app = FastAPI()
    app.include_router(create_lifecycle_router(graph=mock_graph, vector=mock_vector))
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# set_feedback accumulates counters
# ---------------------------------------------------------------------------


class TestSetFeedbackCounters:
    @pytest.mark.asyncio
    async def test_two_thumbs_accumulate_instead_of_overwriting(self):
        from app.db.vector import VectorClient

        vc = VectorClient.__new__(VectorClient)
        inner = AsyncMock()
        vc._client = inner
        vc._collection = "firekeep_memories"

        point = MagicMock()
        point.payload = {"feedback_useful_count": 2, "feedback_not_useful_count": 1}
        inner.retrieve = AsyncMock(return_value=[point])

        await vc.set_feedback("mem-1", useful=True, comment=None,
                              timestamp="2026-08-09T00:00:00+00:00")

        written = inner.set_payload.call_args.kwargs["payload"]
        assert written["feedback_useful_count"] == 3
        assert written["feedback_not_useful_count"] == 1
        assert written["feedback_last_at"] == "2026-08-09T00:00:00+00:00"
        # The old last-write-wins fields must not come back.
        assert "feedback_useful" not in written

    @pytest.mark.asyncio
    async def test_missing_point_raises_rather_than_minting_counters(self):
        from app.db.vector import VectorClient, VectorStoreError

        vc = VectorClient.__new__(VectorClient)
        inner = AsyncMock()
        inner.retrieve = AsyncMock(return_value=[])
        vc._client = inner
        vc._collection = "firekeep_memories"

        with pytest.raises(VectorStoreError):
            await vc.set_feedback("ghost", useful=True, comment=None, timestamp="t")
        inner.set_payload.assert_not_called()

    @pytest.mark.asyncio
    async def test_comment_is_bounded(self):
        from app.db.vector import VectorClient

        vc = VectorClient.__new__(VectorClient)
        inner = AsyncMock()
        point = MagicMock()
        point.payload = {}
        inner.retrieve = AsyncMock(return_value=[point])
        vc._client = inner
        vc._collection = "firekeep_memories"

        await vc.set_feedback("mem-1", useful=False, comment="x" * 2000,
                              timestamp="t")
        written = inner.set_payload.call_args.kwargs["payload"]
        assert len(written["feedback_last_comment"]) == 500
        assert written["feedback_not_useful_count"] == 1


# ---------------------------------------------------------------------------
# Feedback multiplier in scoring
# ---------------------------------------------------------------------------


class TestFeedbackScoring:
    def test_zero_feedback_is_bit_identical(self, engine):
        items = [{"score": 1.0, "content": "t", "metadata": {"status": "active"}}]
        assert engine._apply_lifecycle_scoring(items)[0]["score"] == 1.0

    def test_useful_feedback_boosts_within_clamp(self, engine):
        items = [{"score": 1.0, "content": "t", "metadata": {
            "status": "active", "feedback_useful_count": 10,
            "feedback_not_useful_count": 0,
        }}]
        score = engine._apply_lifecycle_scoring(items)[0]["score"]
        w = engine._settings.FEEDBACK_WEIGHT
        eff = compute_efficacy(10, 10, engine._settings.FEEDBACK_PRIOR_N)
        assert score == pytest.approx(1.0 + w * 2.0 * (eff - 0.5))
        assert score <= 1.0 + w  # clamp holds

    def test_not_useful_feedback_sinks_within_clamp(self, engine):
        items = [{"score": 1.0, "content": "t", "metadata": {
            "status": "active", "feedback_useful_count": 0,
            "feedback_not_useful_count": 10,
        }}]
        score = engine._apply_lifecycle_scoring(items)[0]["score"]
        assert score < 1.0
        assert score >= 1.0 - engine._settings.FEEDBACK_WEIGHT

    def test_one_thumb_nudges_never_yanks(self, engine):
        """compute_efficacy(1,1,prior=4) is 0.6 — a single enthusiastic thumb
        moves the score ~2%, not to the clamp edge. That shrinkage is the
        entire reason exposing feedback to agents is safe."""
        items = [{"score": 1.0, "content": "t", "metadata": {
            "status": "active", "feedback_useful_count": 1,
            "feedback_not_useful_count": 0,
        }}]
        score = engine._apply_lifecycle_scoring(items)[0]["score"]
        assert 1.0 < score < 1.0 + engine._settings.FEEDBACK_WEIGHT / 2

    def test_disabled_setting_is_inert(self, engine):
        engine._settings = engine._settings.model_copy(
            update={"FEEDBACK_ENABLED": False}
        )
        items = [{"score": 1.0, "content": "t", "metadata": {
            "status": "active", "feedback_useful_count": 50,
            "feedback_not_useful_count": 0,
        }}]
        assert engine._apply_lifecycle_scoring(items)[0]["score"] == 1.0


# ---------------------------------------------------------------------------
# Contested: annotation, not judgment
# ---------------------------------------------------------------------------


class TestContestedVisibility:
    def test_contested_keeps_full_score_and_gains_annotation(self, engine):
        items = [{"score": 1.0, "content": "t", "metadata": {
            "status": "active", "contested": True, "contested_with": "mem-9",
        }}]
        out = engine._apply_lifecycle_scoring(items)[0]
        assert out["score"] == 1.0  # no penalty in round 1 — visibility only
        assert out["_contested_with"] == "mem-9"

    def test_markdown_names_the_disputing_memory(self):
        entries = [{
            "score": 0.9, "content": "Use MySQL", "store": "vector",
            "metadata": {"raw_score": 0.9},
            "_contested_with": "mem-9",
        }]
        text = RAGEngine._format_markdown(entries, task="db", top_k=1)
        assert "[CONTESTED by mem-9]" in text


# ---------------------------------------------------------------------------
# /memory/contested/resolve
# ---------------------------------------------------------------------------


def _real_shape(mid: str, **payload_fields) -> dict:
    """The EXACT shape VectorClient.get_memory returns: a handful of hoisted
    lifecycle fields, everything else folded under "metadata". The first
    version of these tests used flat dicts the real method never produces,
    and the endpoint shipped reading contested_with at the top level — 409 on
    every genuinely contested pair. Mock the real contract or mock nothing."""
    hoisted = ("status", "confirmed_count", "contradicted_count",
               "last_confirmed_at", "superseded_by")
    return {
        "id": mid,
        "text": payload_fields.pop("text", "some memory"),
        **{k: payload_fields.pop(k) for k in hoisted if k in payload_fields},
        "metadata": payload_fields,
    }


def _contested_pair(mock_vector):
    async def _get(mid):
        return {
            "w1": _real_shape("w1", status="active", contested=True,
                              contested_with="l1"),
            "l1": _real_shape("l1", status="active", contested=True,
                              contested_with="w1"),
        }.get(mid)

    mock_vector.get_memory = AsyncMock(side_effect=_get)


class TestContestedResolve:
    def test_supersede_verdict(self, lifecycle_client, mock_graph, mock_vector):
        _contested_pair(mock_vector)
        resp = lifecycle_client.post(
            "/memory/contested/resolve",
            json={"winner_id": "w1", "loser_id": "l1", "action": "supersede"},
        )
        assert resp.status_code == 200, resp.text
        # Loser superseded WITH the contradiction counted; winner confirmed —
        # the verdict itself is human evidence.
        mock_vector.update_status.assert_awaited_once()
        assert mock_vector.update_status.call_args.kwargs.get("superseded_by") == "w1" or \
            mock_vector.update_status.call_args.args[:2] == ("l1", "superseded")
        mock_vector.confirm_memory.assert_awaited_once_with("w1")
        # The verdict records the same SUPERSEDES edge every sibling supersede
        # path records — without it the evidence endpoint's lineage is blind
        # to explicit human supersessions.
        mock_graph.create_supersession.assert_awaited_once()
        edge = mock_graph.create_supersession.call_args.kwargs
        assert edge["newer_id"] == "w1" and edge["older_id"] == "l1"
        # Flags cleared on BOTH, after the verdict.
        clear_call = mock_vector._client.set_payload.call_args
        assert clear_call.kwargs["payload"]["contested"] is False
        assert set(clear_call.kwargs["points"]) == {"w1", "l1"}

    def test_supersede_failure_leaves_dispute_recorded(
        self, lifecycle_client, mock_vector
    ):
        """Verdict FIRST, flags cleared LAST. If the supersede write dies the
        request must 500 with the contested flags untouched, so the pair stays
        in the inbox and the human retries — the original ordering cleared the
        flags first, and a transient Qdrant error silently erased the dispute
        with no verdict applied."""
        _contested_pair(mock_vector)
        mock_vector.update_status = AsyncMock(side_effect=RuntimeError("qdrant down"))
        resp = lifecycle_client.post(
            "/memory/contested/resolve",
            json={"winner_id": "w1", "loser_id": "l1", "action": "supersede"},
        )
        assert resp.status_code == 500
        mock_vector._client.set_payload.assert_not_called()

    def test_coexist_verdict_writes_durable_marker(
        self, lifecycle_client, mock_vector
    ):
        """Coexist clears the dispute AND stamps coexist_with on each side.
        Without the marker the nightly pass re-contests the identical pair —
        unchanged texts still sit in the similarity band — and the human's
        verdict is undone within 24 hours."""
        _contested_pair(mock_vector)
        resp = lifecycle_client.post(
            "/memory/contested/resolve",
            json={"winner_id": "w1", "loser_id": "l1", "action": "coexist"},
        )
        assert resp.status_code == 200
        mock_vector.update_status.assert_not_awaited()
        mock_vector.confirm_memory.assert_not_awaited()
        writes = {
            call.kwargs["points"][0]: call.kwargs["payload"]
            for call in mock_vector._client.set_payload.call_args_list
        }
        assert writes["w1"]["coexist_with"] == "l1"
        assert writes["l1"]["coexist_with"] == "w1"
        assert writes["w1"]["contested"] is False
        assert writes["l1"]["contested"] is False

    def test_unrelated_pair_is_409(self, lifecycle_client, mock_vector):
        async def _get(mid):
            return _real_shape(mid, status="active")  # exists, not contested

        mock_vector.get_memory = AsyncMock(side_effect=_get)
        resp = lifecycle_client.post(
            "/memory/contested/resolve",
            json={"winner_id": "a", "loser_id": "b"},
        )
        assert resp.status_code == 409

    def test_missing_memory_is_404(self, lifecycle_client, mock_vector):
        mock_vector.get_memory = AsyncMock(return_value=None)
        resp = lifecycle_client.post(
            "/memory/contested/resolve",
            json={"winner_id": "a", "loser_id": "b"},
        )
        assert resp.status_code == 404

    def test_503_when_frozen(self, mock_graph, mock_vector):
        """MIGRATION_FREEZE gate (identity-v2 D6): a resolve verdict mutates
        both memories, so it must refuse during the freeze window."""
        from app.config import Settings, get_settings

        app = FastAPI()
        app.include_router(create_lifecycle_router(graph=mock_graph, vector=mock_vector))
        app.dependency_overrides[get_settings] = lambda: Settings(MIGRATION_FREEZE=True)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/memory/contested/resolve",
            json={"winner_id": "w1", "loser_id": "l1", "action": "supersede"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == "memory store migration in progress; retry shortly"


# ---------------------------------------------------------------------------
# /memory/{id}/evidence
# ---------------------------------------------------------------------------


@pytest.fixture()
def admin_lifecycle_client(mock_graph, mock_vector, monkeypatch) -> TestClient:
    """Client with the admin gate stubbed out — the evidence endpoint requires
    admin scope, and in unit tests AUTH_ENABLED=false refuses admin outright
    (audit blocker 7). The gate itself is asserted separately below."""
    import app.lifecycle as lifecycle_mod

    monkeypatch.setattr(
        lifecycle_mod, "require_scope",
        lambda scope: (lambda: {"agent_id": "test-admin"}),
    )
    app = FastAPI()
    app.include_router(create_lifecycle_router(graph=mock_graph, vector=mock_vector))
    return TestClient(app, raise_server_exceptions=False)


class TestEvidenceLedger:
    def test_composes_every_signal_class(self, admin_lifecycle_client, mock_vector):
        # Real get_memory shape: scoring signals live under "metadata", and the
        # endpoint must read them there. The flat-dict version of this test
        # passed while the live endpoint returned all-neutral evidence.
        mock_vector.get_memory = AsyncMock(return_value=_real_shape(
            "mem-1", status="active", confirmed_count=1,
            source="dream", agent_id="a1", dreamed_from=["m1", "m2"],
            access_count=7, last_recalled_at="2026-08-01T00:00:00+00:00",
            feedback_useful_count=3, feedback_not_useful_count=1,
            owm_efficacy=0.62, owm_n=9, contested=True, contested_with="m9",
        ))
        resp = admin_lifecycle_client.get("/memory/mem-1/evidence")
        assert resp.status_code == 200
        body = resp.json()
        assert body["provenance"]["dreamed_from"] == ["m1", "m2"]
        assert body["provenance"]["source"] == "dream"
        assert body["usage"]["access_count"] == 7
        assert body["judgments"]["feedback_useful_count"] == 3
        assert body["judgments"]["confirmed_count"] == 1
        assert body["outcomes"]["owm_efficacy"] == 0.62
        assert body["disputes"]["contested"] is True
        assert body["disputes"]["contested_with"] == "m9"

    def test_missing_memory_is_404(self, admin_lifecycle_client, mock_vector):
        mock_vector.get_memory = AsyncMock(return_value=None)
        assert admin_lifecycle_client.get("/memory/none/evidence").status_code == 404

    def test_requires_admin_scope(self, lifecycle_client, mock_vector):
        """Without the stub the real require_scope('admin') gate runs, and in
        the unit-test environment (AUTH_ENABLED=false) admin is refused — the
        endpoint exposes feedback comment text and member/agent provenance,
        so an ungated 200 here is a security regression, not a test artifact."""
        mock_vector.get_memory = AsyncMock(return_value=_real_shape(
            "mem-1", status="active",
        ))
        resp = lifecycle_client.get("/memory/mem-1/evidence")
        assert resp.status_code in (401, 403)
