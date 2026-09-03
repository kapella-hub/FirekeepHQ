"""skill_recall requests status=recallable and labels trial skills in output."""
import pytest

from app import mcp_server


@pytest.fixture(autouse=True)
def _reset_client():
    """Reset the shared httpx client between tests (mirrors test_mcp_skill_create_fleet.py).

    Without this, a real `httpx.AsyncClient` left cached by an earlier test file
    would short-circuit `_get_client()`'s `_client is None or _client.is_closed`
    check before it ever calls the patched constructor below.
    """
    mcp_server._client = None
    yield
    mcp_server._client = None


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


class _Client:
    params: dict = {}
    is_closed = False

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, path, params=None, headers=None, **_k):
        _Client.params = params or {}
        return _Resp([
            {
                "trigger": "Rotate the Neo4j password",
                "content": "Rotate it via the admin console.",
                "skill_status": "active",
            },
            {
                "trigger": "Restore from backup",
                "content": "Restore the nightly snapshot.",
                "skill_status": "trial",
            },
        ])


@pytest.mark.asyncio
async def test_skill_recall_requests_recallable_and_labels_trials(monkeypatch):
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", _Client)
    out = await mcp_server.skill_recall("rotate the neo4j password")
    assert _Client.params["status"] == "recallable"
    active_idx = out.index("**Rotate the Neo4j password**")
    trial_idx = out.index("**[TRIAL] Restore from backup**")
    assert active_idx < trial_idx
    assert "trial skill: not yet proven, verify before relying on it" in out
