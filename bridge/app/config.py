"""Configuration for FirekeepBridge — loaded from environment variables."""

import logging

from pydantic import field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379/3"
    MCP_HOST: str = "0.0.0.0"
    MCP_PORT: int = 8070
    FIREKEEP_API_URL: str = "http://localhost:8100"
    FIREKEEP_API_KEY: str | None = None
    # Unified with Cortex's default namespace so distillates and proactive
    # recall see the same memories agents store via memory_learn (SP0 C1).
    FIREKEEP_NAMESPACE: str = "default"
    SESSION_TTL_DAYS: int = 7
    MAX_SESSIONS: int = 100
    DEFAULT_AGENT_ID: str = "default"

    # Proactive recall
    PROACTIVE_RECALL_ENABLED: bool = True
    PROACTIVE_RECALL_TOP_K: int = 3
    PROACTIVE_RECALL_MIN_SCORE: float = 0.35  # raw-cosine scale, matches Cortex RECALL_SCORE_FLOOR
    PROACTIVE_RECALL_CATEGORIES: str = "plan,progress"

    # Crashed-session reaper (app/reaper.py). A session whose agent died without
    # calling ctx_complete_session sits status="active" forever — no TTL, never
    # distilled, never evaluated. OWM treats a Bridge "abandoned" session as an
    # outright failure signal, so those walked-away sessions are precisely the
    # missing outcome truth; today they vanish from scoring instead of counting
    # against it. Reaping them is what makes the failure signal exist.
    REAPER_ENABLED: bool = True
    # Three days of silence on an "active" session is a crash or a walk-away,
    # not a lunch break. Conservative on purpose: reaping is not reversible in
    # any useful sense (an abandoned session is never distilled), so the cost of
    # waiting too long is a delayed signal while the cost of reaping too early
    # is discarding live work.
    REAPER_IDLE_HOURS: int = 72
    REAPER_INTERVAL_SECONDS: int = 3600
    # Per-pass ceiling. The first pass after enabling on an old deployment
    # would otherwise drain months of backlog in one sweep — several Redis
    # round trips plus a fire-and-forget eval POST per session, all in one
    # burst. Capped, the hourly loop drains the same backlog at a bounded
    # rate; at steady state a pass never comes near the cap.
    REAPER_MAX_PER_PASS: int = 500

    # Component size limits
    PLAN_MAX_BYTES: int = 10240  # 10 KB
    DECISIONS_MAX: int = 50
    FILES_MAX: int = 100
    PROGRESS_MAX: int = 50
    SCRATCH_MAX: int = 50

    model_config = {"env_prefix": "NB_", "env_file": ".env", "extra": "ignore"}

    @field_validator("FIREKEEP_API_KEY", mode="before")
    @classmethod
    def empty_api_key_to_none(cls, v: str | None) -> str | None:
        if v is not None and v.strip() == "":
            logger.warning("FIREKEEP_API_KEY is empty — authentication disabled")
            return None
        return v


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        if _settings.FIREKEEP_API_KEY is None:
            logger.warning("No API key configured for FirekeepCortex")
    return _settings
