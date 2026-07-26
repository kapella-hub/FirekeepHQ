"""SP2 deploy-fix: knowledge classify timeout config.

The server-side classify timeout (sized for slow CPU Ollama) remains
configurable. The former ingest-client timeout invariant was removed once
POST /knowledge/ingest became async (202 + background classify/draft) —
the MCP tool no longer waits on the classify call, so there is nothing left
to outlast.
"""
from __future__ import annotations

from app.config import Settings


def test_classify_timeout_default():
    s = Settings()
    assert s.KNOWLEDGE_CLASSIFY_TIMEOUT_SECONDS == 300.0
    assert not hasattr(s, "KNOWLEDGE_INGEST_CLIENT_TIMEOUT_SECONDS")
