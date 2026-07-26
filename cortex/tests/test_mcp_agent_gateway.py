"""Tests for action_before / action_after MCP tools.

These tools forward requests over HTTP to the cortex-api container rather than
calling the in-process service.  We mock httpx.AsyncClient.post to verify the
payload construction and adapter stamping.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest


def _mock_response(json_data: dict, status_code: int = 200) -> httpx.Response:
    """Create a mock httpx.Response with the given JSON body."""
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("POST", "http://test"),
    )


@pytest.fixture(autouse=True)
def _reset_client():
    """Reset the shared httpx client between tests."""
    import app.mcp_server as mod

    mod._client = None
    yield
    if mod._client and not mod._client.is_closed:
        mod._client = None


@pytest.mark.asyncio
async def test_action_before_mcp_tool_stamps_adapter_mcp():
    """action_before sends adapter='mcp' in the POST body."""
    mock_resp = _mock_response(
        {"decision": "allow", "action_id": "act_x", "tier": "auto", "auto_reconcile": False, "advisories": []}
    )
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
    ) as mock_post:
        from app.mcp_server import action_before

        fn = getattr(action_before, "fn", None) or action_before
        if hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__

        result = await fn(
            session_id="s1",
            agent_id="a1",
            action_type="edit_file",
            target="src/foo.py",
        )

    call_json = mock_post.call_args[1]["json"]
    assert call_json["adapter"] == "mcp"
    assert result["decision"] == "allow"


@pytest.mark.asyncio
async def test_action_before_mcp_tool_includes_prediction_when_provided():
    """action_before includes prediction block when intent/changes/criteria/confidence given."""
    mock_resp = _mock_response(
        {"decision": "allow", "action_id": "act_y", "tier": "lightweight", "auto_reconcile": False, "advisories": []}
    )
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
    ) as mock_post:
        from app.mcp_server import action_before

        fn = getattr(action_before, "fn", None) or action_before
        if hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__

        await fn(
            session_id="s1",
            agent_id="a1",
            action_type="edit_file",
            target="src/foo.py",
            intent="refactor auth module",
            expected_changes=["src/auth.py", "tests/test_auth.py"],
            success_criteria=["tests pass"],
            confidence=0.9,
        )

    call_json = mock_post.call_args[1]["json"]
    assert "prediction" in call_json
    assert call_json["prediction"]["intent"] == "refactor auth module"
    assert call_json["prediction"]["confidence"] == 0.9


@pytest.mark.asyncio
async def test_action_before_mcp_tool_no_prediction_when_no_extras():
    """action_before omits prediction block when no extras provided."""
    mock_resp = _mock_response(
        {"decision": "allow", "action_id": "act_z", "tier": "auto", "auto_reconcile": False, "advisories": []}
    )
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
    ) as mock_post:
        from app.mcp_server import action_before

        fn = getattr(action_before, "fn", None) or action_before
        if hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__

        result = await fn(
            session_id="s1",
            agent_id="a1",
            action_type="run_command",
            target="black src/",
        )

    call_json = mock_post.call_args[1]["json"]
    assert "prediction" not in call_json
    assert result["decision"] == "allow"


@pytest.mark.asyncio
async def test_action_after_mcp_tool_returns_score():
    """action_after forwards outcome and returns response from REST endpoint."""
    mock_resp = _mock_response(
        {"action_id": "act_x", "prediction_match_score": 0.75, "recorded": True}
    )
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
    ) as mock_post:
        from app.mcp_server import action_after

        fn = getattr(action_after, "fn", None) or action_after
        if hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__

        result = await fn(
            action_id="act_x",
            success=True,
            actual_changes=["src/foo.py"],
        )

    call_json = mock_post.call_args[1]["json"]
    assert call_json["action_id"] == "act_x"
    assert call_json["outcome"]["success"] is True
    assert result["prediction_match_score"] == 0.75


@pytest.mark.asyncio
async def test_action_after_mcp_tool_includes_deviation_notes_when_provided():
    """action_after includes deviation_notes when non-empty."""
    mock_resp = _mock_response(
        {"action_id": "act_d", "prediction_match_score": 0.3, "recorded": True}
    )
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
    ) as mock_post:
        from app.mcp_server import action_after

        fn = getattr(action_after, "fn", None) or action_after
        if hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__

        await fn(
            action_id="act_d",
            success=False,
            deviation_notes="unexpected import error",
        )

    call_json = mock_post.call_args[1]["json"]
    assert call_json["outcome"]["deviation_notes"] == "unexpected import error"
