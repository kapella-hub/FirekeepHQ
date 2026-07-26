"""Tests for memory type classification (decay half-lives and model field)."""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import ActionLog


# ---------------------------------------------------------------------------
# ActionLog.memory_type field
# ---------------------------------------------------------------------------


class TestActionLogMemoryType:
    def test_default_is_episodic(self):
        log = ActionLog(action="did thing", outcome="it worked")
        assert log.memory_type == "episodic"

    @pytest.mark.parametrize("memory_type", ["reference", "procedural", "episodic", "transient"])
    def test_accepts_valid_types(self, memory_type: str):
        log = ActionLog(action="did thing", outcome="it worked", memory_type=memory_type)
        assert log.memory_type == memory_type

    def test_rejects_invalid_type(self):
        with pytest.raises(ValidationError):
            ActionLog(action="did thing", outcome="it worked", memory_type="invalid")

    def test_serialization_includes_memory_type(self):
        log = ActionLog(action="a", outcome="b", memory_type="transient")
        data = log.model_dump()
        assert data["memory_type"] == "transient"


# ---------------------------------------------------------------------------
# Per-type decay config
# ---------------------------------------------------------------------------


class TestDecayConfig:
    def test_default_decay_values(self):
        s = Settings(NEO4J_PASSWORD="test", LLM_API_KEY="test")
        assert s.DECAY_REFERENCE_DAYS == 0
        assert s.DECAY_PROCEDURAL_DAYS == 180
        assert s.DECAY_EPISODIC_DAYS == 90
        assert s.DECAY_TRANSIENT_DAYS == 14

    def test_decay_ordering(self):
        """Transient < episodic < procedural; reference has no decay."""
        s = Settings(NEO4J_PASSWORD="test", LLM_API_KEY="test")
        assert s.DECAY_REFERENCE_DAYS == 0
        assert s.DECAY_TRANSIENT_DAYS < s.DECAY_EPISODIC_DAYS
        assert s.DECAY_EPISODIC_DAYS < s.DECAY_PROCEDURAL_DAYS

    def test_legacy_global_half_life_still_exists(self):
        """The old MEMORY_DECAY_HALF_LIFE_DAYS setting is preserved for backward compat."""
        s = Settings(NEO4J_PASSWORD="test", LLM_API_KEY="test")
        assert s.MEMORY_DECAY_HALF_LIFE_DAYS == 90
