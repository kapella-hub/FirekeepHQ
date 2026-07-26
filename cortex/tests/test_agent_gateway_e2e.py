"""End-to-end: predict → allow → record → score appears in eval metrics."""

import pytest
from fastapi.testclient import TestClient


def _make_client(app):
    """Enter TestClient lifespan and return the started client, or skip on infra error."""
    client = TestClient(app)
    try:
        client.__enter__()
    except AssertionError:
        raise  # real test failure — let it propagate
    except Exception as e:
        pytest.skip(f"App lifespan failed (missing infra?): {e}")
    return client


def test_e2e_predict_then_act_loop():
    """Full happy path: submit prediction, allow, report outcome with full match."""
    pytest.importorskip("redis")
    try:
        from app.main import app
    except Exception as e:
        pytest.skip(f"App not importable in this environment: {e}")

    client = _make_client(app)
    try:
        # 1. Submit an action with a prediction
        before_resp = client.post("/agent/action/before", json={
            "session_id": "s_e2e",
            "agent_id": "a_e2e",
            "adapter": "mcp",
            "action": {"type": "edit_file", "target": "/tmp/e2e_test.py"},
            "prediction": {
                "intent": "add docstring",
                "expected_changes": ["/tmp/e2e_test.py"],
                "success_criteria": ["FILE_EXISTS:/tmp/e2e_test.py"],
                "confidence": 0.85,
            },
        })
        assert before_resp.status_code == 200
        body = before_resp.json()
        assert body["decision"] == "allow"
        action_id = body["action_id"]

        # 2. Report outcome
        after_resp = client.post("/agent/action/after", json={
            "action_id": action_id,
            "outcome": {
                "success": True,
                "actual_changes": ["/tmp/e2e_test.py"],
                "observed_criteria_met": ["FILE_EXISTS:/tmp/e2e_test.py"],
            },
        })
        assert after_resp.status_code == 200
        after_body = after_resp.json()
        assert after_body["recorded"] is True
        assert after_body["prediction_match_score"] == 1.0
    finally:
        client.__exit__(None, None, None)


def test_e2e_rethink_then_allow():
    """Rethink on low confidence → resubmit with high confidence → allow."""
    pytest.importorskip("redis")
    try:
        from app.main import app
    except Exception as e:
        pytest.skip(f"App not importable in this environment: {e}")

    client = _make_client(app)
    try:
        # First call with low confidence on elevated action → rethink
        r1 = client.post("/agent/action/before", json={
            "session_id": "s_rethink",
            "agent_id": "a_rethink",
            "adapter": "mcp",
            "action": {"type": "delete", "target": "/tmp/important.txt"},
            "prediction": {
                "intent": "remove old artifact",
                "expected_changes": ["/tmp/important.txt"],
                "success_criteria": [],
                "confidence": 0.3,
            },
        })
        assert r1.status_code == 200
        assert r1.json()["decision"] == "rethink"

        # Resubmit with higher confidence → allow
        r2 = client.post("/agent/action/before", json={
            "session_id": "s_rethink",
            "agent_id": "a_rethink",
            "adapter": "mcp",
            "action": {"type": "delete", "target": "/tmp/important.txt"},
            "prediction": {
                "intent": "remove old artifact, confirmed safe",
                "expected_changes": ["/tmp/important.txt"],
                "success_criteria": [],
                "confidence": 0.9,
            },
        })
        assert r2.status_code == 200
        assert r2.json()["decision"] == "allow"
    finally:
        client.__exit__(None, None, None)


def test_e2e_shell_hook_never_blocks_on_missing_prediction():
    """shell-hook adapter receives advisory but is never blocked on prediction_required."""
    pytest.importorskip("redis")
    try:
        from app.main import app
    except Exception as e:
        pytest.skip(f"App not importable in this environment: {e}")

    client = _make_client(app)
    try:
        # Shell-hook adapter on an elevated action with no prediction → still allow
        r = client.post("/agent/action/before", json={
            "session_id": "s_shell",
            "agent_id": "a_shell",
            "adapter": "shell-hook",
            "action": {"type": "delete", "target": "/tmp/foo.txt"},
        })
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "allow"
        # Advisory recorded
        codes = [a["code"] for a in body["advisories"]]
        assert "prediction_required" in codes
    finally:
        client.__exit__(None, None, None)
