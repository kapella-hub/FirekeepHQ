"""Atomic distillation: a session becomes a compact summary plus one memory
per decision, instead of one transcript-shaped blob (distiller.py, "Atomic
distillation")."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import Settings
from app.distiller import Distiller

LONG_A = "Receipts are sourced from the replay trace rather than the tool-call stream"
LONG_B = "The Keep panel reads through a gateway child with a read-only tool allowlist"


@pytest.fixture
def distiller():
    return Distiller(Settings(_env_file=None))


def _ok(vector_id):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"status": "stored", "vector_id": vector_id}
    return resp


def _session(**over):
    data = {
        "goal": "Rethink Studio UX",
        "plan": "",
        "decisions": [],
        "progress": [],
        "files": {},
        "tags": [],
    }
    data.update(over)
    return data


class TestSummaryPayload:
    def test_empty_sections_leave_no_boilerplate(self, distiller):
        payload = distiller._build_summary_payload(_session(), outcome="done")
        assert payload["action"] == "Rethink Studio UX"
        for filler in ("No plan steps recorded", "No decisions recorded", "No files"):
            assert filler not in payload["action"]
        assert payload["resolution"] is None

    def test_long_decisions_are_not_repeated_in_the_summary(self, distiller):
        data = _session(decisions=[{"content": LONG_A}, {"content": "Use option C"}])
        payload = distiller._build_summary_payload(data, outcome="done")
        assert LONG_A not in payload["action"]
        # A short decision is not atomized, so the summary is where it lives.
        assert "Use option C" in payload["action"]

    def test_steps_and_files_appear_when_recorded(self, distiller):
        data = _session(
            plan="- [x] Write spec\n- [ ] Ship\n",
            files={"src/app.tsx": "x", "docs/spec.md": "y"},
        )
        payload = distiller._build_summary_payload(data, outcome="done")
        assert "Steps: Write spec" in payload["action"]
        assert "Files: docs/spec.md, src/app.tsx" in payload["action"]
        assert {"tsx", "md", "firekeepbridge"} <= set(payload["tags"])

    def test_summary_fits_inside_the_embedding_window(self, distiller):
        data = _session(
            goal="g" * 3000,
            progress=[{"content": "p" * 3000} for _ in range(40)],
            files={f"src/f{i}.py": "x" for i in range(200)},
        )
        payload = distiller._build_summary_payload(data, outcome="o" * 9000)
        text = (f"{payload['action']}. The outcome was: {payload['outcome']}."
                f" Resolution: {payload['resolution']}")
        assert len(text) <= 2000

    def test_resolution_is_the_tail_of_progress_not_all_of_it(self, distiller):
        data = _session(progress=[{"content": f"step {i}"} for i in range(10)])
        payload = distiller._build_summary_payload(data, outcome="shipped")
        assert payload["resolution"] == "step 7 → step 8 → step 9"

    def test_single_progress_entry_used_as_outcome_is_not_duplicated(self, distiller):
        data = _session(progress=[{"content": "all green"}])
        payload = distiller._build_summary_payload(data)
        assert payload["outcome"] == "all green"
        assert payload["resolution"] is None


class TestDecisionPayloads:
    def test_one_memory_per_long_decision(self, distiller):
        data = _session(decisions=[{"content": LONG_A}, {"content": LONG_B}],
                        tags=["studio"])
        payloads = distiller._build_decision_payloads(data)
        assert [p["action"] for p in payloads] == [LONG_A, LONG_B]
        for p in payloads:
            assert p["outcome"] == "Decided while working on: Rethink Studio UX"
            assert {"studio", "decision", "firekeepbridge"} <= set(p["tags"])
            assert p["namespace"] == "default"

    def test_short_decisions_are_not_atomized(self, distiller):
        data = _session(decisions=[{"content": "Use approach A"},
                                   {"content": "Use approach B"}])
        assert distiller._build_decision_payloads(data) == []

    def test_keeps_the_newest_when_over_the_cap(self, distiller):
        decisions = [{"content": f"decision number {i:02d} " + "x" * 40}
                     for i in range(30)]
        payloads = distiller._build_decision_payloads(_session(decisions=decisions))
        assert len(payloads) == 12
        assert payloads[0]["action"].startswith("decision number 18")
        assert payloads[-1]["action"].startswith("decision number 29")

    def test_duplicates_and_blanks_are_dropped(self, distiller):
        data = _session(decisions=[{"content": LONG_A}, {"content": "  "},
                                   {"content": LONG_A}, {}])
        assert len(distiller._build_decision_payloads(data)) == 1

    def test_goal_context_is_bounded(self, distiller):
        data = _session(goal="g" * 500, decisions=[{"content": LONG_A}])
        (payload,) = distiller._build_decision_payloads(data)
        assert len(payload["outcome"]) <= len("Decided while working on: ") + 80


class TestDistillWrites:
    @pytest.mark.asyncio
    async def test_writes_summary_then_each_decision(self, distiller):
        distiller._client.post = AsyncMock(
            side_effect=[_ok("sum"), _ok("d1"), _ok("d2")])
        data = _session(decisions=[{"content": LONG_A}, {"content": LONG_B}],
                        project="Studio")
        result = await distiller.distill(data, outcome="done", session_id="s-1")

        assert result == {"status": "success", "firekeep_memory_id": "sum",
                          "atom_ids": ["d1", "d2"]}
        calls = distiller._client.post.await_args_list
        assert [c.kwargs["json"]["action"] for c in calls] == [
            "Rethink Studio UX", LONG_A, LONG_B]
        for c in calls:
            assert c.kwargs["headers"]["X-Session-Id"] == "s-1"
            assert c.kwargs["json"]["project"] == "Studio"

    @pytest.mark.asyncio
    async def test_a_failed_decision_write_fails_the_distillation(self, distiller):
        """So the worker retries; the retry re-upserts text-derived point ids."""
        distiller._client.post = AsyncMock(
            side_effect=[_ok("sum"), httpx.RequestError("down")])
        data = _session(decisions=[{"content": LONG_A}])
        result = await distiller.distill(data, outcome="done")
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_payloads_are_deterministic_across_retries(self, distiller):
        data = _session(decisions=[{"content": LONG_A}, {"content": LONG_B}],
                        progress=[{"content": "a"}, {"content": "b"}])
        first = ([distiller._build_summary_payload(data, "done")]
                 + distiller._build_decision_payloads(data))
        second = ([distiller._build_summary_payload(data, "done")]
                  + distiller._build_decision_payloads(data))
        assert first == second
