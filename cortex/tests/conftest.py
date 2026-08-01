"""Shared pytest fixtures for FirekeepCortex test suite."""

from __future__ import annotations

import os
import sys
import types

# Ensure shared modules (replay, auth, vault, corpus) are importable
# when running tests locally outside of Docker (mirrors Dockerfile COPY layout).
_FIREKEEP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _FIREKEEP_ROOT not in sys.path:
    sys.path.insert(0, _FIREKEEP_ROOT)
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeFastMCP:
    # **kwargs, not a fixed signature: the real FastMCP takes `instructions=` (the
    # MCP initialize handshake text) and `lifespan=`, and a double that enumerates
    # only the args it happens to know about turns every future constructor kwarg
    # into a collection ERROR rather than a test failure. That is what happened
    # when instructions= was added -- three test modules failed to import.
    def __init__(self, name: str, **_kwargs):
        self.name = name
        self.instructions = _kwargs.get("instructions")
        # Records the decorator kwargs per tool name (e.g. {"output_schema": None}),
        # since this double discards them otherwise -- a real FastMCP.get_tool()
        # is not available here, and a test asserting output_schema=None survived
        # on a `-> str` tool needs some way to see what the decorator was called
        # with.
        self.registered_tools: dict[str, dict] = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.registered_tools[fn.__name__] = kwargs
            return fn
        return decorator

    def custom_route(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    def run(self, *args, **kwargs):
        return None


def _fake_get_http_headers(*_args, **_kwargs) -> dict[str, str]:
    """Mirror the real get_http_headers: {} outside a request context."""
    return {}


if "fastmcp" not in sys.modules:
    fastmcp_module = types.ModuleType("fastmcp")
    fastmcp_module.FastMCP = _FakeFastMCP
    # Register the server.dependencies submodule chain so app.mcp_server's
    # `from fastmcp.server.dependencies import get_http_headers` takes the
    # production import path (no fallback, no collection-time ERROR log).
    fastmcp_server_module = types.ModuleType("fastmcp.server")
    fastmcp_deps_module = types.ModuleType("fastmcp.server.dependencies")
    fastmcp_deps_module.get_http_headers = _fake_get_http_headers
    fastmcp_server_module.dependencies = fastmcp_deps_module
    fastmcp_module.server = fastmcp_server_module
    sys.modules["fastmcp"] = fastmcp_module
    sys.modules["fastmcp.server"] = fastmcp_server_module
    sys.modules["fastmcp.server.dependencies"] = fastmcp_deps_module

from app.config import Settings
from app.engine.rag import RAGEngine
from app.main import app, get_graph, get_rag_engine, get_redis, get_vector


@pytest.fixture()
def test_settings() -> Settings:
    """Settings with test-friendly defaults (nothing connects to real services)."""
    return Settings(
        APP_NAME="FirekeepCortex-Test",
        DEBUG=True,
        NEO4J_URI="bolt://localhost:7687",
        NEO4J_USER="neo4j",
        NEO4J_PASSWORD="test",
        QDRANT_HOST="localhost",
        QDRANT_PORT=6333,
        QDRANT_COLLECTION="test_memory",
        REDIS_URL="redis://localhost:6379/15",
        REDIS_STREAM_KEY="firekeep:test_stream",
        REDIS_BATCH_SIZE=10,
        LLM_BASE_URL="http://localhost:11434/v1",
        LLM_MODEL="test-model",
        LLM_API_KEY="test-key",
        EMBEDDING_MODEL="test-embed",
        EMBEDDING_DIM=768,
    )


@pytest.fixture()
def mock_graph() -> AsyncMock:
    """AsyncMock standing in for Neo4jClient."""
    graph = AsyncMock()
    graph.connect = AsyncMock()
    graph.close = AsyncMock()
    graph.merge_action_log = AsyncMock(return_value="neo4j-element-id-1")
    graph.merge_knowledge_nodes = AsyncMock(return_value=3)
    graph.query_related = AsyncMock(return_value=[])
    graph.query_related_multihop = AsyncMock(return_value=[])
    graph.query_resolutions = AsyncMock(return_value=[])
    return graph


@pytest.fixture()
def mock_vector() -> AsyncMock:
    """AsyncMock standing in for VectorClient."""
    vector = AsyncMock()
    vector.initialize = AsyncMock()
    vector.close = AsyncMock()
    vector.upsert = AsyncMock(return_value="vec-uuid-1")
    vector.search = AsyncMock(return_value=[])
    vector._embed = AsyncMock(return_value=[0.1] * 768)
    return vector


@pytest.fixture()
def mock_redis() -> AsyncMock:
    """AsyncMock standing in for redis.asyncio.Redis."""
    r = AsyncMock()
    r.lpush = AsyncMock(return_value=1)
    r.rpop = AsyncMock(return_value=None)
    r.aclose = AsyncMock()

    # Pipeline support: pipeline() returns an object with lpush + execute
    mock_pipe = MagicMock()
    mock_pipe.lpush = MagicMock(return_value=mock_pipe)
    mock_pipe.execute = AsyncMock(return_value=[1])
    r.pipeline = MagicMock(return_value=mock_pipe)
    r._pipeline = mock_pipe  # exposed for test assertions

    return r


@pytest.fixture()
def test_client(mock_graph: AsyncMock, mock_vector: AsyncMock, mock_redis: AsyncMock) -> TestClient:
    """FastAPI TestClient with all external dependencies mocked via dependency overrides.

    We replace the real lifespan with a no-op so the TestClient does not
    attempt to connect to Neo4j, Qdrant, or Redis during startup.
    """

    async def _override_graph():
        return mock_graph

    async def _override_vector():
        return mock_vector

    async def _override_redis():
        return mock_redis

    # Swap out the lifespan to avoid real connections
    original_router_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _noop_lifespan(a: FastAPI):
        yield

    app.router.lifespan_context = _noop_lifespan

    rag_engine = RAGEngine(graph=mock_graph, vector=mock_vector)

    async def _override_rag_engine():
        return rag_engine

    app.dependency_overrides[get_graph] = _override_graph
    app.dependency_overrides[get_vector] = _override_vector
    app.dependency_overrides[get_redis] = _override_redis
    app.dependency_overrides[get_rag_engine] = _override_rag_engine

    from app import main as main_module
    main_module._health_cache = None
    main_module._health_cache_time = None

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client

    app.dependency_overrides.clear()
    app.router.lifespan_context = original_router_lifespan
