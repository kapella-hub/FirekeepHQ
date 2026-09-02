"""skill_create forwards origin_job / reauthor_of to POST /skills only when given."""
import inspect

import pytest

from app import mcp_server


@pytest.fixture(autouse=True)
def _reset_client():
    """Reset the shared httpx client between tests (mirrors test_mcp_server.py).

    Without this, a real `httpx.AsyncClient` left cached by an earlier test file
    would short-circuit `_get_client()`'s `_client is None or _client.is_closed`
    check before it ever calls the patched constructor below.
    """
    mcp_server._client = None
    yield
    mcp_server._client = None


def test_signature_has_the_two_optional_params():
    sig = inspect.signature(mcp_server.skill_create)
    assert sig.parameters["origin_job"].default is None
    assert sig.parameters["reauthor_of"].default is None


class _Resp:
    status_code = 201

    def json(self):
        return {"id": "sk-new", "trigger": "t", "skill_status": "draft"}

    def raise_for_status(self):
        return None


class _Client:
    sent: dict = {}
    # `_get_client()` caches this instance across calls and re-checks
    # `.is_closed` on every subsequent call — a plain object with no such
    # attribute would raise AttributeError on the second `skill_create` call
    # in the same test, so the fake must answer that check like the real thing.
    is_closed = False

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, path, json=None, headers=None, **_k):
        _Client.sent = {"path": path, "json": json, "headers": headers}
        return _Resp()


@pytest.mark.asyncio
async def test_body_carries_them_only_when_truthy(monkeypatch):
    # Patch the constructor skill_create actually uses (see file header).
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", _Client)
    await mcp_server.skill_create("t", "s", "steps", status="draft",
                                  origin_job="reauthor_stale_skill", reauthor_of="old-1")
    assert _Client.sent["json"]["origin_job"] == "reauthor_stale_skill"
    assert _Client.sent["json"]["reauthor_of"] == "old-1"
    await mcp_server.skill_create("t", "s", "steps", status="draft")
    assert "origin_job" not in _Client.sent["json"] and "reauthor_of" not in _Client.sent["json"]
