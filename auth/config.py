"""Auth configuration."""

from pydantic_settings import BaseSettings


class AuthSettings(BaseSettings):
    """Auth settings. Loaded from env vars with AUTH_ prefix."""

    ENABLED: bool = False  # When False, all requests pass through
    REDIS_URL: str = "redis://localhost:6379/7"

    model_config = {"env_prefix": "AUTH_", "env_file": ".env", "extra": "ignore"}


_settings: AuthSettings | None = None


def get_auth_settings() -> AuthSettings:
    global _settings
    if _settings is None:
        _settings = AuthSettings()
    return _settings
