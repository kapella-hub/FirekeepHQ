"""Configuration for FirekeepSentinel — loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379/4"
    DOCKER_SOCKET: str = "/var/run/docker.sock"
    POLL_INTERVAL_DOCKER: int = 30
    POLL_INTERVAL_GIT: int = 60
    POLL_INTERVAL_FILES: int = 30
    EVENT_RETENTION_HOURS: int = 72
    EVENT_MAXLEN: int = 10000
    WATCH_PATHS: str = ""
    CORS_ORIGINS: str = '["*"]'
    # Alerting: broadcast error+ events to Relay
    RELAY_URL: str = "http://relay:8050"
    ALERT_SEVERITIES: str = "error,critical"
    # Auto-indexing: trigger Symdex reindex on new git commits
    SYMDEX_URL: str = "http://symdex:8090"
    AUTO_INDEX_ENABLED: bool = True
    # Cortex API for webhook firing
    CORTEX_API_URL: str = "http://cortex-api:8000"
    # Internal service key for server-initiated outbound calls under office
    # AUTH_ENABLED=true (Sentinel->Relay alert broadcast, Sentinel->Cortex
    # webhook). Populated by NS_FIREKEEP_INTERNAL_KEY, wired from the top-level
    # FIREKEEP_INTERNAL_KEY in docker-compose.yml. Unset on personal VPS
    # (AUTH_ENABLED=false) -> outbound calls stay byte-identical to today.
    FIREKEEP_INTERNAL_KEY: str | None = None

    model_config = {"env_prefix": "NS_", "env_file": ".env", "extra": "ignore"}


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
