"""Corpus configuration. Loaded from CORPUS_ prefixed env vars."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class CorpusSettings(BaseSettings):
    """Settings for the Corpus business knowledge graph module."""

    ENABLED: bool = True
    CHUNK_SIZE: int = 1500
    CHUNK_OVERLAP: int = 200
    MAX_ENTITIES_PER_CHUNK: int = 20
    MAX_RELATIONSHIPS_PER_CHUNK: int = 30

    model_config = SettingsConfigDict(
        env_prefix="CORPUS_",
        env_file=".env",
        extra="ignore",
    )


@lru_cache()
def get_corpus_settings() -> CorpusSettings:
    return CorpusSettings()
