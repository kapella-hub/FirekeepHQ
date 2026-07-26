"""Startup log hygiene for empty-secret checks in Settings.model_post_init.

An empty LLM_API_KEY is the NORMAL state for local/office Ollama deployments
(no compose file sets it; every call site sends no Authorization header when
it is empty). It must not surface as a WARNING that reads like a
misconfiguration — INFO with Ollama context is the correct level.
NEO4J_PASSWORD, by contrast, is always required and must keep warning.
"""

from __future__ import annotations

import logging

from app.config import Settings


def _messages(caplog, needle: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if needle in r.getMessage()]


def test_empty_llm_api_key_logs_info_with_ollama_context(caplog):
    with caplog.at_level(logging.DEBUG, logger="app.config"):
        Settings(NEO4J_PASSWORD="x", LLM_API_KEY="")
    records = _messages(caplog, "LLM_API_KEY")
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert "Ollama" in records[0].getMessage()


def test_set_llm_api_key_logs_nothing(caplog):
    with caplog.at_level(logging.DEBUG, logger="app.config"):
        Settings(NEO4J_PASSWORD="x", LLM_API_KEY="sk-something")
    assert _messages(caplog, "LLM_API_KEY") == []


def test_empty_neo4j_password_still_warns(caplog):
    with caplog.at_level(logging.DEBUG, logger="app.config"):
        Settings(NEO4J_PASSWORD="", LLM_API_KEY="x")
    records = _messages(caplog, "NEO4J_PASSWORD")
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
