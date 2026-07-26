"""Skill Synthesis package (auto crystallization of breakthrough sessions)."""
from __future__ import annotations


def internal_key_headers(internal_key: str | None) -> dict[str, str]:
    """X-API-Key header for server-initiated cortex->bridge calls.

    Skill Synthesis (synthesizer.py + scorer.py) fetches session data from
    Bridge on cortex's own behalf — there is no end-user caller to forward a
    key from, so this always uses the internal service key (SP1a
    FIREKEEP_INTERNAL_KEY), never a per-request caller key. Under
    AUTH_ENABLED=true, an unheadered GET to Bridge's /sessions* routes 401s,
    which silently disables Skill Synthesis; under AUTH_ENABLED=false
    (personal-VPS default) FIREKEEP_INTERNAL_KEY is unset and this returns {},
    leaving requests byte-identical to today.
    """
    return {"X-API-Key": internal_key} if internal_key else {}
