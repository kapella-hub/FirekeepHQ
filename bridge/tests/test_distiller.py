"""Tests for FirekeepCortex distillation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import Settings
from app.distiller import Distiller


@pytest.fixture
def distiller():
    d = Distiller(Settings())
    # Replace the real client with a mock for testing
    d._client = AsyncMock()
    return d


class TestBuildPayload:
    def test_builds_action_from_goal_and_plan(self, distiller):
        data = {
            "goal": "fix auth bug",
            "plan": "- [x] Step 1: find bug\n- [x] Step 2: fix it\n- [ ] Step 3: test",
            "decisions": [{"timestamp": "t", "content": "used approach A"}],
            "progress": [{"timestamp": "t", "content": "done"}],
            "tags": ["auth", "bugfix"],
        }
        payload = distiller.build_payload(data, outcome="bug fixed")
        assert "fix auth bug" in payload["action"]
        assert payload["outcome"] == "bug fixed"
        assert "approach A" in payload["resolution"]
        assert "firekeepbridge" in payload["tags"]

    def test_uses_last_progress_when_no_outcome(self, distiller):
        data = {
            "goal": "task",
            "plan": "",
            "decisions": [],
            "progress": [{"timestamp": "t", "content": "last thing done"}],
            "tags": [],
        }
        payload = distiller.build_payload(data)
        assert payload["outcome"] == "last thing done"

    def test_fallback_outcome(self, distiller):
        data = {"goal": "task", "plan": "", "decisions": [], "progress": [], "tags": []}
        payload = distiller.build_payload(data)
        assert payload["outcome"] == "Session completed"


class TestDistill:
    @pytest.mark.asyncio
    async def test_successful_distillation(self, distiller):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "stored", "vector_id": "v-123"}
        mock_response.raise_for_status = MagicMock()

        distiller._client.post = AsyncMock(return_value=mock_response)

        result = await distiller.distill(
            {"goal": "t", "plan": "", "decisions": [], "progress": [], "tags": []},
        )
        assert result["status"] == "success"
        assert result["firekeep_memory_id"] == "v-123"

    @pytest.mark.asyncio
    async def test_failed_distillation(self, distiller):
        distiller._client.post = AsyncMock(side_effect=httpx.RequestError("down"))

        result = await distiller.distill(
            {"goal": "t", "plan": "", "decisions": [], "progress": [], "tags": []},
        )
        assert result["status"] == "failed"


class TestNamespaceUnification:
    def test_episodic_payload_targets_default_namespace(self, distiller):
        """Distillates must land where memory_recall looks (defect #9)."""
        data = {"goal": "g", "plan": "", "decisions": [], "progress": [], "tags": []}
        payload = distiller._build_episodic_payload(data)
        assert payload["namespace"] == "default"

    def test_legacy_payload_targets_default_namespace(self, distiller):
        data = {"goal": "g", "plan": "", "decisions": [], "progress": [], "tags": []}
        payload = distiller.build_payload(data)
        assert payload["namespace"] == "default"
