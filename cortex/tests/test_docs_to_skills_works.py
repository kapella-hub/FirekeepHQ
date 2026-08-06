"""Docs->Skills must actually produce drafts, and must not claim it did when it didn't.

TWO DEFECTS, one right and one wrong.

RIGHT: refusing a truncated completion. A card that stopped at the token cap is
half-written by construction; a live draft ended '...I think the domain should
be' and sat in the human review queue wearing the same badge as a real skill.

WRONG: the refusal disabled the feature. MEASURED 2026-08-06 on the live VPS
(ollama 0.32.4, qwen3:4b, native /api/chat, think:false, the real
`_DOC_LLM_PROMPT` over the real "Runbook: Restart stuck Celery worker" corpus
document, `SKILL_SYNTH_MAX_TOKENS=800`):

    free-form           -> done_reason="length", eval_count=800, 3512 chars,
                           143.82s, no `---` header, no `## Steps`
    free-form + /no_think -> done_reason="length", 800 tokens, 3393 chars,
                           116.63s, head "We are given a specific procedure
                           title:" — the exact string found in the live queue
    JSON SCHEMA         -> done_reason="stop", 263-317 output tokens, parsed,
                           all 8 fields, 33.69-54.54s, 5 runs out of 5

`think:false` does not stop qwen3:4b deliberating on this build — it moves the
deliberation from the `thinking` key INTO `content` (a probe returned a literal
`</think>` inside content). The budget was spent before the card began.

RAISING THE CAP CANNOT FIX IT. Generation measured ~5.6 tok/s, so
`SKILL_SYNTH_TIMEOUT_SECONDS=300` buys ~1680 tokens; a run long enough to
deliberate AND write a card hits the clock instead of the cap. A grammar removes
the deliberation instead of budgeting for it. So the cap STAYS at 800 (worst
measured run used 317) and the truncation guard stays — it just stops firing.

Plus: `GET /knowledge/sources` reported `classified/skills_queued=N` forever
regardless of whether any draft succeeded, because the fan-out tasks reported
their outcome to nobody. Live: "Runbook: Restart stuck Celery worker", queued 1,
drafted 0, unchanged since 2026-07-12.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.knowledge.api import _effective_status
from app.skills.synthesizer import (
    _CARD_SCHEMA,
    SkillSynthesizer,
    card_defect,
    card_from_payload,
    parse_skill_content,
)

_GOOD_PAYLOAD = {
    "trigger": "A Celery worker stops acknowledging tasks",
    "symptoms": "Queue depth climbs; no task activity in the worker log",
    "domain": "celery",
    "verified_on": "firekeep/2026-08",
    "whats_happening": "The solo-pool worker is wedged inside a long task.",
    "steps": ["Confirm with celery inspect active", "docker compose restart cortex-worker"],
    "gotchas": ["Restarting redis looks like it helps but drops queued tasks"],
    "example": "docker compose restart cortex-worker",
}


def _settings(**over):
    s = MagicMock()
    s.LLM_BASE_URL = "http://ollama:11434/v1"
    s.LLM_MODEL = "qwen3:4b"
    s.LLM_API_KEY = ""
    s.LLM_NATIVE_CHAT = "always"
    s.LLM_NATIVE_BASE_URL = ""
    s.SKILL_SYNTH_TIMEOUT_SECONDS = 300.0
    s.SKILL_SYNTH_MAX_TOKENS = 800
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _http(payload_json: str, *, done_reason: str = "stop"):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "message": {"content": payload_json}, "done_reason": done_reason,
    })
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resp)
    return client


class TestTheCardIsRequestedAsJson:
    def test_the_schema_names_every_field_the_card_template_needs(self):
        props = _CARD_SCHEMA["properties"]
        for field in ("trigger", "symptoms", "domain", "verified_on",
                      "whats_happening", "steps", "gotchas", "example"):
            assert field in props
            assert field in _CARD_SCHEMA["required"]

    @pytest.mark.asyncio
    async def test_the_doc_call_sends_the_schema_on_the_wire(self):
        with patch("app.llm.httpx.AsyncClient") as mc:
            mc.return_value = _http(json.dumps(_GOOD_PAYLOAD))
            synth = SkillSynthesizer(_settings())
            await synth._call_llm_doc("src", "Restart a stuck worker", "body")
            body = mc.return_value.post.await_args.kwargs["json"]
        assert body["format"] == _CARD_SCHEMA
        assert body["think"] is False
        assert body["options"]["num_predict"] == 800

    @pytest.mark.asyncio
    async def test_the_completion_is_rendered_back_into_a_parseable_card(self):
        """The STORED artifact is unchanged: header + Markdown body. The grammar
        lives on the wire, not in the store."""
        with patch("app.llm.httpx.AsyncClient") as mc:
            mc.return_value = _http(json.dumps(_GOOD_PAYLOAD))
            synth = SkillSynthesizer(_settings())
            out = await synth._call_llm_doc("src", "Restart a stuck worker", "body")

        parsed = parse_skill_content(out)
        assert parsed["trigger"] == _GOOD_PAYLOAD["trigger"]
        assert parsed["domain"] == "celery"
        assert "## Steps" in parsed["body"]
        assert card_defect(parsed) is None, "a good schema payload must yield a usable card"


class TestTheRealFailureIsNoLongerReachable:
    """The measured production output, replayed through the parser."""

    def test_the_live_deliberation_dump_is_still_rejected(self):
        """This is the head of the real 3393-char completion measured on the VPS."""
        raw = (
            "We are given a specific procedure title: \"Restart a stuck Celery worker\"\n"
            " We need to extract the procedure from the provided document.\n"
            " But the problem says \"single word\"... I think the domain should be"
        )
        assert card_defect(parse_skill_content(raw)) == "no-card-header"

    @pytest.mark.asyncio
    async def test_a_truncated_completion_still_raises(self):
        """The guard is right and stays right — it just stops firing in practice."""
        with patch("app.llm.httpx.AsyncClient") as mc:
            mc.return_value = _http(json.dumps(_GOOD_PAYLOAD)[:120],
                                    done_reason="length")
            synth = SkillSynthesizer(_settings())
            with pytest.raises(ValueError, match="truncated"):
                await synth._call_llm_doc("src", "t", "body")

    def test_the_token_cap_stays_at_800_because_the_schema_fits_inside_it(self):
        """Worst of five live runs used 317 output tokens. Raising the cap was
        the wrong lever — at ~5.6 tok/s the 300s timeout binds before 1680
        tokens, so a bigger cap buys a slower failure, not a card."""
        from app.config import Settings

        assert Settings.model_fields["SKILL_SYNTH_MAX_TOKENS"].default == 800


class TestNonSchemaBackendsStillWork:
    @pytest.mark.asyncio
    async def test_a_prose_card_passes_through_untouched(self):
        """`llm.chat` drops the schema for a backend that rejects it. Whatever
        comes back then must reach `parse_skill_content` exactly as before."""
        card = (
            "trigger: Widget wedged\nsymptoms: none\ndomain: widgets\n"
            "verified_on: t/2026\n---\n## Steps\n1. Restart it."
        )
        with patch("app.llm.httpx.AsyncClient") as mc:
            mc.return_value = _http(card)
            synth = SkillSynthesizer(_settings())
            out = await synth._call_llm_doc("src", "t", "body")
        assert out == card

    def test_a_json_object_that_is_not_a_card_is_not_mangled(self):
        from app.skills.synthesizer import _render_card

        assert _render_card('{"unrelated": 1}') == '{"unrelated": 1}'
        assert _render_card("not json at all") == "not json at all"
        assert _render_card("[1, 2, 3]") == "[1, 2, 3]"


class TestCardRendering:
    def test_steps_are_numbered_and_gotchas_bulleted(self):
        card = card_from_payload(_GOOD_PAYLOAD)
        assert "1. Confirm with celery inspect active" in card["body"]
        assert "- Restarting redis looks like it helps" in card["body"]

    def test_a_missing_section_is_omitted_not_rendered_empty(self):
        card = card_from_payload({"trigger": "t", "steps": ["a"]})
        assert "## Steps" in card["body"]
        assert "## Gotchas" not in card["body"]
        assert "## Example" not in card["body"]

    def test_a_string_where_an_array_was_promised_is_tolerated(self):
        card = card_from_payload({**_GOOD_PAYLOAD, "steps": "one\ntwo"})
        assert "1. one" in card["body"] and "2. two" in card["body"]


class TestSourceStatusIsHonest:
    """`GET /knowledge/sources` must not report an intention as a result."""

    def test_all_drafts_failed_is_reported_as_drafts_failed(self):
        rec = {"status": "classified", "disposition": "procedural"}
        assert _effective_status(rec, drafted=0, failed=1, queued=1, draft_points=0) == "drafts_failed"

    def test_a_source_with_a_real_draft_is_never_relabelled(self):
        rec = {"status": "classified"}
        assert _effective_status(rec, drafted=0, failed=1, queued=1, draft_points=3) == "classified"
        assert _effective_status(rec, drafted=1, failed=1, queued=2, draft_points=1) == "classified"

    def test_an_in_flight_ingest_is_not_reported_as_broken(self):
        """Nothing has failed yet — reporting failure would be as wrong as
        reporting success."""
        rec = {"status": "classified"}
        assert _effective_status(rec, drafted=0, failed=0, queued=2, draft_points=0) == "classified"

    def test_non_classified_statuses_pass_through(self):
        for stored in ("queued", "classifying", "failed", "corpus_only", "unknown"):
            rec = {"status": stored}
            assert _effective_status(rec, 0, 1, 1, 0) == stored


    @pytest.mark.asyncio
    async def test_the_draft_worker_reports_its_outcome(self):
        """Without this the fan-out tasks are invisible and `classified` is
        the only thing the endpoint ever sees."""
        import app.workers.skill_synthesis as ws

        recorded = []

        async def _fake_record(source_name, *, ok, redis_client, note=""):
            recorded.append((source_name, ok, note))

        fake_redis = AsyncMock()
        with patch.object(ws, "record_draft_outcome", new=_fake_record), \
             patch.object(ws.redis.asyncio, "from_url", return_value=fake_redis), \
             patch.object(ws, "SkillSynthesizer") as sc:
            inst = sc.return_value
            inst.synthesize_from_document = AsyncMock(
                return_value={"status": "empty", "defect": "no-steps"}
            )
            await ws._run_doc_synthesis("Runbook", "Restart", "doc", None, "default")

            inst.synthesize_from_document = AsyncMock(
                return_value={"status": "drafted", "id": "x"}
            )
            await ws._run_doc_synthesis("Runbook", "Restart", "doc", None, "default")

        assert recorded[0] == ("Runbook", False, "no-steps")
        assert recorded[1][:2] == ("Runbook", True)

    @pytest.mark.asyncio
    async def test_a_bookkeeping_failure_never_fails_a_stored_draft(self):
        import app.workers.skill_synthesis as ws

        with patch.object(ws.redis.asyncio, "from_url", side_effect=RuntimeError("no redis")), \
             patch.object(ws, "SkillSynthesizer") as sc:
            sc.return_value.synthesize_from_document = AsyncMock(
                return_value={"status": "drafted", "id": "x"}
            )
            out = await ws._run_doc_synthesis("Runbook", "Restart", "doc", None, "default")
        assert out["status"] == "drafted"


class TestStaleQueuedSourcesAreNotReportedAsSuccess:
    """The record that PROVED the bug must not still read as success.

    `record_draft_outcome` did not exist when the live "Runbook: Restart stuck
    Celery worker" record was written, so its `skills_failed` is unset. It can
    therefore never satisfy `failed > 0` and, with only the `drafts_failed`
    branch, resolved to `classified` — queued 1, drafted 0, no draft point,
    unchanged since 2026-07-12 and verified still reading `classified` 25 days
    later. Counting outcomes fixed the FORWARD path and left the evidence of
    the bug wearing a success badge.
    """

    _STALE = "2026-07-12T10:00:00+00:00"
    _NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)

    def test_the_live_pre_counter_record_is_reported_as_missing(self):
        rec = {"status": "classified", "disposition": "procedural", "updated_at": self._STALE}
        assert _effective_status(
            rec, drafted=0, failed=0, queued=1, draft_points=0, now=self._NOW
        ) == "drafts_missing"

    def test_absence_is_never_reported_as_observed_failure(self):
        """`drafts_failed` asserts a draft REPORTED failure. Nothing did here,
        so claiming it would be the same overreach in the other direction."""
        rec = {"status": "classified", "updated_at": self._STALE}
        assert _effective_status(
            rec, drafted=0, failed=0, queued=1, draft_points=0, now=self._NOW
        ) != "drafts_failed"

    def test_observed_failure_still_wins_over_staleness(self):
        rec = {"status": "classified", "updated_at": self._STALE}
        assert _effective_status(
            rec, drafted=0, failed=2, queued=2, draft_points=0, now=self._NOW
        ) == "drafts_failed"

    def test_a_recent_record_is_still_treated_as_in_flight(self):
        """The grace window is the whole safety margin — a slow ingest must not
        be called missing."""
        rec = {
            "status": "classified",
            "updated_at": (self._NOW - timedelta(minutes=90)).isoformat(),
        }
        assert _effective_status(
            rec, drafted=0, failed=0, queued=3, draft_points=0, now=self._NOW
        ) == "classified"

    def test_a_stale_record_that_did_produce_drafts_is_never_relabelled(self):
        rec = {"status": "classified", "updated_at": self._STALE}
        for drafted, points in ((1, 0), (0, 4), (2, 2)):
            assert _effective_status(
                rec, drafted=drafted, failed=0, queued=4,
                draft_points=points, now=self._NOW,
            ) == "classified"

    @pytest.mark.parametrize("stamp", ["", "not-a-date", "2026-13-45T99:99:99", None, 12345])
    def test_an_unreadable_updated_at_counts_as_in_flight(self, stamp):
        """Failing to read the clock is not evidence the drafts are missing,
        and a listing endpoint must never raise over a malformed field."""
        rec = {"status": "classified", "updated_at": stamp}
        assert _effective_status(
            rec, drafted=0, failed=0, queued=1, draft_points=0, now=self._NOW
        ) == "classified"

    def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing(self):
        """Records written by older code carry no offset; comparing naive to
        aware raises, and the raise would surface as a 500 on the listing."""
        rec = {"status": "classified", "updated_at": "2026-07-12T10:00:00"}
        assert _effective_status(
            rec, drafted=0, failed=0, queued=1, draft_points=0, now=self._NOW
        ) == "drafts_missing"

    def test_the_grace_window_floors_at_24h_and_scales_with_config(self):
        """Derived, not hardcoded: a deploy that raises the per-draft budget
        must not start calling slow-but-healthy ingests missing."""
        from app.knowledge.api import _draft_grace_seconds

        class _S:
            KNOWLEDGE_MAX_PROCEDURES = 10
            SKILL_SYNTH_TIMEOUT_SECONDS = 300.0

        assert _draft_grace_seconds(_S()) == 86_400.0  # 10x300x24 = 72000 < floor
        _S.SKILL_SYNTH_TIMEOUT_SECONDS = 1800.0        # 10x1800x24 = 432000 > floor
        assert _draft_grace_seconds(_S()) == 432_000.0

    def test_the_grace_window_survives_an_unreadable_settings_object(self):
        from app.knowledge.api import _draft_grace_seconds

        class _Broken:
            @property
            def KNOWLEDGE_MAX_PROCEDURES(self):
                raise RuntimeError("boom")

        assert _draft_grace_seconds(_Broken()) == 86_400.0
