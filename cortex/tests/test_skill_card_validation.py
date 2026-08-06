"""A synthesis that did not produce a CARD must not be stored as a skill.

WHY THESE EXIST. Both drafts produced from one clean two-procedure runbook on
the live deployment were unusable, and every status surface reported success —
the worker logged `{status: drafted}`, `/knowledge/sources` showed
`classified / procedural / skills_queued=2`, and both sat in the human review
queue looking legitimate:

  * Draft 1 — trigger `"Synthesized skill"` (the PARSER's fallback), symptoms
    `""`, domain `""`, body the model's raw deliberation: "We are given a
    specific procedure title... But the problem says \\"single word\\"... I
    think the domain should be" — cut off mid-sentence at the token cap.
  * Draft 2 — trigger `"<one sentence — what situation activates this skill>"`,
    symptoms `"<observable signals: error messages, failing patterns>"`, domain
    `"<single word: e.g. neo4j, docker, qdrant, python, api-auth>"`: the prompt
    template echoed back verbatim and stored as field values.

The guard that was supposed to prevent this required trigger AND body to be
blank. Neither failure produces a blank trigger — `parse_skill_content`
substitutes "Synthesized skill" when no `---` header parsed, and an echoed
placeholder is a perfectly truthy string — so both sailed straight through the
code whose own comment reads "A failed/empty synthesis must not become a
placeholder draft — that just pollutes the review queue."
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm import ChatResult
from app.skills.synthesizer import FALLBACK_TRIGGER, card_defect


def _card(**over):
    base = {
        "trigger": "the widget queue wedges",
        "symptoms": "backlog grows without draining",
        "domain": "ops",
        "verified_on": "firekeep/2026-08",
        "body": "## What's happening\nx\n\n## Steps\n1. drain it\n",
    }
    base.update(over)
    return base


class TestCardDefect:
    def test_a_real_card_is_accepted(self):
        assert card_defect(_card()) is None

    def test_parser_fallback_trigger_is_refused(self):
        """Draft 1: the model returned prose with no `---` header, so the
        trigger is the PARSER's placeholder, not anything the model wrote."""
        assert card_defect(_card(trigger=FALLBACK_TRIGGER)) == "no-card-header"

    @pytest.mark.parametrize("field", ["trigger", "symptoms", "domain"])
    def test_echoed_template_placeholders_are_refused(self, field):
        """Draft 2: `<one sentence — what situation activates this skill>` and
        its siblings, stored verbatim as real field values."""
        placeholder = "<one sentence — what situation activates this skill>"
        assert card_defect(_card(**{field: placeholder})) == (
            f"template-placeholder:{field}"
        )

    def test_a_card_with_no_steps_is_refused(self):
        """A skill without steps is not a playbook. The prompt asks for the
        section by name, so its absence means the format was not followed."""
        assert card_defect(_card(body="## What's happening\njust prose\n")) == "no-steps"

    def test_blank_output_is_still_refused(self):
        """The pre-existing guard's one real case must keep working."""
        assert card_defect({"trigger": "", "body": ""}) == "empty"

    def test_angle_brackets_inside_a_real_value_are_not_a_placeholder(self):
        """The test is start-AND-end, not "contains a bracket" — a trigger like
        `use <ctrl-c> to abort` is legitimate text and must not be refused."""
        assert card_defect(_card(trigger="press <ctrl-c> when the job stalls")) is None


class TestUnusableCardsAreNotStored:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw",
        [
            # No `---` header at all: raw deliberation.
            "We are given a specific procedure title... I think the domain should be",
            # The prompt template, echoed.
            "trigger: <one sentence — what situation activates this skill>\n"
            "symptoms: <observable signals: error messages, failing patterns>\n"
            "domain: <single word: e.g. neo4j, docker, qdrant, python, api-auth>\n"
            "verified_on: <project/YYYY-MM>\n---\n## Steps\n1. <first action>\n",
        ],
    )
    async def test_doc_draft_is_not_stored(self, raw):
        """Both live failures, end to end through the drafting path."""
        from app.skills.synthesizer import SkillSynthesizer

        settings = MagicMock()
        settings.QDRANT_COLLECTION = "c"
        synth = SkillSynthesizer(settings)
        store = AsyncMock()

        with (
            patch.object(synth, "_call_llm_doc", new=AsyncMock(return_value=raw)),
            patch.object(synth, "_store", new=store),
        ):
            result = await synth.synthesize_from_document(
                source_name="Runbook", procedure_title="Rotate the token",
                doc_content="body",
            )

        assert result["status"] == "empty"
        store.assert_not_awaited()


class TestTruncationIsAFailedDraft:
    @pytest.mark.asyncio
    async def test_native_length_stop_raises(self):
        """`done_reason: "length"` means the model stopped at the cap, not at
        the end of the answer — the card is half-written by construction.

        The live draft ended '...I think the domain should be'. Storing a
        mid-sentence fragment puts it in the review queue wearing the same
        badge as a real skill.
        """
        from app.skills import synthesizer as mod

        settings = MagicMock()
        settings.SKILL_SYNTH_TIMEOUT_SECONDS = 300.0
        settings.SKILL_SYNTH_MAX_TOKENS = 800
        synth = mod.SkillSynthesizer(settings)

        result = ChatResult(
            content="trigger: t\n---\n## Steps\n1. half a sen",
            reasoning="", endpoint="native", raw={"done_reason": "length"},
        )
        with patch.object(mod.llm, "chat", new=AsyncMock(return_value=result)):
            with pytest.raises(ValueError, match="truncated"):
                await synth._chat("prompt", purpose="test")

    @pytest.mark.asyncio
    async def test_openai_length_stop_raises(self):
        """Same fact, the other endpoint's field name."""
        from app.skills import synthesizer as mod

        settings = MagicMock()
        settings.SKILL_SYNTH_TIMEOUT_SECONDS = 300.0
        settings.SKILL_SYNTH_MAX_TOKENS = 800
        synth = mod.SkillSynthesizer(settings)

        result = ChatResult(
            content="trigger: t\n---\n## Steps\n1. half a sen", reasoning="",
            endpoint="openai", raw={"choices": [{"finish_reason": "length"}]},
        )
        with patch.object(mod.llm, "chat", new=AsyncMock(return_value=result)):
            with pytest.raises(ValueError, match="truncated"):
                await synth._chat("prompt", purpose="test")

    @pytest.mark.asyncio
    async def test_a_normal_stop_is_returned(self):
        """Absent or normal stop metadata must never refuse good output."""
        from app.skills import synthesizer as mod

        settings = MagicMock()
        settings.SKILL_SYNTH_TIMEOUT_SECONDS = 300.0
        settings.SKILL_SYNTH_MAX_TOKENS = 800
        synth = mod.SkillSynthesizer(settings)

        for raw in ({"done_reason": "stop"}, {"choices": [{"finish_reason": "stop"}]}, {}):
            result = ChatResult(
                content="a card", reasoning="", endpoint="native", raw=raw,
            )
            with patch.object(mod.llm, "chat", new=AsyncMock(return_value=result)):
                assert await synth._chat("p", purpose="test") == "a card"


class TestChatResultTruncated:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ({"done_reason": "length"}, True),
            ({"done_reason": "stop"}, False),
            ({"choices": [{"finish_reason": "length"}]}, True),
            ({"choices": [{"finish_reason": "stop"}]}, False),
            ({}, False),
            ({"choices": []}, False),
            ({"choices": "not-a-list"}, False),
            ({"choices": [None]}, False),
        ],
    )
    def test_reads_both_endpoints_and_never_raises(self, raw, expected):
        """Malformed or absent metadata means "cannot tell", which is False.

        Never refuse output on the basis of a field the backend did not send.
        """
        assert ChatResult("c", "", "native", raw).truncated is expected
