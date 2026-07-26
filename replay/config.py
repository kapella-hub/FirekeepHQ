"""Replay Engine configuration — shared across all services that emit events."""

from pydantic_settings import BaseSettings


class ReplaySettings(BaseSettings):
    """Replay configuration. Loaded from env vars with RP_ prefix."""

    ENABLED: bool = True
    REDIS_URL: str = "redis://localhost:6379/6"

    # Retention
    RETENTION_DAYS: int = 30
    STREAM_MAXLEN: int = 100_000

    # Context snapshots
    SNAPSHOT_INTERVAL: int = 50  # Force full snapshot every N events if no decision
    SNAPSHOT_TTL_DAYS: int = 30

    # Idempotency dedup window
    DEDUP_TTL_SECONDS: int = 300  # 5 minutes

    # Trimming schedule (seconds between runs)
    TRIM_INTERVAL_SECONDS: int = 3600  # 1 hour

    model_config = {"env_prefix": "RP_", "env_file": ".env", "extra": "ignore"}


_settings: ReplaySettings | None = None


def get_replay_settings() -> ReplaySettings:
    global _settings
    if _settings is None:
        _settings = ReplaySettings()
    return _settings
