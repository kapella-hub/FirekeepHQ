"""Tests for episodic memory distillation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.distiller import Distiller, _truncate


@pytest.fixture
def distiller():
    d = Distiller(Settings())
    d._client = AsyncMock()
    return d


def _session_data(**overrides):
    """Build a realistic session data dict with sensible defaults."""
    base = {
        "goal": "Implement feature X",
        "plan": "- [x] Step 1\n- [x] Step 2\n- [ ] Step 3",
        "decisions": [
            {"timestamp": "2026-03-15T10:00:00Z", "content": "Use approach A"},
            {"timestamp": "2026-03-15T10:05:00Z", "content": "Refactor module B"},
            {"timestamp": "2026-03-15T10:10:00Z", "content": "Add error handling"},
        ],
        "progress": [
            {"timestamp": "2026-03-15T10:01:00Z", "content": "Scaffolded module"},
            {"timestamp": "2026-03-15T10:06:00Z", "content": "Refactored B"},
            {"timestamp": "2026-03-15T10:11:00Z", "content": "Tests passing"},
        ],
        "files": {
            "src/feature.py": {"summary": "New feature", "last_action": "created"},
            "tests/test_feature.py": {"summary": "Tests", "last_action": "created"},
        },
        "scratch": {"note": "quick note"},
        "tags": ["feature", "backend"],
    }
    base.update(overrides)
    return base


class TestEpisodicPayloadDecisionSequence:
    def test_preserves_decision_order_with_arrow_separator(self, distiller):
        data = _session_data()
        payload = distiller._build_episodic_payload(data)

        assert "Use approach A" in payload["action"]
        assert "Refactor module B" in payload["action"]
        assert "Add error handling" in payload["action"]
        # Sequence preserved via arrow
        assert "Use approach A \u2192 Refactor module B \u2192 Add error handling" in payload["action"]

    def test_includes_up_to_ten_decisions(self, distiller):
        decisions = [
            {"timestamp": f"t{i}", "content": f"decision-{i}"} for i in range(12)
        ]
        data = _session_data(decisions=decisions)
        payload = distiller._build_episodic_payload(data)

        # First 10 included, 11th and 12th excluded
        for i in range(10):
            assert f"decision-{i}" in payload["action"]
        assert "decision-10" not in payload["action"]
        assert "decision-11" not in payload["action"]


class TestEpisodicPayloadFilePaths:
    def test_includes_file_paths_in_action(self, distiller):
        data = _session_data()
        payload = distiller._build_episodic_payload(data)

        assert "src/feature.py" in payload["action"]
        assert "tests/test_feature.py" in payload["action"]

    def test_file_extensions_added_as_tags(self, distiller):
        data = _session_data(
            files={
                "main.go": {"summary": "entry", "last_action": "modified"},
                "utils.ts": {"summary": "helpers", "last_action": "created"},
                "README.md": {"summary": "docs", "last_action": "modified"},
            },
        )
        payload = distiller._build_episodic_payload(data)

        assert "go" in payload["tags"]
        assert "ts" in payload["tags"]
        assert "md" in payload["tags"]


class TestEpisodicPayloadOutcome:
    def test_explicit_outcome_used_when_provided(self, distiller):
        data = _session_data()
        payload = distiller._build_episodic_payload(data, outcome="Feature shipped")

        assert payload["outcome"] == "Feature shipped"

    def test_falls_back_to_last_progress_entry(self, distiller):
        data = _session_data()
        payload = distiller._build_episodic_payload(data)

        assert payload["outcome"] == "Tests passing"

    def test_falls_back_to_default_when_no_progress(self, distiller):
        data = _session_data(progress=[])
        payload = distiller._build_episodic_payload(data)

        assert payload["outcome"] == "Session completed"


class TestEpisodicPayloadResolution:
    def test_resolution_is_full_progress_sequence(self, distiller):
        data = _session_data()
        payload = distiller._build_episodic_payload(data)

        assert payload["resolution"] == "Scaffolded module \u2192 Refactored B \u2192 Tests passing"

    def test_resolution_none_when_no_progress(self, distiller):
        data = _session_data(progress=[])
        payload = distiller._build_episodic_payload(data)

        assert payload["resolution"] is None


class TestEpisodicPayloadMetadata:
    def test_memory_type_is_episodic(self, distiller):
        data = _session_data()
        payload = distiller._build_episodic_payload(data)

        assert payload["memory_type"] == "episodic"

    def test_domain_is_development(self, distiller):
        data = _session_data()
        payload = distiller._build_episodic_payload(data)

        assert payload["domain"] == "development"

    def test_firekeepbridge_tag_always_present(self, distiller):
        data = _session_data(tags=[])
        payload = distiller._build_episodic_payload(data)

        assert "firekeepbridge" in payload["tags"]

    def test_tags_limited_to_max(self, distiller):
        data = _session_data(tags=[f"tag-{i}" for i in range(25)])
        payload = distiller._build_episodic_payload(data)

        assert len(payload["tags"]) <= 20


class TestEpisodicPayloadEmptyInputs:
    def test_empty_decisions(self, distiller):
        data = _session_data(decisions=[])
        payload = distiller._build_episodic_payload(data)

        assert "No decisions recorded" in payload["action"]

    def test_empty_progress(self, distiller):
        data = _session_data(progress=[])
        payload = distiller._build_episodic_payload(data)

        assert payload["outcome"] == "Session completed"
        assert payload["resolution"] is None

    def test_empty_files(self, distiller):
        data = _session_data(files={})
        payload = distiller._build_episodic_payload(data)

        assert "No files" in payload["action"]

    def test_completely_empty_data(self, distiller):
        payload = distiller._build_episodic_payload({})

        assert "Unknown task" in payload["action"]
        assert payload["outcome"] == "Session completed"
        assert payload["resolution"] is None
        assert "firekeepbridge" in payload["tags"]
        assert payload["memory_type"] == "episodic"


class TestTruncation:
    def test_truncate_short_text(self):
        assert _truncate("hello", 100) == "hello"

    def test_truncate_long_text(self):
        text = "a" * 6000
        result = _truncate(text, 5000)
        assert len(result) == 5000
        assert result.endswith("...")

    def test_action_field_respects_limit(self, distiller):
        # Create data that would produce a very long action string
        decisions = [
            {"timestamp": f"t{i}", "content": "x" * 500} for i in range(10)
        ]
        data = _session_data(decisions=decisions)
        payload = distiller._build_episodic_payload(data)

        assert len(payload["action"]) <= 5000
