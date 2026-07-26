"""Tests for FirekeepBridge configuration."""

import os

from app.config import Settings


class TestSettings:
    def test_defaults(self):
        s = Settings()
        assert s.REDIS_URL == "redis://localhost:6379/3"
        assert s.MCP_PORT == 8070
        assert s.SESSION_TTL_DAYS == 7
        assert s.MAX_SESSIONS == 100
        assert s.DEFAULT_AGENT_ID == "default"
        assert s.PLAN_MAX_BYTES == 10240
        assert s.DECISIONS_MAX == 50
        assert s.FILES_MAX == 100
        assert s.FIREKEEP_API_KEY is None

    def test_env_prefix(self):
        os.environ["NB_MCP_PORT"] = "9999"
        try:
            s = Settings()
            assert s.MCP_PORT == 9999
        finally:
            del os.environ["NB_MCP_PORT"]

    def test_empty_api_key_becomes_none(self):
        os.environ["NB_FIREKEEP_API_KEY"] = ""
        try:
            s = Settings()
            assert s.FIREKEEP_API_KEY is None
        finally:
            del os.environ["NB_FIREKEEP_API_KEY"]

    def test_whitespace_api_key_becomes_none(self):
        os.environ["NB_FIREKEEP_API_KEY"] = "   "
        try:
            s = Settings()
            assert s.FIREKEEP_API_KEY is None
        finally:
            del os.environ["NB_FIREKEEP_API_KEY"]

    def test_valid_api_key_preserved(self):
        os.environ["NB_FIREKEEP_API_KEY"] = "secret-key-123"
        try:
            s = Settings()
            assert s.FIREKEEP_API_KEY == "secret-key-123"
        finally:
            del os.environ["NB_FIREKEEP_API_KEY"]

    def test_namespace_default_unified_with_cortex(self):
        """Defect #9: Bridge wrote/read namespace 'firekeepbridge' while memory_learn
        stores under 'default' — agent-learned memories never appeared in
        proactive recall. Both sides must use 'default'."""
        s = Settings()
        assert s.FIREKEEP_NAMESPACE == "default"
