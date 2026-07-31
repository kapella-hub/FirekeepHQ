"""Shared fixtures for hook-core tests (Windows-first: never touch real ~)."""
from __future__ import annotations

import textwrap

import pytest


@pytest.fixture
def client_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text(textwrap.dedent("""\
        [identity]
        agent_id = tester
        [server]
        kind = ports
        scheme = http
        host = 127.0.0.1
        verify_tls = false
    """))
    cache = tmp_path / "cache"
    cache.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(cache))
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(logs))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    monkeypatch.delenv("FIREKEEP_AGENT_GOAL", raising=False)
    return {"tmp": tmp_path, "cfg": cfg, "cache": cache, "logs": logs, "agent": "tester"}
