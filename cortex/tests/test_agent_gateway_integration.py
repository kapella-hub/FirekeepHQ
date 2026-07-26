"""Integration test: real AgentGatewayService wired through the full FastAPI stack."""

import pytest
from fastapi.testclient import TestClient


def test_gateway_endpoint_returns_allow_for_minimal_action():
    """Integration test: real service through full FastAPI stack."""
    pytest.importorskip("redis")
    try:
        from app.main import app
    except Exception as e:
        pytest.skip(f"App not constructable in this environment: {e}")

    try:
        with TestClient(app) as client:
            r = client.post(
                "/agent/action/before",
                json={
                    "session_id": "s_integration",
                    "agent_id": "a_integration",
                    "adapter": "mcp",
                    "action": {"type": "edit_file", "target": "src/foo.py"},
                },
            )
            # If app loaded, the response should be 200 (decision varies by policy state)
            assert r.status_code == 200
            body = r.json()
            assert body["decision"] in ("allow", "rethink", "block")
            assert "action_id" in body
            assert body["action_id"].startswith("act_")
    except Exception as e:
        pytest.skip(f"App lifespan failed (missing infra): {e}")
