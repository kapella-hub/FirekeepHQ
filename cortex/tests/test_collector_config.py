"""SP3 Living Knowledge Sync: collector config settings (spine)."""
from __future__ import annotations

from app.config import Settings


def test_collector_defaults():
    s = Settings()
    assert s.COLLECTORS_ENABLED is False
    assert s.CONFLUENCE_COLLECTOR_ENABLED is False
    assert s.CONFLUENCE_PAT_VAULT_KEY == "confluence_pat"
    assert s.CONFLUENCE_COLLECTOR_SCHEDULE_HOURS == 24.0
    assert s.COLLECTOR_LOCK_TTL_SECONDS == 3600
