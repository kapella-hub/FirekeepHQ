"""Configuration for FirekeepRelay — loaded from environment variables."""

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379/5"
    MCP_HOST: str = "0.0.0.0"
    MCP_PORT: int = 8050
    BULLETIN_TTL_HOURS: int = Field(default=24, gt=0)
    CHANNEL_BACKLOG_SIZE: int = Field(default=100, gt=0)
    CLAIM_TTL_MINUTES: int = Field(default=30, gt=0)
    BRIDGE_URL: str = "http://bridge:8070"
    # No FIREKEEP_API_KEY (NR_FIREKEEP_API_KEY) since 2026-10-05, THREAT-MODEL
    # §5.18: Relay writes scope decisions into Bridge with the key of the
    # member who owns the session, and with auth off Bridge checks no key.
    # extra="ignore" keeps a lingering NR_FIREKEEP_API_KEY harmless.

    model_config = {"env_prefix": "NR_", "env_file": ".env", "extra": "ignore"}


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
