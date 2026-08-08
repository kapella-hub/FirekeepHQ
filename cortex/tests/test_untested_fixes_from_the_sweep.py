"""Guards for fix-round changes that shipped with no test of their own.

Each of these altered live behaviour and was flagged by review as unpinned:

  * route-level principal stamping on POST /skills, POST /corpus/ingest and
    POST /knowledge/ingest — a payload written with `workspace_id=null` is
    filtered out of every recall path (`VectorClient.search` applies it as a
    hard `must`), so the content exists, lists in the dashboard, and matches
    nothing;
  * `deployment_workspace_id()` stamping in the two paths that have no HTTP
    principal (the Confluence collector and the URL-ingest worker);
  * `replay_narrow`'s three distinct messages;
  * compose drift on the new SKILL_MATCH_* defaults, following the
    `test_decision_config.py` precedent — which caught a REAL one here: compose
    still supplied `SKILL_MATCH_EMBED_TIMEOUT_SECONDS:-1.2`, so the raise to
    10.0 would have been a no-op on every compose deployment.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings

_REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Principal / workspace stamping
# ---------------------------------------------------------------------------


class TestPrincipalStamping:
    def test_skills_post_stamps_the_verified_principal(self):
        import app.skills.api as api

        src = inspect.getsource(api.create_skills_router)
        assert 'payload["workspace_id"] = principal["workspace_id"]' in src
        assert 'payload["member_id"] = principal["member_id"]' in src
        assert "request_principal(request)" in src

    def test_corpus_ingest_threads_the_principal_into_the_pipeline(self):
        import corpus.api as api

        src = inspect.getsource(api.create_corpus_router)
        assert "request_principal(request)" in src
        assert 'workspace_id=principal["workspace_id"]' in src
        assert 'member_id=principal["member_id"]' in src

    def test_knowledge_ingest_core_forwards_tenancy_to_both_halves(self):
        """Corpus chunks AND the fanned-out draft-skill tasks. A null on either
        half is content that exists and cannot be found."""
        from app.knowledge.ingest_core import ingest_knowledge_document

        params = inspect.signature(ingest_knowledge_document).parameters
        assert "workspace_id" in params and "member_id" in params

        src = inspect.getsource(ingest_knowledge_document)
        # once into corpus_ingest_document, once into the Celery kwargs
        assert src.count("workspace_id=workspace_id") == 2
        assert src.count("member_id=member_id") == 2

    @pytest.mark.asyncio
    async def test_ingest_core_actually_passes_them_through(self):
        """Behavioural, not textual: both downstream calls receive the values."""
        import app.knowledge.ingest_core as core

        seen: dict = {}

        async def _fake_corpus(**kw):
            seen["corpus"] = kw

        async def _fake_status(*a, **kw):
            return None

        delay = MagicMock()
        with patch.object(core, "corpus_ingest_document", new=_fake_corpus), \
             patch.object(core, "set_ingest_status", new=_fake_status), \
             patch.object(core.classify_and_draft_from_doc, "delay", delay):
            await core.ingest_knowledge_document(
                "body", "src", "text", vector=None, redis=None,
                workspace_id="ws-9", member_id="member-9",
            )

        assert seen["corpus"]["workspace_id"] == "ws-9"
        assert seen["corpus"]["member_id"] == "member-9"
        assert delay.call_args.kwargs["workspace_id"] == "ws-9"
        assert delay.call_args.kwargs["member_id"] == "member-9"

    def test_the_doc_synthesizer_only_emits_tenancy_when_it_knows_it(self):
        """Emitting `workspace_id=None` would let a re-draft overwrite a
        migration backfill with a null."""
        from app.skills.synthesizer import SkillSynthesizer

        src = inspect.getsource(SkillSynthesizer.synthesize_from_document)
        assert "if workspace_id:" in src
        assert "if member_id:" in src


class TestNoHttpPrincipalPathsStampTheDeployment:
    def test_the_collector_stamps_the_deployment_workspace(self):
        import app.collectors.engine as engine

        src = inspect.getsource(engine.CollectorEngine.run)
        assert "workspace_id=deployment_workspace_id()" in src
        assert "member_id=deployment_owner_member_id()" in src

    def test_the_url_ingest_worker_stamps_it_too(self):
        import app.workers.skill_synthesis as ws

        src = inspect.getsource(ws._run_url_ingest_impl)
        assert "deployment_workspace_id()" in src
        assert "workspace_id=workspace_id" in src

    def test_the_helpers_exist_and_return_a_stable_value(self):
        from auth.principal import deployment_owner_member_id, deployment_workspace_id

        assert deployment_workspace_id() == deployment_workspace_id()
        assert isinstance(deployment_workspace_id(), str)
        assert deployment_workspace_id()
        assert isinstance(deployment_owner_member_id(), str)


# ---------------------------------------------------------------------------
# replay_narrow's three messages
# ---------------------------------------------------------------------------


def _narrow_payload(**over):
    base = {
        "failure_event_id": "deadbeefdeadbeefdeadbeefdeadbeef",
        "suspects": [],
        "total_events_walked": 0,
        "failure_event_found": True,
        "session_has_trace_links": True,
    }
    base.update(over)
    return base


async def _call_narrow(payload):
    from app import mcp_server

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.post = AsyncMock(return_value=resp)
    with patch.object(mcp_server, "_get_client", new=AsyncMock(return_value=client)):
        return await mcp_server.replay_narrow("sess-1", payload["failure_event_id"])


class TestReplayNarrowSaysWhichOfThreeThingsHappened:
    @pytest.mark.asyncio
    async def test_an_unknown_event_id_is_named_as_unknown(self):
        """A fabricated id used to get the same confident sentence as a real
        one, with the fake id echoed back."""
        out = await _call_narrow(_narrow_payload(failure_event_found=False))
        assert "No event" in out
        assert "unknown event, not a failure without a cause" in out

    @pytest.mark.asyncio
    async def test_a_session_with_no_trace_links_says_so(self):
        """Live census of the 3,000 most recent events: trace_links populated
        zero times. The tool was reporting 'nothing caused it' about a feature
        with no data."""
        out = await _call_narrow(_narrow_payload(session_has_trace_links=False))
        assert "NO trace links" in out
        assert "missing instrumentation" in out
        assert "replay_timeline" in out

    @pytest.mark.asyncio
    async def test_a_genuine_empty_walk_is_still_reported_as_one(self):
        out = await _call_narrow(_narrow_payload())
        assert "No suspects found" in out
        assert "none lead back to it" in out

    @pytest.mark.asyncio
    async def test_the_three_messages_are_distinct(self):
        a = await _call_narrow(_narrow_payload(failure_event_found=False))
        b = await _call_narrow(_narrow_payload(session_has_trace_links=False))
        c = await _call_narrow(_narrow_payload())
        assert len({a, b, c}) == 3

    def test_the_response_model_defaults_keep_old_callers_working(self):
        from replay.models import NarrowingResponse

        m = NarrowingResponse(
            failure_event_id="x", suspects=[], total_events_walked=0
        )
        assert m.failure_event_found is True
        assert m.session_has_trace_links is False

    @pytest.mark.asyncio
    async def test_the_trace_link_census_is_fail_soft(self):
        """An unreadable timeline must make the message MORE cautious, not
        raise inside a debugging tool."""
        import replay.narrowing as narrowing

        with patch.object(
            narrowing, "get_session_timeline",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ):
            assert await narrowing._session_has_trace_links(None, "s") is False


# ---------------------------------------------------------------------------
# SKILL_MATCH_* config drift  (test_decision_config.py precedent)
# ---------------------------------------------------------------------------


class TestSkillMatchConfigDrift:
    def test_code_defaults(self):
        s = Settings()
        assert s.SKILL_MATCH_SCORE_FLOOR == 0.30
        # The endpoint budget. 1.2 was the briefing's number applied to a caller
        # with no per-section cap; cold `_embed` measured 0.62/4.63/8.16s live,
        # so it disabled `skill_recall` rather than bounding it.
        assert s.SKILL_MATCH_EMBED_TIMEOUT_SECONDS == 10.0
        # The briefing's own, tighter budget — `_run_section` caps at 2.0s.
        assert s.SKILL_MATCH_BRIEFING_EMBED_TIMEOUT_SECONDS == 1.2

    def test_compose_defaults_do_not_override_the_code_defaults(self):
        """Compose supplies its fallback when `.env` does not set the variable,
        so a stale `${VAR:-1.2}` silently wins and Settings never sees its own
        default. This caught a real one: compose still said 1.2 for the endpoint
        budget after the code moved to 10.0, which would have shipped the whole
        `skill_recall` fix as a no-op."""
        text = (_REPO / "docker-compose.yml").read_text(encoding="utf-8")
        s = Settings()
        for var, expected in (
            ("SKILL_MATCH_SCORE_FLOOR", s.SKILL_MATCH_SCORE_FLOOR),
            ("SKILL_MATCH_EMBED_TIMEOUT_SECONDS", s.SKILL_MATCH_EMBED_TIMEOUT_SECONDS),
            ("SKILL_MATCH_BRIEFING_EMBED_TIMEOUT_SECONDS",
             s.SKILL_MATCH_BRIEFING_EMBED_TIMEOUT_SECONDS),
        ):
            found = re.findall(
                rf"{var}:\s*\$\{{{var}:-([0-9.]+)\}}", text
            )
            assert found, f"docker-compose.yml no longer sets {var}"
            assert {float(v) for v in found} == {expected}, (
                f"{var}: compose says {set(found)}, code default is {expected}"
            )

    def test_both_cortex_services_carry_the_same_defaults(self):
        """cortex-api serves GET /skills, cortex-mcp serves skill_recall — a
        value set on one and not the other is a split brain."""
        text = (_REPO / "docker-compose.yml").read_text(encoding="utf-8")
        for var in ("SKILL_MATCH_EMBED_TIMEOUT_SECONDS",
                    "SKILL_MATCH_BRIEFING_EMBED_TIMEOUT_SECONDS"):
            assert len(re.findall(rf"^\s+{var}:", text, re.MULTILINE)) == 2

    def test_the_module_fallback_matches_the_settings_default(self):
        """`DEFAULT_EMBED_TIMEOUT` is what a MagicMock-settings caller gets."""
        from app.skills.search import DEFAULT_EMBED_TIMEOUT, DEFAULT_SCORE_FLOOR

        assert DEFAULT_EMBED_TIMEOUT == Settings().SKILL_MATCH_EMBED_TIMEOUT_SECONDS
        assert DEFAULT_SCORE_FLOOR == Settings().SKILL_MATCH_SCORE_FLOOR

    def test_the_briefing_passes_its_own_budget_not_the_endpoint_one(self):
        import app.briefing.sections as sections

        src = inspect.getsource(sections.skills_section)
        assert "SKILL_MATCH_BRIEFING_EMBED_TIMEOUT_SECONDS" in src
        assert "embed_timeout=" in src
