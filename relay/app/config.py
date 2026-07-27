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
    FIREKEEP_API_KEY: str | None = None

    model_config = {"env_prefix": "NR_", "env_file": ".env", "extra": "ignore"}


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
