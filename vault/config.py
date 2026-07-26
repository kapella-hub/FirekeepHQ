"""Vault configuration."""

from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings


class VaultSettings(BaseSettings):
    """Vault settings. Loaded from env vars with VAULT_ prefix."""

    ENABLED: bool = True
    KEY: str = ""
    REDIS_URL: str = "redis://localhost:6379/7"

    model_config = {"env_prefix": "VAULT_", "env_file": ".env", "extra": "ignore"}


_settings: VaultSettings | None = None


def get_vault_settings() -> VaultSettings:
    global _settings
    if _settings is None:
        _settings = VaultSettings()
    return _settings


def generate_vault_key() -> str:
    """Generate a new Fernet encryption key."""
    return Fernet.generate_key().decode()
