import pytest
from pydantic import ValidationError

from app.agent_gateway.models import (
    ActionBeforeRequest,
    ActionAfterRequest,
)


def test_action_before_request_minimal():
    req = ActionBeforeRequest(
        session_id="s1",
        agent_id="a1",
        adapter="mcp",
        action={"type": "edit_file", "target": "/tmp/foo.py"},
    )
    assert req.prediction is None
    assert req.adapter == "mcp"


def test_action_before_request_rejects_bad_action_type():
    with pytest.raises(ValidationError):
        ActionBeforeRequest(
            session_id="s1",
            agent_id="a1",
            adapter="mcp",
            action={"type": "read_file", "target": "/tmp/foo.py"},
        )


def test_action_before_request_rejects_bad_adapter():
    with pytest.raises(ValidationError):
        ActionBeforeRequest(
            session_id="s1",
            agent_id="a1",
            adapter="curl",
            action={"type": "edit_file", "target": "/tmp/foo.py"},
        )


def test_action_before_request_with_prediction():
    req = ActionBeforeRequest(
        session_id="s1",
        agent_id="a1",
        adapter="mcp",
        action={"type": "edit_file", "target": "/tmp/foo.py"},
        prediction={
            "intent": "add docstring",
            "expected_changes": ["/tmp/foo.py"],
            "success_criteria": ["FILE_EXISTS:/tmp/foo.py"],
            "confidence": 0.9,
        },
    )
    assert req.prediction.confidence == 0.9


def test_prediction_confidence_clamped_to_unit_interval():
    with pytest.raises(ValidationError):
        ActionBeforeRequest(
            session_id="s1",
            agent_id="a1",
            adapter="mcp",
            action={"type": "edit_file", "target": "/tmp/foo.py"},
            prediction={"intent": "x", "confidence": 1.5},
        )


def test_action_after_request_minimal():
    req = ActionAfterRequest(
        action_id="act_1",
        outcome={"success": True},
    )
    assert req.outcome.success is True
