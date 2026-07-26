import pytest
from unittest.mock import AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent_gateway.api import create_agent_gateway_router
from app.agent_gateway.models import (
    ActionAfterResponse,
    ActionBeforeResponse,
)


class _StubService:
    """Minimal stub service implementing decide/record."""

    async def decide(self, body) -> ActionBeforeResponse:
        return ActionBeforeResponse(
            decision="allow",
            action_id="act_stub",
            tier="auto",
            auto_reconcile=False,
        )

    async def record(self, body) -> ActionAfterResponse:
        return ActionAfterResponse(
            action_id=body.action_id,
            recorded=True,
        )


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(create_agent_gateway_router(get_service=lambda: _StubService()))
    return TestClient(app)


@pytest.fixture
def mock_service():
    svc = AsyncMock()
    svc.decide.return_value = ActionBeforeResponse(
        decision="allow", action_id="act_mock", tier="lightweight", auto_reconcile=False,
    )
    svc.record.return_value = ActionAfterResponse(
        action_id="act_mock", recorded=True,
    )
    return svc


@pytest.fixture
def mock_client(mock_service):
    app = FastAPI()
    app.include_router(create_agent_gateway_router(get_service=lambda: mock_service))
    return TestClient(app)


def test_action_before_returns_allow_for_minimal_request(client):
    r = client.post(
        "/agent/action/before",
        json={
            "session_id": "s1",
            "agent_id": "a1",
            "adapter": "mcp",
            "action": {"type": "edit_file", "target": "/tmp/foo.py"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "allow"
    assert body["action_id"] == "act_stub"
    assert body["tier"] == "auto"


def test_action_before_rejects_bad_payload(client):
    r = client.post(
        "/agent/action/before",
        json={"session_id": "s1"},
    )
    assert r.status_code == 422


def test_action_after_accepts_outcome(client):
    r = client.post(
        "/agent/action/after",
        json={
            "action_id": "act_stub",
            "outcome": {"success": True},
        },
    )
    assert r.status_code == 200
    assert r.json()["recorded"] is True


def test_action_before_delegates_to_service_decide(mock_client, mock_service):
    r = mock_client.post(
        "/agent/action/before",
        json={
            "session_id": "s1",
            "agent_id": "a1",
            "adapter": "mcp",
            "action": {"type": "edit_file", "target": "/tmp/foo.py"},
        },
    )
    assert r.status_code == 200
    assert r.json()["action_id"] == "act_mock"
    mock_service.decide.assert_awaited_once()


def test_action_after_delegates_to_service_record(mock_client, mock_service):
    r = mock_client.post(
        "/agent/action/after",
        json={"action_id": "act_mock", "outcome": {"success": True}},
    )
    assert r.status_code == 200
    assert r.json()["recorded"] is True
    mock_service.record.assert_awaited_once()
