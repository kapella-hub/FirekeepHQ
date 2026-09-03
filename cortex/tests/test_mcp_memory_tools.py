"""Tests for new memory_recall params and memory_handoff MCP tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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


# ---------------------------------------------------------------------------
# memory_recall — new params
# ---------------------------------------------------------------------------


class TestMemoryRecallNewParams:
    @pytest.mark.asyncio
    async def test_includes_token_budget_in_request_body(self):
        """memory_recall sends token_budget in the POST body."""
        mock_resp = _mock_response(
            {"context_block": "synthesized context <!-- tokens:130/600 -->"}
        )
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ) as mock_post:
            from app.mcp_server import memory_recall
            await memory_recall(task="auth bugs", token_budget=400)

        call_json = mock_post.call_args[1]["json"]
        assert call_json.get("token_budget") == 400

    @pytest.mark.asyncio
    async def test_includes_format_in_request_body(self):
        """memory_recall sends format in the POST body."""
        mock_resp = _mock_response({"context_block": "raw list of memories"})
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ) as mock_post:
            from app.mcp_server import memory_recall
            await memory_recall(task="auth bugs", format="raw")

        call_json = mock_post.call_args[1]["json"]
        assert call_json.get("format") == "raw"

    @pytest.mark.asyncio
    async def test_includes_project_in_request_body_when_set(self):
        """memory_recall sends project in the POST body when provided."""
        mock_resp = _mock_response({"context_block": "project-scoped context"})
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ) as mock_post:
            from app.mcp_server import memory_recall
            await memory_recall(task="auth bugs", project="myapp")

        call_json = mock_post.call_args[1]["json"]
        assert call_json.get("project") == "myapp"

    @pytest.mark.asyncio
    async def test_omits_project_when_not_set(self):
        """memory_recall omits project key when not provided."""
        mock_resp = _mock_response({"context_block": "context"})
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ) as mock_post:
            from app.mcp_server import memory_recall
            await memory_recall(task="auth bugs")

        call_json = mock_post.call_args[1]["json"]
        assert "project" not in call_json

    @pytest.mark.asyncio
    async def test_all_new_params_together(self):
        """memory_recall sends token_budget, format, project together in the POST body."""
        mock_resp = _mock_response(
            {"context_block": "synthesized context <!-- tokens:130/600 -->"}
        )
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ) as mock_post:
            from app.mcp_server import memory_recall
            result = await memory_recall(
                task="auth bugs",
                top_k=3,
                namespace="default",
                agent_id="alice",
                session_id="sess-1",
                token_budget=400,
                format="raw",
                project="myapp",
            )

        call_json = mock_post.call_args[1]["json"]
        assert call_json.get("token_budget") == 400
        assert call_json.get("format") == "raw"
        assert call_json.get("project") == "myapp"
        assert "synthesized" in result

    @pytest.mark.asyncio
    async def test_default_top_k_is_3(self):
        """memory_recall default top_k changed to 3."""
        mock_resp = _mock_response({"context_block": "context"})
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ) as mock_post:
            from app.mcp_server import memory_recall
            await memory_recall(task="test")

        call_json = mock_post.call_args[1]["json"]
        assert call_json["top_k"] == 3


# ---------------------------------------------------------------------------
# memory_handoff
# ---------------------------------------------------------------------------


class TestMemoryHandoff:
    @pytest.mark.asyncio
    async def test_returns_string(self):
        """memory_handoff returns a non-empty string."""
        from unittest.mock import patch

        _mock_response([
            {
                "contributor_id": "alice",
                "memory_count": 5,
                "projects": ["myapp"],
                "last_active": "2026-05-23T10:00:00Z",
                "top_domain": "auth",
            }
        ])
        # Use a GET response object (request method must match)
        contrib_resp_get = httpx.Response(
            status_code=200,
            json=[
                {
                    "contributor_id": "alice",
                    "memory_count": 5,
                    "projects": ["myapp"],
                    "last_active": "2026-05-23T10:00:00Z",
                    "top_domain": "auth",
                }
            ],
            request=httpx.Request("GET", "http://test"),
        )
        recall_resp = _mock_response({"context_block": "Recent auth work by alice."})

        with (
            patch.object(
                httpx.AsyncClient, "get",
                new_callable=AsyncMock,
                return_value=contrib_resp_get,
            ),
            patch.object(
                httpx.AsyncClient, "post",
                new_callable=AsyncMock,
                # side_effect: first call = recall, second call (LLM) raises to trigger fallback
                side_effect=[recall_resp, Exception("no llm in test")],
            ),
        ):
            from app.mcp_server import memory_handoff
            result = await memory_handoff(project="myapp", since_days=7, agent_id="alice")

        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_fallback_when_llm_unavailable(self):
        """memory_handoff falls back to raw recall when LLM synthesis fails."""
        from unittest.mock import patch

        contrib_resp_get = httpx.Response(
            status_code=200,
            json=[],
            request=httpx.Request("GET", "http://test"),
        )
        # `sources` is required for this test to reach the LLM path at all:
        # a handoff with no contributors AND no recalled sources now
        # short-circuits to "nothing to hand off" rather than asking an LLM to
        # narrate an empty result (see TestMemoryHandoffEmptyProject). A real
        # /memory/recall never returns a populated context_block with zero
        # sources — the block is BUILT from them — so this makes the fixture
        # match the API, it does not weaken the test.
        recall_resp = _mock_response({
            "context_block": "Recent auth work by alice.",
            "sources": [{"store": "vector", "content": "auth work", "score": 0.9,
                         "metadata": {"id": "m1"}}],
        })

        with (
            patch.object(
                httpx.AsyncClient, "get",
                new_callable=AsyncMock,
                return_value=contrib_resp_get,
            ),
            patch.object(
                httpx.AsyncClient, "post",
                new_callable=AsyncMock,
                side_effect=[recall_resp, Exception("LLM unreachable")],
            ),
        ):
            from app.mcp_server import memory_handoff
            result = await memory_handoff(project="myapp", since_days=7)

        # Fallback includes the raw recall content
        assert "Recent auth work by alice." in result

    @pytest.mark.asyncio
    async def test_handles_contributor_endpoint_failure(self):
        """memory_handoff continues even when /memory/contributors is unavailable."""
        from unittest.mock import patch

        contrib_resp_get = httpx.Response(
            status_code=503,
            text="unavailable",
            request=httpx.Request("GET", "http://test"),
        )
        recall_resp = _mock_response({
            "context_block": "Some memories here.",
            "sources": [{"store": "vector", "content": "x", "score": 0.8,
                         "metadata": {"id": "m1"}}],
        })

        with (
            patch.object(
                httpx.AsyncClient, "get",
                new_callable=AsyncMock,
                return_value=contrib_resp_get,
            ),
            patch.object(
                httpx.AsyncClient, "post",
                new_callable=AsyncMock,
                side_effect=[recall_resp, Exception("no llm")],
            ),
        ):
            from app.mcp_server import memory_handoff
            result = await memory_handoff(project="myapp")

        assert isinstance(result, str)
        assert len(result) > 0


class TestMemoryHandoffEmptyProject:
    @pytest.mark.asyncio
    async def test_unknown_project_is_not_narrated(self):
        """A handoff for a project with nothing in it must SAY it is empty.

        Everything after retrieval hands the context to an LLM and asks for a
        handoff summary — and it will always write one, from whatever survived.
        Measured on the REST sibling of this flow: a handoff for
        `__no_such_project_xyz` returned HTTP 200 narrating ANOTHER project's
        work ("All 303 Karma tests pass...") as though it were that project's.
        Scoped retrieval stops the wrong memories arriving; this stops an
        EMPTY result being narrated anyway.
        """
        from unittest.mock import patch

        contrib_resp_get = httpx.Response(
            status_code=200, json=[], request=httpx.Request("GET", "http://test"),
        )
        recall_resp = _mock_response({"context_block": "", "sources": []})
        llm_calls = []

        # Patched onto the CLASS, so it is bound: `self` first, then the URL.
        async def _post(self, url, *a, **k):
            if "recall" in str(url):
                return recall_resp
            llm_calls.append(url)
            raise AssertionError("the LLM must not be asked to invent a handoff")

        with (
            patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock,
                         return_value=contrib_resp_get),
            patch.object(httpx.AsyncClient, "post", new=_post),
        ):
            from app.mcp_server import memory_handoff
            result = await memory_handoff(project="__no_such_project_xyz")

        assert "__no_such_project_xyz" in result
        assert "Nothing to hand off" in result
        assert llm_calls == []


@pytest.mark.asyncio
async def test_skill_recall_returns_string():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value=[
        {"trigger": "Fix Neo4j", "content": "trigger: Fix Neo4j\n---\n## Steps\n1. Check port.", "domain": "neo4j", "symptoms": "Error"}
    ])
    with patch("app.mcp_server._get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_get.return_value = mock_client
        from app.mcp_server import skill_recall
        result = await skill_recall(task="debug neo4j")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_skill_recall_sends_the_full_task_as_the_query():
    """`q` must carry the WHOLE task, not a five-word prefix.

    The old `" ".join(task.split()[:5])` existed to make a literal substring match
    against a trigger plausible; against a semantic query it just discards signal.
    Note `test_skill_recall_returns_string` above asserts only `isinstance(result, str)`
    — structurally incapable of catching this class of bug, which is why it shipped.
    """
    task = "the vector database keeps dropping writes after a rebuild"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value=[])
    with patch("app.mcp_server._get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_get.return_value = mock_client
        from app.mcp_server import skill_recall
        await skill_recall(task=task, top_k=3)

    path, kwargs = mock_client.get.call_args[0], mock_client.get.call_args[1]
    assert path[0] == "/skills"
    params = kwargs["params"]
    assert params["q"] == task, "the full task must be sent, not a truncated prefix"
    # Skill ladder PR1 (spec 2026-09-03): skill_recall now asks for `recallable`
    # (active + trial), not `active` alone — see test_mcp_skill_recall_trial.py
    # for the [TRIAL] labeling this alias enables.
    assert params["status"] == "recallable"
    assert params["limit"] == 3
    assert params["record_recall"] is True


@pytest.mark.asyncio
async def test_skill_create_returns_id():
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json = MagicMock(return_value={
        "id": "abc123", "trigger": "Fix X", "symptoms": "Error Y",
        "content": "...", "skill_status": "active",
    })
    with patch("app.mcp_server._get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_get.return_value = mock_client
        from app.mcp_server import skill_create
        result = await skill_create(trigger="Fix X", symptoms="Error Y", steps="1. Check.")
    assert "abc123" in result


@pytest.mark.asyncio
async def test_skill_list_returns_string():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value=[
        {"id": "s1", "trigger": "Fix Y", "skill_status": "active", "domain": "neo4j"}
    ])
    with patch("app.mcp_server._get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_get.return_value = mock_client
        from app.mcp_server import skill_list
        result = await skill_list()
    assert isinstance(result, str)
    assert "Fix Y" in result
