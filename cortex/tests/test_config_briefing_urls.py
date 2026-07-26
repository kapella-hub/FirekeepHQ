"""SP1b: cortex config exposes RELAY_URL + SENTINEL_URL for the briefing aggregator.

The GET /briefing router (WS-2) fans out to Relay + Sentinel REST; it reads
these two base URLs from Settings. Defaults are the docker service DNS names,
overridable via env (docker-compose passes ${RELAY_URL}/${SENTINEL_URL}).
"""
from __future__ import annotations

from app.config import Settings


def test_relay_and_sentinel_urls_default_to_service_names():
    s = Settings()
    assert s.RELAY_URL == "http://relay:8050"
    assert s.SENTINEL_URL == "http://sentinel:8060"


def test_relay_and_sentinel_urls_overridable_via_env(monkeypatch):
    monkeypatch.setenv("RELAY_URL", "http://relay-test:9050")
    monkeypatch.setenv("SENTINEL_URL", "http://sentinel-test:9060")
    s = Settings()
    assert s.RELAY_URL == "http://relay-test:9050"
    assert s.SENTINEL_URL == "http://sentinel-test:9060"
