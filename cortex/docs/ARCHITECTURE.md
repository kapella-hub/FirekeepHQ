# FirekeepCortex Architecture Specification

**Version**: 1.0.0 (original standalone design)
**Status**: Historical — foundational core still accurate; deployment details corrected inline
**Last Updated**: 2026-07-12

> **Status banner (2026-07-12):** This documents the **original standalone FirekeepCortex v1 core** — the RAG dual-retrieval merge (§9), the Domain/Concept/Action/Outcome/Resolution graph schema, the Sleep Cycle worker (§10), and the Redis stream protocol (§11) are all still real and foundational, and are documented in this depth nowhere else. **However, Cortex is now one service inside the Firekeep monorepo**, split into `cortex-api` / `cortex-mcp` / `cortex-worker` / `cortex-beat`, and has since grown an MCP tool layer, scope-based auth, client-authored skills, a decision board, replay, patterns, vault, corpus, knowledge/docs→skills, collectors, and an agent gateway — none of which existed in the 2026-03-02 v1 design. Deployment details below have been corrected to the running system where the numbers matter (embedding model/dim, ports); larger surfaces (the §13 compose topology, the full config, the §14 threat model, the request models) are annotated with a divergence note pointing at the source of truth rather than rewritten wholesale. For the current full surface see the root `CLAUDE.md` and `docs/DESIGN.md`; the authoritative config is `cortex/app/config.py`.

---

## Table of Contents

1. [Goals & Non-Goals](#1-goals--non-goals)
2. [System Overview](#2-system-overview)
3. [Configuration (`app/config.py`)](#3-configuration)
4. [Data Models (`app/models.py`)](#4-data-models)
5. [Exception Hierarchy & Error Handling](#5-exception-hierarchy--error-handling)
6. [FastAPI Application (`app/main.py`)](#6-fastapi-application)
7. [Neo4j Graph Client (`app/db/graph.py`)](#7-neo4j-graph-client)
8. [Qdrant Vector Client (`app/db/vector.py`)](#8-qdrant-vector-client)
9. [RAG Engine (`app/engine/rag.py`)](#9-rag-engine)
10. [Sleep Cycle Worker (`app/workers/sleep_cycle.py`)](#10-sleep-cycle-worker)
11. [Redis Queue Protocol](#11-redis-queue-protocol)
12. [Inter-Module Data Flow](#12-inter-module-data-flow)
13. [Docker Compose Topology](#13-docker-compose-topology)
14. [Security Considerations](#14-security-considerations)
15. [Observability Plan](#15-observability-plan)
16. [Acceptance Criteria](#16-acceptance-criteria)

---

## 1. Goals & Non-Goals

### Goals

- Provide a Memory-as-a-Service (MaaS) layer that any LLM agent can call via HTTP to store and retrieve experiential knowledge.
- Dual-retrieval architecture: semantic similarity (Qdrant) + structural/relational reasoning (Neo4j).
- High-volume event ingestion via Redis stream with backpressure.
- Background "Sleep Cycle" worker that consolidates raw events into structured knowledge using an LLM.
- Every module independently implementable and testable with clear contracts.

### Non-Goals

- FirekeepCortex does NOT run its own LLM inference server. It calls an external OpenAI-compatible API (Ollama, vLLM, OpenAI, etc.).
- No user-facing UI in v1. *(Superseded: Firekeep now ships a unified web dashboard on port 8040, and Cortex itself registers a `/dashboard` router.)*
- No multi-tenancy or authentication in v1. Single-tenant deployment assumed. *(Superseded: scope-based per-key auth is now implemented via a shared ASGI validator and is opt-in with `AUTH_ENABLED=true`; default off on the personal VPS.)*
- No horizontal scaling of Neo4j or Qdrant in v1. Single-node deployments.

### Assumptions

- Python 3.11+ (we target 3.11 for `TaskGroup` and `ExceptionGroup` support).
- Neo4j 5.x with APOC plugin available.
- Qdrant 1.7+ with gRPC or HTTP API.
- Redis 7+ with Streams support.
- LLM endpoint speaks the OpenAI `/v1/chat/completions` protocol.
- Embedding model produces 1024-dimensional vectors (configurable). The deployed model is `mxbai-embed-large` (1024-dim); v1 originally shipped `nomic-embed-text` (768-dim).

---

## 2. System Overview

```
                        +------------------+
                        |   LLM Agent(s)   |
                        +--------+---------+
                                 |
                    HTTP POST /memory/*
                                 |
                        +--------v---------+
                        |  FastAPI Server   |
                        |   (app/main.py)   |
                        +--+------+------+--+
                           |      |      |
              /recall      |      |      |  /stream
              /learn       |      |      |
                           v      v      v
                     +-----+  +---+--+  +------+
                     |Neo4j|  |Qdrant|  |Redis  |
                     |Graph|  |Vector|  |Stream |
                     +-----+  +------+  +---+---+
                                            |
                                    Celery consume
                                            |
                                   +--------v--------+
                                   | Sleep Cycle     |
                                   | Worker          |
                                   | (Celery task)   |
                                   +--------+--------+
                                            |
                                   LLM call + write
                                            |
                                    +-------v-------+
                                    | Neo4j + Qdrant|
                                    +---------------+
```

### Component Responsibilities

| Component | Responsibility |
|---|---|
| `app/main.py` | FastAPI app factory, lifespan management, route registration, DI wiring |
| `app/config.py` | Pydantic BaseSettings, all env vars, defaults, validation |
| `app/models.py` | Pydantic request/response models, domain types |
| `app/exceptions.py` | Custom exception hierarchy |
| `app/db/graph.py` | Neo4j async driver wrapper, Cypher queries, node/edge CRUD |
| `app/db/vector.py` | Qdrant async client wrapper, collection management, upsert/search |
| `app/engine/rag.py` | Dual-retrieval orchestrator, score merging, Markdown formatting |
| `app/workers/sleep_cycle.py` | Celery task, batch Redis consumption, LLM extraction, graph writes |

---

## 3. Configuration

**File**: `app/config.py`

Uses `pydantic-settings` v2 `BaseSettings` with `.env` file support.

> **Model/dim values corrected to the deployment; the field names and structure below are the v1 illustration.** The embedding model and dimension shown in this section reflect the running deployment (`mxbai-embed-large`, 1024-dim). The **field names and overall shape**, however, are v1 — the current `cortex/app/config.py` uses **UPPERCASE** env names and a much larger set (100+ settings for decay/GC, recall, skills, decision, knowledge, collectors, agent gateway, replay, auth, vault). Notable current values: the REST API is published externally on **8100** (container-internal 8000); the MCP server runs on **`MCP_PORT=8080`**; the generation model default is **`qwen3:4b`**; the vector size derives from **`EMBEDDING_DIM`** (there is no `QDRANT_VECTOR_SIZE`/`QDRANT_USE_GRPC`); and embed input is capped + shrink-to-fit at **`EMBED_MAX_CHARS=2000`**. Treat `cortex/app/config.py` as the source of truth.

```python
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- FastAPI ---
    app_name: str = "FirekeepCortex"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"   # DEBUG | INFO | WARNING | ERROR

    # --- Neo4j ---
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "firekeepcortex"
    neo4j_database: str = "neo4j"
    neo4j_max_connection_pool_size: int = 50

    # --- Qdrant ---
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_grpc_port: int = 6334
    qdrant_collection: str = "firekeep_memory"
    qdrant_vector_size: int = 1024
    qdrant_use_grpc: bool = True

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"
    redis_stream_key: str = "firekeep:event_stream"
    redis_consumer_group: str = "firekeep_workers"
    redis_consumer_name: str = "worker-1"
    redis_batch_size: int = 50
    redis_block_ms: int = 5000

    # --- LLM ---
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"   # Ollama ignores this but the OpenAI client requires it
    llm_model: str = "llama3.1:8b"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 2048
    llm_timeout: float = 120.0

    # --- Embeddings ---
    embedding_base_url: str = "http://localhost:11434/v1"
    embedding_api_key: str = "ollama"
    embedding_model: str = "mxbai-embed-large"
    embedding_dimensions: int = 1024

    # --- Sleep Cycle ---
    sleep_cycle_interval_seconds: int = 300      # 5 minutes
    sleep_cycle_batch_size: int = 50
    sleep_cycle_max_retries: int = 3

    # --- Celery ---
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"


def get_settings() -> Settings:
    """Factory function. Returns a cached Settings instance.

    Uses module-level singleton to avoid re-parsing env on every call.
    """
    return _settings


_settings = Settings()
```

### `.env.example`

```env
# FastAPI
APP_NAME=FirekeepCortex
DEBUG=false
HOST=0.0.0.0
PORT=8000
LOG_LEVEL=INFO

# Neo4j
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=firekeepcortex
NEO4J_DATABASE=neo4j

# Qdrant
QDRANT_HOST=qdrant
QDRANT_PORT=6333
QDRANT_GRPC_PORT=6334
QDRANT_COLLECTION=firekeep_memory
QDRANT_VECTOR_SIZE=1024

# Redis
REDIS_URL=redis://redis:6379/0
REDIS_STREAM_KEY=firekeep:event_stream
REDIS_CONSUMER_GROUP=firekeep_workers
REDIS_BATCH_SIZE=50

# LLM (Ollama)
LLM_BASE_URL=http://ollama:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=llama3.1:8b
LLM_TEMPERATURE=0.1

# Embeddings
EMBEDDING_BASE_URL=http://ollama:11434/v1
EMBEDDING_API_KEY=ollama
EMBEDDING_MODEL=mxbai-embed-large
EMBEDDING_DIMENSIONS=1024

# Sleep Cycle
SLEEP_CYCLE_INTERVAL_SECONDS=300
SLEEP_CYCLE_BATCH_SIZE=50

# Celery
CELERY_BROKER_URL=redis://redis:6379/1
CELERY_RESULT_BACKEND=redis://redis:6379/2
```

---

## 4. Data Models

**File**: `app/models.py`

> **Request-model shapes have changed** (the graph *labels* below — Domain/Concept/Action/Outcome/Resolution — are still used by the sleep-cycle extraction and remain accurate). In the current `cortex/app/models.py`: `ActionLog` fields are `action`, `outcome` (free-text `str`, no longer the SUCCESS/FAILURE/PARTIAL enum on input), `resolution`, `tags`, `domain`, `memory_type` (reference/procedural/episodic/transient), `namespace`, plus provenance (`memory_chain_id`, `created_by`, `source_session_id`, …) — there is no `outcome_detail` or `concepts` field. `ContextQuery` gained `namespace`, `include_archived`, `project`, `token_budget`, and `format`, and `top_k` now defaults to **5** (not 10).

### Request Models

```python
from pydantic import BaseModel, Field
from typing import Any
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4


# --- Enums ---

class OutcomeType(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"


class NodeLabel(str, Enum):
    DOMAIN = "Domain"
    CONCEPT = "Concept"
    ACTION = "Action"
    OUTCOME = "Outcome"
    RESOLUTION = "Resolution"
    EVENT_STREAM = "EventStream"


class EdgeType(str, Enum):
    RELATES_TO = "RELATES_TO"
    CAUSED = "CAUSED"
    RESOLVED_BY = "RESOLVED_BY"
    UTILIZES = "UTILIZES"


# --- Request Bodies ---

class ContextQuery(BaseModel):
    """POST /memory/recall - Retrieve contextual memories."""
    task: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Natural language description of the current task or question.",
    )
    tags: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Optional domain/topic tags to filter results.",
    )
    top_k: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of results to return from each retrieval source.",
    )


class ActionLog(BaseModel):
    """POST /memory/learn - Log an action, its outcome, and resolution."""
    domain: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="The domain this action belongs to (e.g. 'deployment', 'database').",
    )
    attempted_action: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="What was attempted.",
    )
    outcome: OutcomeType = Field(
        ...,
        description="Whether the action succeeded, failed, or partially succeeded.",
    )
    outcome_detail: str = Field(
        default="",
        max_length=5000,
        description="Details about what happened.",
    )
    resolution: str = Field(
        default="",
        max_length=5000,
        description="What was done to resolve the issue (if outcome was failure/partial).",
    )
    tags: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Tags for categorization.",
    )
    concepts: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Key concepts involved (e.g. 'docker', 'nginx', 'ssl').",
    )


class GenericEventIngest(BaseModel):
    """POST /memory/stream - High-volume event ingestion."""
    source: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Identifier for the event source (e.g. agent name, tool name).",
    )
    payload: dict[str, Any] = Field(
        ...,
        description="Arbitrary JSON payload. Will be serialized and stored.",
    )
    timestamp: datetime | None = Field(
        default=None,
        description="Event timestamp. Server will assign one if omitted.",
    )
```

### Response Models

```python
class MemoryResult(BaseModel):
    """A single memory item returned from recall."""
    id: str = Field(..., description="Unique identifier (UUID or Neo4j element ID).")
    source: str = Field(..., description="'vector', 'graph', or 'both' indicating retrieval source.")
    score: float = Field(..., ge=0.0, le=1.0, description="Normalized relevance score.")
    content: str = Field(..., description="The memory content text.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata.")


class RecallResponse(BaseModel):
    """POST /memory/recall response body."""
    query: str = Field(..., description="Echo of the original query task.")
    results: list[MemoryResult] = Field(default_factory=list)
    markdown: str = Field(..., description="Pre-formatted Markdown block for LLM injection.")
    total_results: int = Field(..., ge=0)
    retrieval_time_ms: float = Field(..., ge=0)


class LearnResponse(BaseModel):
    """POST /memory/learn response body."""
    action_id: str = Field(..., description="UUID of the created Action node.")
    nodes_created: int = Field(..., ge=0)
    edges_created: int = Field(..., ge=0)
    vectors_upserted: int = Field(..., ge=0)


class StreamResponse(BaseModel):
    """POST /memory/stream response body."""
    event_id: str = Field(..., description="Redis stream message ID.")
    queued: bool = Field(default=True)


class HealthResponse(BaseModel):
    """GET /health response body."""
    status: str = Field(default="ok")
    neo4j: str = Field(default="unknown")  # "connected" | "disconnected" | "error"
    qdrant: str = Field(default="unknown")
    redis: str = Field(default="unknown")


class ErrorResponse(BaseModel):
    """Standard error envelope."""
    error: str = Field(..., description="Error code (e.g. 'GRAPH_ERROR', 'VALIDATION_ERROR').")
    message: str = Field(..., description="Human-readable error message.")
    detail: dict[str, Any] | None = Field(default=None, description="Additional error context.")
```

### Internal Domain Types (not exposed via API)

```python
class GraphNode(BaseModel):
    """Internal representation of a node to be written to Neo4j."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    label: NodeLabel
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """Internal representation of an edge to be written to Neo4j."""
    source_id: str
    target_id: str
    edge_type: EdgeType
    properties: dict[str, Any] = Field(default_factory=dict)


class VectorRecord(BaseModel):
    """Internal representation of a vector to be upserted to Qdrant."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    text: str
    vector: list[float] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SleepCycleExtraction(BaseModel):
    """Output schema for the LLM extraction during sleep cycle."""
    domain: str
    concepts: list[str]
    action_summary: str
    outcome: OutcomeType
    resolution_summary: str
    confidence: float = Field(ge=0.0, le=1.0)
```

---

## 5. Exception Hierarchy & Error Handling

**File**: `app/exceptions.py`

```python
class FirekeepCortexError(Exception):
    """Base exception for all FirekeepCortex errors."""
    def __init__(self, message: str, detail: dict | None = None):
        self.message = message
        self.detail = detail
        super().__init__(message)


class GraphError(FirekeepCortexError):
    """Raised when a Neo4j operation fails."""
    pass


class VectorError(FirekeepCortexError):
    """Raised when a Qdrant operation fails."""
    pass


class EmbeddingError(FirekeepCortexError):
    """Raised when the embedding API call fails."""
    pass


class LLMError(FirekeepCortexError):
    """Raised when the LLM API call fails."""
    pass


class QueueError(FirekeepCortexError):
    """Raised when a Redis stream operation fails."""
    pass


class RecallError(FirekeepCortexError):
    """Raised when the RAG recall pipeline fails."""
    pass
```

### HTTP Error Mapping (registered in `app/main.py`)

| Exception Class | HTTP Status | Error Code |
|---|---|---|
| `GraphError` | 502 | `GRAPH_ERROR` |
| `VectorError` | 502 | `VECTOR_ERROR` |
| `EmbeddingError` | 502 | `EMBEDDING_ERROR` |
| `LLMError` | 502 | `LLM_ERROR` |
| `QueueError` | 502 | `QUEUE_ERROR` |
| `RecallError` | 500 | `RECALL_ERROR` |
| `ValidationError` (Pydantic) | 422 | `VALIDATION_ERROR` |
| Unhandled `Exception` | 500 | `INTERNAL_ERROR` |

Exception handlers return JSON `ErrorResponse` bodies. Stack traces are never leaked to the client.

```python
# In main.py -- exception handler pattern:

from fastapi import Request
from fastapi.responses import JSONResponse
from app.models import ErrorResponse
from app.exceptions import FirekeepCortexError

_EXCEPTION_MAP: dict[str, tuple[int, str]] = {
    "GraphError":     (502, "GRAPH_ERROR"),
    "VectorError":    (502, "VECTOR_ERROR"),
    "EmbeddingError": (502, "EMBEDDING_ERROR"),
    "LLMError":       (502, "LLM_ERROR"),
    "QueueError":     (502, "QUEUE_ERROR"),
    "RecallError":    (500, "RECALL_ERROR"),
}


async def firekeep_exception_handler(request: Request, exc: FirekeepCortexError) -> JSONResponse:
    cls_name = type(exc).__name__
    status_code, error_code = _EXCEPTION_MAP.get(cls_name, (500, "INTERNAL_ERROR"))
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            error=error_code,
            message=exc.message,
            detail=exc.detail,
        ).model_dump(),
    )
```

---

## 6. FastAPI Application

**File**: `app/main.py`

> **v1 route surface only.** The four endpoints documented in this section (`/health`, `/memory/recall`, `/memory/learn`, `/memory/stream`) are the original core. The current `cortex/app/main.py` additionally registers routers for dashboard, webhooks, stats, transfer, streaming, embedding_admin, lifecycle, ops, auth, vault, corpus, knowledge, decision, collectors, audit, evals, patterns, policy, agent_gateway, replay, skills, and briefing — and a **separate** `cortex-mcp` server (`cortex/app/mcp_server.py`, 27 MCP tools) exposes the memory/skill/corpus/vault/knowledge/replay/eval/gateway toolset. **Skills** are client-authored via the `skill_create` MCP tool (server-side session→skill synthesis is OFF by default, `SKILL_SYNTHESIS_ENABLED=false`). The **Decision Board** lives at Cortex `POST /decision/synthesize` (`DECISION_ENABLED=true`), fronted by the always-on client-stdio `firekeep-decision` server (`decision_board` / `decision_board_check`). See root `CLAUDE.md` / `docs/DESIGN.md`.

### App Factory & Lifespan

```python
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Depends, Request
from fastapi.responses import JSONResponse

from app.config import get_settings, Settings
from app.db.graph import GraphClient
from app.db.vector import VectorClient
from app.engine.rag import RAGEngine
from app.models import (
    ContextQuery, ActionLog, GenericEventIngest,
    RecallResponse, LearnResponse, StreamResponse,
    HealthResponse, ErrorResponse,
)
from app.exceptions import FirekeepCortexError

logger = logging.getLogger("firekeepcortex")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manages startup/shutdown of DB clients and engine."""
    settings = get_settings()

    # --- Startup ---
    graph = GraphClient(settings)
    await graph.connect()

    vector = VectorClient(settings)
    await vector.connect()
    await vector.ensure_collection()

    engine = RAGEngine(settings, graph, vector)

    # Store on app.state for dependency injection
    app.state.graph = graph
    app.state.vector = vector
    app.state.engine = engine
    app.state.settings = settings

    logger.info("FirekeepCortex started.")
    yield

    # --- Shutdown ---
    await graph.close()
    await vector.close()
    logger.info("FirekeepCortex stopped.")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_exception_handler(FirekeepCortexError, firekeep_exception_handler)
    return app

app = create_app()
```

### Dependency Injection

Dependencies are resolved via `app.state`, accessed through FastAPI's `Request` object.

```python
def get_graph(request: Request) -> GraphClient:
    return request.app.state.graph


def get_vector(request: Request) -> VectorClient:
    return request.app.state.vector


def get_engine(request: Request) -> RAGEngine:
    return request.app.state.engine


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings
```

### Route Signatures

```python
@app.get("/health", response_model=HealthResponse)
async def health_check(
    graph: GraphClient = Depends(get_graph),
    vector: VectorClient = Depends(get_vector),
    settings: Settings = Depends(get_app_settings),
) -> HealthResponse:
    """Returns connectivity status of all backing services."""
    ...


@app.post("/memory/recall", response_model=RecallResponse)
async def memory_recall(
    query: ContextQuery,
    engine: RAGEngine = Depends(get_engine),
) -> RecallResponse:
    """Dual-retrieval RAG query. Returns merged results as Markdown."""
    return await engine.recall(query)


@app.post("/memory/learn", response_model=LearnResponse)
async def memory_learn(
    action: ActionLog,
    engine: RAGEngine = Depends(get_engine),
) -> LearnResponse:
    """Logs an action/outcome/resolution to both graph and vector stores."""
    return await engine.learn(action)


@app.post("/memory/stream", response_model=StreamResponse)
async def memory_stream(
    event: GenericEventIngest,
    engine: RAGEngine = Depends(get_engine),
) -> StreamResponse:
    """Pushes an event to the Redis stream for async processing."""
    return await engine.ingest_event(event)
```

---

## 7. Neo4j Graph Client

**File**: `app/db/graph.py`

### Class Interface

```python
import logging
from typing import Any
from neo4j import AsyncGraphDatabase, AsyncDriver, AsyncSession
from app.config import Settings
from app.exceptions import GraphError
from app.models import GraphNode, GraphEdge, NodeLabel, EdgeType

logger = logging.getLogger("firekeepcortex.graph")


class GraphClient:
    """Async Neo4j driver wrapper. All Cypher queries are centralized here."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._driver: AsyncDriver | None = None

    async def connect(self) -> None:
        """Initialize the Neo4j async driver, verify connectivity, and create indexes."""
        try:
            self._driver = AsyncGraphDatabase.driver(
                self._settings.neo4j_uri,
                auth=(self._settings.neo4j_user, self._settings.neo4j_password),
                max_connection_pool_size=self._settings.neo4j_max_connection_pool_size,
            )
            await self._driver.verify_connectivity()
            await self._ensure_indexes()
            logger.info("Neo4j connected at %s", self._settings.neo4j_uri)
        except Exception as e:
            raise GraphError(f"Failed to connect to Neo4j: {e}") from e

    async def close(self) -> None:
        """Close the Neo4j driver."""
        if self._driver:
            await self._driver.close()
            logger.info("Neo4j connection closed.")

    def _session(self) -> AsyncSession:
        """Create a new async session."""
        if not self._driver:
            raise GraphError("Neo4j driver not initialized. Call connect() first.")
        return self._driver.session(database=self._settings.neo4j_database)

    async def _ensure_indexes(self) -> None:
        """Create indexes on node id properties for fast MERGE lookups."""
        index_statements = [
            "CREATE INDEX domain_id IF NOT EXISTS FOR (n:Domain) ON (n.id)",
            "CREATE INDEX concept_id IF NOT EXISTS FOR (n:Concept) ON (n.id)",
            "CREATE INDEX action_id IF NOT EXISTS FOR (n:Action) ON (n.id)",
            "CREATE INDEX outcome_id IF NOT EXISTS FOR (n:Outcome) ON (n.id)",
            "CREATE INDEX resolution_id IF NOT EXISTS FOR (n:Resolution) ON (n.id)",
            "CREATE INDEX event_stream_id IF NOT EXISTS FOR (n:EventStream) ON (n.id)",
        ]
        async with self._session() as session:
            for stmt in index_statements:
                await session.run(stmt)

    async def is_connected(self) -> bool:
        """Health check. Returns True if Neo4j is reachable."""
        try:
            async with self._session() as session:
                await session.run("RETURN 1")
            return True
        except Exception:
            return False
```

### Node & Edge Operations

```python
    async def merge_node(self, node: GraphNode) -> str:
        """
        Create or update a node. Returns the node's `id` property.
        Uses MERGE on (label, id) to ensure idempotency.
        """
        query = (
            f"MERGE (n:{node.label.value} {{id: $id}}) "
            f"SET n += $props, n.updated_at = datetime() "
            f"ON CREATE SET n.created_at = datetime() "
            f"RETURN n.id AS id"
        )
        props = {**node.properties, "id": node.id}
        try:
            async with self._session() as session:
                result = await session.run(query, {"id": node.id, "props": props})
                record = await result.single()
                return record["id"]
        except Exception as e:
            raise GraphError(f"merge_node failed for {node.label.value}: {e}") from e

    async def merge_edge(self, edge: GraphEdge) -> None:
        """
        Create or update an edge between two nodes (matched by `id` property).
        Source and target nodes must already exist.
        """
        query = (
            f"MATCH (a {{id: $source_id}}) "
            f"MATCH (b {{id: $target_id}}) "
            f"MERGE (a)-[r:{edge.edge_type.value}]->(b) "
            f"SET r += $props, r.updated_at = datetime() "
            f"ON CREATE SET r.created_at = datetime() "
        )
        try:
            async with self._session() as session:
                await session.run(
                    query,
                    {
                        "source_id": edge.source_id,
                        "target_id": edge.target_id,
                        "props": edge.properties,
                    },
                )
        except Exception as e:
            raise GraphError(
                f"merge_edge failed for {edge.edge_type.value} "
                f"({edge.source_id} -> {edge.target_id}): {e}"
            ) from e
```

### Query Operations

```python
    async def find_related_actions(
        self,
        tags: list[str],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Find Action nodes related to given tags/concepts via graph traversal.
        Traverses: (Concept|Domain)-[:RELATES_TO|UTILIZES*1..3]-(Action)
        Returns action properties + connected outcomes/resolutions.
        """
        query = """
        MATCH (entry)
        WHERE (entry:Domain OR entry:Concept)
          AND toLower(entry.name) IN $tags
        MATCH path = (entry)-[:RELATES_TO|UTILIZES*1..3]-(a:Action)
        OPTIONAL MATCH (a)-[:CAUSED]->(o:Outcome)
        OPTIONAL MATCH (o)-[:RESOLVED_BY]->(r:Resolution)
        RETURN DISTINCT a.id AS id,
               a.description AS description,
               a.domain AS domain,
               collect(DISTINCT o.detail) AS outcomes,
               collect(DISTINCT r.description) AS resolutions,
               length(path) AS depth
        ORDER BY depth ASC
        LIMIT $limit
        """
        try:
            async with self._session() as session:
                result = await session.run(
                    query,
                    {"tags": [t.lower() for t in tags], "limit": limit},
                )
                return [dict(record) async for record in result]
        except Exception as e:
            raise GraphError(f"find_related_actions failed: {e}") from e

    async def find_by_text_search(
        self,
        search_text: str,
        labels: list[NodeLabel] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Full-text search across node properties using Neo4j CONTAINS.
        For v1 we use simple CONTAINS matching. A full-text index can be
        added later for better performance.
        """
        label_filter = ""
        if labels:
            label_strs = " OR ".join(f"n:{l.value}" for l in labels)
            label_filter = f"AND ({label_strs})"

        query = f"""
        MATCH (n)
        WHERE (n.description CONTAINS $text OR n.name CONTAINS $text)
          {label_filter}
        RETURN n.id AS id,
               labels(n)[0] AS label,
               n.description AS description,
               n.name AS name,
               n.domain AS domain
        LIMIT $limit
        """
        try:
            async with self._session() as session:
                result = await session.run(
                    query,
                    {"text": search_text, "limit": limit},
                )
                return [dict(record) async for record in result]
        except Exception as e:
            raise GraphError(f"find_by_text_search failed: {e}") from e
```

### Compound Action Logging

```python
    async def log_action(
        self,
        domain: str,
        action_description: str,
        outcome_type: str,
        outcome_detail: str,
        resolution: str,
        concepts: list[str],
        tags: list[str],
    ) -> dict[str, Any]:
        """
        Creates the full Action -> Outcome -> Resolution subgraph
        with Domain and Concept nodes linked.

        Returns: {"action_id": str, "nodes_created": int, "edges_created": int}
        """
        from uuid import uuid4

        action_id = str(uuid4())
        outcome_id = str(uuid4())
        resolution_id = str(uuid4())
        domain_id = f"domain:{domain.lower()}"

        nodes_created = 0
        edges_created = 0

        # 1. Domain node
        await self.merge_node(GraphNode(
            id=domain_id, label=NodeLabel.DOMAIN,
            properties={"name": domain},
        ))
        nodes_created += 1

        # 2. Action node
        await self.merge_node(GraphNode(
            id=action_id, label=NodeLabel.ACTION,
            properties={"description": action_description, "domain": domain, "tags": tags},
        ))
        nodes_created += 1

        # 3. Domain -> Action
        await self.merge_edge(GraphEdge(
            source_id=domain_id, target_id=action_id, edge_type=EdgeType.RELATES_TO,
        ))
        edges_created += 1

        # 4. Outcome node
        await self.merge_node(GraphNode(
            id=outcome_id, label=NodeLabel.OUTCOME,
            properties={"type": outcome_type, "detail": outcome_detail},
        ))
        nodes_created += 1

        # 5. Action -[:CAUSED]-> Outcome
        await self.merge_edge(GraphEdge(
            source_id=action_id, target_id=outcome_id, edge_type=EdgeType.CAUSED,
        ))
        edges_created += 1

        # 6. Resolution node (only if provided)
        if resolution:
            await self.merge_node(GraphNode(
                id=resolution_id, label=NodeLabel.RESOLUTION,
                properties={"description": resolution},
            ))
            nodes_created += 1
            await self.merge_edge(GraphEdge(
                source_id=outcome_id, target_id=resolution_id, edge_type=EdgeType.RESOLVED_BY,
            ))
            edges_created += 1

        # 7. Concept nodes + UTILIZES edges
        for concept_name in concepts:
            concept_id = f"concept:{concept_name.lower()}"
            await self.merge_node(GraphNode(
                id=concept_id, label=NodeLabel.CONCEPT,
                properties={"name": concept_name},
            ))
            nodes_created += 1
            await self.merge_edge(GraphEdge(
                source_id=action_id, target_id=concept_id, edge_type=EdgeType.UTILIZES,
            ))
            edges_created += 1

        return {
            "action_id": action_id,
            "nodes_created": nodes_created,
            "edges_created": edges_created,
        }
```

### Cypher Reference -- All Queries

| Operation | Cypher Pattern |
|---|---|
| Merge node | `MERGE (n:<Label> {id: $id}) SET n += $props, n.updated_at = datetime() ON CREATE SET n.created_at = datetime()` |
| Merge edge | `MATCH (a {id: $source_id}) MATCH (b {id: $target_id}) MERGE (a)-[r:<TYPE>]->(b) SET r += $props, r.updated_at = datetime() ON CREATE SET r.created_at = datetime()` |
| Find related | `MATCH (entry)-[:RELATES_TO\|UTILIZES*1..3]-(a:Action) ...` (see `find_related_actions`) |
| Text search | `MATCH (n) WHERE n.description CONTAINS $text ...` |
| Health check | `RETURN 1` |

### Neo4j Indexes (created automatically in `connect()`)

```cypher
CREATE INDEX domain_id IF NOT EXISTS FOR (n:Domain) ON (n.id);
CREATE INDEX concept_id IF NOT EXISTS FOR (n:Concept) ON (n.id);
CREATE INDEX action_id IF NOT EXISTS FOR (n:Action) ON (n.id);
CREATE INDEX outcome_id IF NOT EXISTS FOR (n:Outcome) ON (n.id);
CREATE INDEX resolution_id IF NOT EXISTS FOR (n:Resolution) ON (n.id);
CREATE INDEX event_stream_id IF NOT EXISTS FOR (n:EventStream) ON (n.id);
```

---

## 8. Qdrant Vector Client

**File**: `app/db/vector.py`

### Collection Schema

| Property | Value |
|---|---|
| Collection name | Configured via `QDRANT_COLLECTION` (default: `firekeep_memory`) |
| Vector size | **1024** (deployed `mxbai-embed-large`; derives from `EMBEDDING_DIM`). Originally 768 in v1. |
| Distance metric | **Cosine** |
| On-disk | `true` (optimized for larger-than-RAM datasets) |

### Payload Schema

Every point in the Qdrant collection carries this payload:

```json
{
  "text": "The original text content that was embedded",
  "source_type": "action | event | resolution",
  "domain": "deployment",
  "tags": ["docker", "nginx"],
  "concepts": ["ssl", "reverse-proxy"],
  "outcome_type": "success | failure | partial",
  "created_at": "2026-03-02T10:00:00Z",
  "graph_node_id": "uuid-of-linked-neo4j-node"
}
```

### Class Interface

```python
import logging
from typing import Any
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct,
    Filter, FieldCondition, MatchAny, MatchValue,
)
from app.config import Settings
from app.exceptions import VectorError
from app.models import VectorRecord

logger = logging.getLogger("firekeepcortex.vector")


class VectorClient:
    """Async Qdrant client wrapper. Manages collection and point operations."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncQdrantClient | None = None
        self._collection = settings.qdrant_collection

    async def connect(self) -> None:
        """Initialize the Qdrant async client."""
        try:
            self._client = AsyncQdrantClient(
                host=self._settings.qdrant_host,
                port=self._settings.qdrant_port,
                grpc_port=self._settings.qdrant_grpc_port,
                prefer_grpc=self._settings.qdrant_use_grpc,
            )
            logger.info("Qdrant client initialized at %s:%d",
                        self._settings.qdrant_host, self._settings.qdrant_port)
        except Exception as e:
            raise VectorError(f"Failed to initialize Qdrant client: {e}") from e

    async def close(self) -> None:
        """Close the Qdrant client."""
        if self._client:
            await self._client.close()
            logger.info("Qdrant client closed.")

    async def is_connected(self) -> bool:
        """Health check. Returns True if Qdrant is reachable."""
        try:
            await self._client.get_collections()
            return True
        except Exception:
            return False

    async def ensure_collection(self) -> None:
        """Create the collection if it does not exist."""
        try:
            collections = await self._client.get_collections()
            names = [c.name for c in collections.collections]
            if self._collection not in names:
                await self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(
                        size=self._settings.qdrant_vector_size,
                        distance=Distance.COSINE,
                        on_disk=True,
                    ),
                )
                logger.info("Created Qdrant collection '%s'", self._collection)
            else:
                logger.info("Qdrant collection '%s' already exists.", self._collection)
        except Exception as e:
            raise VectorError(f"ensure_collection failed: {e}") from e

    async def upsert(self, records: list[VectorRecord]) -> int:
        """
        Upsert one or more vector records.
        Returns the number of points upserted.
        """
        if not records:
            return 0
        points = [
            PointStruct(
                id=rec.id,
                vector=rec.vector,
                payload={"text": rec.text, **rec.metadata},
            )
            for rec in records
        ]
        try:
            await self._client.upsert(
                collection_name=self._collection,
                points=points,
            )
            return len(points)
        except Exception as e:
            raise VectorError(f"upsert failed: {e}") from e

    async def search(
        self,
        query_vector: list[float],
        top_k: int = 10,
        tags: list[str] | None = None,
        domain: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search for similar vectors. Optionally filter by tags and/or domain.
        Returns list of dicts: {id, score, text, metadata}.
        """
        filters = []
        if tags:
            filters.append(FieldCondition(key="tags", match=MatchAny(any=tags)))
        if domain:
            filters.append(FieldCondition(key="domain", match=MatchValue(value=domain)))

        search_filter = Filter(must=filters) if filters else None

        try:
            results = await self._client.search(
                collection_name=self._collection,
                query_vector=query_vector,
                limit=top_k,
                query_filter=search_filter,
                with_payload=True,
            )
            return [
                {
                    "id": str(hit.id),
                    "score": hit.score,
                    "text": hit.payload.get("text", ""),
                    "metadata": {k: v for k, v in hit.payload.items() if k != "text"},
                }
                for hit in results
            ]
        except Exception as e:
            raise VectorError(f"search failed: {e}") from e
```

---

## 9. RAG Engine

**File**: `app/engine/rag.py`

The RAG engine orchestrates the dual-retrieval pipeline (vector + graph), merges results, and formats the output for LLM consumption.

### Scoring Algorithm

The dual-retrieval merge works as follows:

1. **Vector search** returns `top_k` results with cosine similarity scores in [0, 1].
2. **Graph search** returns related actions based on tag/concept traversal. Graph results are scored based on traversal depth:
   - depth 1: score = 0.95
   - depth 2: score = 0.80
   - depth 3: score = 0.65
3. **Deduplication**: Results from both sources are deduplicated by `graph_node_id` (vector payloads carry the linked graph node ID). When a result appears in both sources, scores are combined: `combined = 0.6 * vector_score + 0.4 * graph_score`.
4. **Re-ranking**: All results are sorted by final score descending, then truncated to `top_k`.
5. **Markdown generation**: Top results are formatted into a Markdown block suitable for injection into an LLM system prompt.

### Class Interface

```python
import logging
import time
from typing import Any
from openai import AsyncOpenAI
import redis.asyncio as redis

from app.config import Settings
from app.db.graph import GraphClient
from app.db.vector import VectorClient
from app.models import (
    ContextQuery, ActionLog, GenericEventIngest,
    RecallResponse, LearnResponse, StreamResponse,
    MemoryResult, VectorRecord,
)
from app.exceptions import EmbeddingError, RecallError, QueueError

logger = logging.getLogger("firekeepcortex.rag")


class RAGEngine:
    """
    Dual-retrieval Recall-Augmented Generation engine.
    Orchestrates vector + graph search, score merging, and Markdown formatting.
    """

    def __init__(
        self,
        settings: Settings,
        graph: GraphClient,
        vector: VectorClient,
    ) -> None:
        self._settings = settings
        self._graph = graph
        self._vector = vector
        self._embed_client = AsyncOpenAI(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
        )
        self._redis: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis:
        """Lazy-initialize Redis connection."""
        if self._redis is None:
            self._redis = redis.from_url(
                self._settings.redis_url,
                decode_responses=True,
            )
        return self._redis
```

### Embedding Methods

```python
    async def embed_text(self, text: str) -> list[float]:
        """
        Generate an embedding vector for the given text.
        Uses the OpenAI-compatible /v1/embeddings endpoint.
        Returns a list of floats with length == settings.embedding_dimensions (1024).

        NOTE: In the current system, input is first capped and shrink-to-fit
        truncated at EMBED_MAX_CHARS=2000 (mxbai-embed-large's ~512-token context)
        so long memories still embed; the v1 pass-through shown here does not truncate.
        """
        try:
            response = await self._embed_client.embeddings.create(
                model=self._settings.embedding_model,
                input=text,
            )
            return response.data[0].embedding
        except Exception as e:
            raise EmbeddingError(f"Embedding failed: {e}") from e

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Batch embed multiple texts. Returns list of vectors."""
        try:
            response = await self._embed_client.embeddings.create(
                model=self._settings.embedding_model,
                input=texts,
            )
            return [item.embedding for item in response.data]
        except Exception as e:
            raise EmbeddingError(f"Batch embedding failed: {e}") from e
```

### Recall (Dual Retrieval)

```python
    async def recall(self, query: ContextQuery) -> RecallResponse:
        """
        Execute dual-retrieval RAG pipeline:
        1. Embed query text
        2. Vector search (Qdrant)
        3. Graph search (Neo4j) -- tag traversal + text search
        4. Merge and re-rank
        5. Format as Markdown
        """
        start_time = time.monotonic()

        try:
            # Step 1: Embed
            query_vector = await self.embed_text(query.task)

            # Step 2: Vector search
            vector_results = await self._vector.search(
                query_vector=query_vector,
                top_k=query.top_k,
                tags=query.tags if query.tags else None,
            )

            # Step 3: Graph search
            graph_results = []
            if query.tags:
                graph_results = await self._graph.find_related_actions(
                    tags=query.tags, limit=query.top_k,
                )

            graph_text_results = await self._graph.find_by_text_search(
                search_text=query.task[:200],
                limit=query.top_k,
            )

            # Step 4: Merge and re-rank
            merged = self._merge_results(
                vector_results, graph_results, graph_text_results, query.top_k,
            )

            # Step 5: Format as Markdown
            markdown = self._format_markdown(merged, query.task)

            elapsed_ms = (time.monotonic() - start_time) * 1000
            return RecallResponse(
                query=query.task,
                results=merged,
                markdown=markdown,
                total_results=len(merged),
                retrieval_time_ms=round(elapsed_ms, 2),
            )
        except EmbeddingError:
            raise
        except Exception as e:
            raise RecallError(f"Recall pipeline failed: {e}") from e
```

### Merge Algorithm

```python
    def _merge_results(
        self,
        vector_results: list[dict[str, Any]],
        graph_tag_results: list[dict[str, Any]],
        graph_text_results: list[dict[str, Any]],
        top_k: int,
    ) -> list[MemoryResult]:
        """
        Merge results from vector and graph searches.

        Scoring rules:
        - Vector results: cosine similarity as-is (already [0,1])
        - Graph tag results: scored by traversal depth (1->0.95, 2->0.80, 3->0.65)
        - Graph text results: flat score of 0.70 (CONTAINS is a weak signal)
        - Duplicates (same graph_node_id): combined = 0.6*vector + 0.4*graph
        """
        DEPTH_SCORES = {1: 0.95, 2: 0.80, 3: 0.65}
        GRAPH_TEXT_SCORE = 0.70
        VECTOR_WEIGHT = 0.6
        GRAPH_WEIGHT = 0.4

        merged: dict[str, MemoryResult] = {}

        # Vector results
        for vr in vector_results:
            rid = vr["id"]
            graph_id = vr.get("metadata", {}).get("graph_node_id", "")
            key = graph_id if graph_id else rid
            merged[key] = MemoryResult(
                id=rid, source="vector", score=vr["score"],
                content=vr.get("text", ""), metadata=vr.get("metadata", {}),
            )

        # Graph tag results
        for gr in graph_tag_results:
            rid = gr.get("id", "")
            depth = gr.get("depth", 3)
            graph_score = DEPTH_SCORES.get(depth, 0.50)

            content_parts = []
            if gr.get("description"):
                content_parts.append(gr["description"])
            if gr.get("outcomes"):
                content_parts.append(f"Outcomes: {', '.join(filter(None, gr['outcomes']))}")
            if gr.get("resolutions"):
                content_parts.append(f"Resolutions: {', '.join(filter(None, gr['resolutions']))}")
            content = "\n".join(content_parts)

            if rid in merged:
                existing = merged[rid]
                combined = VECTOR_WEIGHT * existing.score + GRAPH_WEIGHT * graph_score
                existing.score = min(combined, 1.0)
                existing.source = "both"
                if content and not existing.content:
                    existing.content = content
            else:
                merged[rid] = MemoryResult(
                    id=rid, source="graph", score=graph_score,
                    content=content, metadata={"domain": gr.get("domain", "")},
                )

        # Graph text results
        for gtr in graph_text_results:
            rid = gtr.get("id", "")
            if rid in merged:
                existing = merged[rid]
                existing.score = min(existing.score + 0.05, 1.0)
            else:
                content = gtr.get("description") or gtr.get("name", "")
                merged[rid] = MemoryResult(
                    id=rid, source="graph", score=GRAPH_TEXT_SCORE,
                    content=content,
                    metadata={"label": gtr.get("label", ""), "domain": gtr.get("domain", "")},
                )

        results = sorted(merged.values(), key=lambda r: r.score, reverse=True)
        return results[:top_k]
```

### Markdown Formatting

```python
    def _format_markdown(self, results: list[MemoryResult], query: str) -> str:
        """
        Format retrieval results as Markdown for LLM injection.

        Output format:
            ## Relevant Memories for: <query>
            ### 1. [source: vector | score: 0.92]
            <content>
            _Meta: domain: deployment, ..._
        """
        if not results:
            return f"## Relevant Memories for: {query}\n\n_No relevant memories found._\n"

        lines = [f"## Relevant Memories for: {query}\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"### {i}. [source: {r.source} | score: {r.score:.2f}]")
            lines.append(r.content)
            if r.metadata:
                meta_str = ", ".join(f"{k}: {v}" for k, v in r.metadata.items() if v)
                if meta_str:
                    lines.append(f"_Meta: {meta_str}_")
            lines.append("")
        return "\n".join(lines)
```

### Learn (Action Logging)

```python
    async def learn(self, action: ActionLog) -> LearnResponse:
        """
        Log an action to both graph and vector stores.
        1. Write action subgraph to Neo4j
        2. Embed the action text
        3. Upsert embedding to Qdrant with metadata
        """
        graph_result = await self._graph.log_action(
            domain=action.domain,
            action_description=action.attempted_action,
            outcome_type=action.outcome.value,
            outcome_detail=action.outcome_detail,
            resolution=action.resolution,
            concepts=action.concepts,
            tags=action.tags,
        )

        text_to_embed = (
            f"Action: {action.attempted_action}\n"
            f"Outcome ({action.outcome.value}): {action.outcome_detail}\n"
            f"Resolution: {action.resolution}"
        ).strip()

        embedding = await self.embed_text(text_to_embed)

        record = VectorRecord(
            id=graph_result["action_id"],
            text=text_to_embed,
            vector=embedding,
            metadata={
                "source_type": "action",
                "domain": action.domain,
                "tags": action.tags,
                "concepts": action.concepts,
                "outcome_type": action.outcome.value,
                "graph_node_id": graph_result["action_id"],
            },
        )
        vectors_upserted = await self._vector.upsert([record])

        return LearnResponse(
            action_id=graph_result["action_id"],
            nodes_created=graph_result["nodes_created"],
            edges_created=graph_result["edges_created"],
            vectors_upserted=vectors_upserted,
        )
```

### Stream (Event Ingestion)

```python
    async def ingest_event(self, event: GenericEventIngest) -> StreamResponse:
        """Push an event to the Redis stream for async processing."""
        import json
        from datetime import datetime, timezone

        r = await self._get_redis()
        timestamp = event.timestamp or datetime.now(timezone.utc)
        stream_data = {
            "source": event.source,
            "payload": json.dumps(event.payload),
            "timestamp": timestamp.isoformat(),
        }
        try:
            event_id = await r.xadd(self._settings.redis_stream_key, stream_data)
            return StreamResponse(event_id=event_id, queued=True)
        except Exception as e:
            raise QueueError(f"Failed to push event to Redis stream: {e}") from e
```

---

## 10. Sleep Cycle Worker

**File**: `app/workers/sleep_cycle.py`

The Sleep Cycle is a Celery Beat periodic task that:
1. Reads a batch of unprocessed events from the Redis stream.
2. Sends them to the LLM for structured extraction.
3. Writes the extracted knowledge to Neo4j and Qdrant.

### Celery App Setup

```python
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from celery import Celery
from openai import OpenAI  # Sync client for Celery workers
import redis as sync_redis

from app.config import get_settings
from app.models import SleepCycleExtraction, NodeLabel, EdgeType, OutcomeType
from app.exceptions import LLMError

logger = logging.getLogger("firekeepcortex.sleep_cycle")

settings = get_settings()

celery_app = Celery(
    "firekeepcortex",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.beat_schedule = {
    "sleep-cycle": {
        "task": "app.workers.sleep_cycle.run_sleep_cycle",
        "schedule": settings.sleep_cycle_interval_seconds,
    },
}
celery_app.conf.timezone = "UTC"
```

### LLM Extraction Prompt

```python
EXTRACTION_SYSTEM_PROMPT = """You are a knowledge extraction system. Given a batch of raw events \
from an AI agent, extract structured knowledge entries.

For each meaningful event or group of related events, produce a JSON object with these fields:
- "domain": The knowledge domain (e.g., "deployment", "debugging", "api-integration")
- "concepts": List of key concepts involved (e.g., ["docker", "nginx", "ssl"])
- "action_summary": What was attempted (1-2 sentences)
- "outcome": One of "success", "failure", or "partial"
- "resolution_summary": What resolved the issue, or what was learned (1-2 sentences). \
Empty string if not applicable.
- "confidence": Float 0-1 indicating how confident you are in this extraction

Return a JSON array of these objects. If no meaningful knowledge can be extracted, return [].
IMPORTANT: Return ONLY the JSON array, no additional text or markdown formatting."""

EXTRACTION_USER_TEMPLATE = """Extract structured knowledge from these raw events:

{events_text}"""
```

### Task Implementation

```python
@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="app.workers.sleep_cycle.run_sleep_cycle",
)
def run_sleep_cycle(self) -> dict[str, Any]:
    """
    Celery task: consume events from Redis stream, extract knowledge via LLM,
    and write to Neo4j + Qdrant.

    Returns summary dict with counts and errors.
    """
    summary = {
        "events_consumed": 0,
        "extractions": 0,
        "nodes_created": 0,
        "edges_created": 0,
        "vectors_upserted": 0,
        "errors": [],
    }

    try:
        events = _consume_redis_batch()
        summary["events_consumed"] = len(events)

        if not events:
            logger.info("Sleep cycle: no pending events.")
            return summary

        extractions = _extract_knowledge(events)
        summary["extractions"] = len(extractions)

        if not extractions:
            logger.info("Sleep cycle: LLM produced no extractions.")
            return summary

        write_result = _write_knowledge(extractions)
        summary["nodes_created"] = write_result["nodes_created"]
        summary["edges_created"] = write_result["edges_created"]
        summary["vectors_upserted"] = write_result["vectors_upserted"]

        logger.info("Sleep cycle complete: %s", summary)
        return summary

    except Exception as e:
        error_msg = f"Sleep cycle failed: {e}"
        logger.error(error_msg, exc_info=True)
        summary["errors"].append(error_msg)
        try:
            self.retry(exc=e)
        except self.MaxRetriesExceededError:
            logger.error("Sleep cycle max retries exceeded.")
        return summary
```

### Redis Batch Consumer

```python
def _consume_redis_batch() -> list[dict[str, str]]:
    """
    Read up to BATCH_SIZE messages from the Redis stream using consumer groups.
    - Creates consumer group if it does not exist.
    - Reads pending (unacknowledged) messages first, then new messages.
    - Acknowledges messages after successful read.
    """
    r = sync_redis.from_url(settings.redis_url, decode_responses=True)
    stream_key = settings.redis_stream_key
    group = settings.redis_consumer_group
    consumer = settings.redis_consumer_name
    batch_size = settings.sleep_cycle_batch_size

    # Ensure consumer group exists
    try:
        r.xgroup_create(stream_key, group, id="0", mkstream=True)
    except sync_redis.exceptions.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise

    events = []

    # Read pending first (claimed but not ACK'd)
    pending = r.xreadgroup(group, consumer, {stream_key: "0"}, count=batch_size)
    for stream_name, messages in pending:
        for msg_id, data in messages:
            if data:
                events.append({"id": msg_id, **data})
                r.xack(stream_key, group, msg_id)

    # Read new messages if batch not full
    remaining = batch_size - len(events)
    if remaining > 0:
        new_msgs = r.xreadgroup(
            group, consumer, {stream_key: ">"}, count=remaining, block=100,
        )
        for stream_name, messages in (new_msgs or []):
            for msg_id, data in messages:
                events.append({"id": msg_id, **data})
                r.xack(stream_key, group, msg_id)

    r.close()
    return events
```

### LLM Extraction

```python
def _extract_knowledge(events: list[dict[str, str]]) -> list[SleepCycleExtraction]:
    """
    Send batch of events to the LLM for structured extraction.
    Handles JSON parsing errors gracefully -- returns empty list on parse failure.
    """
    event_lines = []
    for i, ev in enumerate(events, 1):
        payload_str = ev.get("payload", "{}")
        try:
            payload = json.loads(payload_str)
            payload_formatted = json.dumps(payload, indent=2)
        except json.JSONDecodeError:
            payload_formatted = payload_str

        event_lines.append(
            f"--- Event {i} ---\n"
            f"Source: {ev.get('source', 'unknown')}\n"
            f"Timestamp: {ev.get('timestamp', 'unknown')}\n"
            f"Payload:\n{payload_formatted}\n"
        )

    events_text = "\n".join(event_lines)

    client = OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout,
    )

    try:
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": EXTRACTION_USER_TEMPLATE.format(
                    events_text=events_text,
                )},
            ],
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )

        raw_content = response.choices[0].message.content.strip()

        # Strip markdown code fences if present
        if raw_content.startswith("```"):
            lines = raw_content.split("\n")
            raw_content = "\n".join(
                line for line in lines if not line.strip().startswith("```")
            )

        parsed = json.loads(raw_content)
        if not isinstance(parsed, list):
            logger.warning("LLM returned non-array: %s", type(parsed))
            return []

        extractions = []
        for item in parsed:
            try:
                extractions.append(SleepCycleExtraction(**item))
            except Exception as e:
                logger.warning("Skipping invalid extraction: %s -- %s", item, e)
        return extractions

    except json.JSONDecodeError as e:
        logger.error("LLM response was not valid JSON: %s", e)
        return []
    except Exception as e:
        raise LLMError(f"LLM extraction call failed: {e}") from e
```

### Knowledge Writer

```python
def _write_knowledge(extractions: list[SleepCycleExtraction]) -> dict[str, int]:
    """
    Write extracted knowledge to Neo4j and Qdrant (sync clients for Celery).
    Individual extraction failures are logged and skipped.
    """
    from neo4j import GraphDatabase
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct

    result = {"nodes_created": 0, "edges_created": 0, "vectors_upserted": 0}

    neo4j_driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    qdrant = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
    embed_client = OpenAI(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
    )

    try:
        for extraction in extractions:
            action_id = str(uuid4())
            outcome_id = str(uuid4())
            resolution_id = str(uuid4())
            domain_id = f"domain:{extraction.domain.lower()}"

            try:
                with neo4j_driver.session(database=settings.neo4j_database) as session:
                    # Domain
                    session.run(
                        "MERGE (n:Domain {id: $id}) "
                        "SET n.name = $name, n.updated_at = datetime() "
                        "ON CREATE SET n.created_at = datetime()",
                        {"id": domain_id, "name": extraction.domain},
                    )
                    result["nodes_created"] += 1

                    # Action
                    session.run(
                        "MERGE (n:Action {id: $id}) "
                        "SET n.description = $desc, n.domain = $domain, "
                        "n.source = 'sleep_cycle', n.updated_at = datetime() "
                        "ON CREATE SET n.created_at = datetime()",
                        {"id": action_id, "desc": extraction.action_summary,
                         "domain": extraction.domain},
                    )
                    result["nodes_created"] += 1

                    # Domain -> Action
                    session.run(
                        "MATCH (a {id: $src}) MATCH (b {id: $tgt}) "
                        "MERGE (a)-[r:RELATES_TO]->(b) SET r.updated_at = datetime()",
                        {"src": domain_id, "tgt": action_id},
                    )
                    result["edges_created"] += 1

                    # Outcome
                    session.run(
                        "MERGE (n:Outcome {id: $id}) "
                        "SET n.type = $type, n.detail = $detail, "
                        "n.updated_at = datetime() ON CREATE SET n.created_at = datetime()",
                        {"id": outcome_id, "type": extraction.outcome.value,
                         "detail": extraction.action_summary},
                    )
                    result["nodes_created"] += 1

                    # Action -> Outcome
                    session.run(
                        "MATCH (a {id: $src}) MATCH (b {id: $tgt}) "
                        "MERGE (a)-[r:CAUSED]->(b) SET r.updated_at = datetime()",
                        {"src": action_id, "tgt": outcome_id},
                    )
                    result["edges_created"] += 1

                    # Resolution (if present)
                    if extraction.resolution_summary:
                        session.run(
                            "MERGE (n:Resolution {id: $id}) "
                            "SET n.description = $desc, n.updated_at = datetime() "
                            "ON CREATE SET n.created_at = datetime()",
                            {"id": resolution_id, "desc": extraction.resolution_summary},
                        )
                        result["nodes_created"] += 1
                        session.run(
                            "MATCH (a {id: $src}) MATCH (b {id: $tgt}) "
                            "MERGE (a)-[r:RESOLVED_BY]->(b) SET r.updated_at = datetime()",
                            {"src": outcome_id, "tgt": resolution_id},
                        )
                        result["edges_created"] += 1

                    # Concepts
                    for concept in extraction.concepts:
                        concept_id = f"concept:{concept.lower()}"
                        session.run(
                            "MERGE (n:Concept {id: $id}) "
                            "SET n.name = $name, n.updated_at = datetime() "
                            "ON CREATE SET n.created_at = datetime()",
                            {"id": concept_id, "name": concept},
                        )
                        result["nodes_created"] += 1
                        session.run(
                            "MATCH (a {id: $src}) MATCH (b {id: $tgt}) "
                            "MERGE (a)-[r:UTILIZES]->(b) SET r.updated_at = datetime()",
                            {"src": action_id, "tgt": concept_id},
                        )
                        result["edges_created"] += 1

            except Exception as e:
                logger.error("Failed to write extraction to graph: %s", e)
                continue

            # Embed and upsert to Qdrant
            text_to_embed = (
                f"Action: {extraction.action_summary}\n"
                f"Outcome ({extraction.outcome.value}): {extraction.action_summary}\n"
                f"Resolution: {extraction.resolution_summary}"
            ).strip()

            try:
                embed_response = embed_client.embeddings.create(
                    model=settings.embedding_model, input=text_to_embed,
                )
                vector = embed_response.data[0].embedding
                qdrant.upsert(
                    collection_name=settings.qdrant_collection,
                    points=[PointStruct(
                        id=action_id, vector=vector,
                        payload={
                            "text": text_to_embed,
                            "source_type": "action",
                            "domain": extraction.domain,
                            "tags": [],
                            "concepts": extraction.concepts,
                            "outcome_type": extraction.outcome.value,
                            "graph_node_id": action_id,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )],
                )
                result["vectors_upserted"] += 1
            except Exception as e:
                logger.warning("Failed to embed/upsert extraction %s: %s", action_id, e)

    finally:
        neo4j_driver.close()
        qdrant.close()

    return result
```

### Error Recovery Strategy

| Failure | Recovery |
|---|---|
| Redis read fails | Celery retries with exponential backoff (30s, 60s, 120s) |
| LLM returns invalid JSON | Log warning, return empty extractions, events already ACK'd |
| LLM times out | Raise `LLMError`, Celery retries the whole task |
| Individual extraction parse fails | Skip that extraction, continue with remaining |
| Neo4j write fails for one extraction | Log error, continue with next extraction |
| Qdrant upsert fails | Log warning, graph nodes still exist (eventual consistency) |
| Consumer group doesn't exist | Auto-created on first read |

---

## 11. Redis Queue Protocol

### Stream Key

```
firekeep:event_stream
```

### Message Format

Each Redis stream message is a flat hash:

| Field | Type | Description |
|---|---|---|
| `source` | string | Event source identifier (e.g. "agent-alpha", "code-runner") |
| `payload` | string | JSON-encoded arbitrary payload |
| `timestamp` | string | ISO 8601 timestamp |

### Consumer Group Configuration

| Setting | Value |
|---|---|
| Group name | `firekeep_workers` (configurable) |
| Consumer name | `worker-1` (configurable per instance) |
| Read strategy | Pending first (`id=0`), then new (`id=>`) |
| Batch size | 50 (configurable) |
| Block timeout | 100ms in worker context |
| Acknowledgment | Immediate after successful read (before processing) |

### Stream Management

- Stream auto-created on first `XADD` (via `mkstream=True` on `XGROUP CREATE`).
- No TTL on stream messages. Sleep cycle worker ACKs them.
- For production, add `MAXLEN ~ 10000` to `XADD` to cap memory usage.

---

## 12. Inter-Module Data Flow

### Flow 1: `/memory/recall` (Synchronous)

```
Client POST /memory/recall {task, tags, top_k}
  |
  v
main.py --> RAGEngine.recall(ContextQuery)
  |
  |-> embed_text(task)
  |     --> OpenAI /v1/embeddings --> vector[1024]
  |
  |-> VectorClient.search(vector, top_k, tags)
  |     --> Qdrant HTTP/gRPC --> list[{id, score, text, metadata}]
  |
  |-> GraphClient.find_related_actions(tags, top_k)
  |     --> Neo4j Bolt --> list[{id, description, outcomes, resolutions, depth}]
  |
  |-> GraphClient.find_by_text_search(task[:200], top_k)
  |     --> Neo4j Bolt --> list[{id, label, description, name, domain}]
  |
  |-> _merge_results(vector_results, graph_tag_results, graph_text_results, top_k)
  |     --> Deduplicate, score, sort --> list[MemoryResult]
  |
  --> _format_markdown(results, task)
        --> Markdown string
  |
  v
Client <-- RecallResponse {query, results, markdown, total_results, retrieval_time_ms}
```

### Flow 2: `/memory/learn` (Synchronous)

```
Client POST /memory/learn {domain, attempted_action, outcome, ...}
  |
  v
main.py --> RAGEngine.learn(ActionLog)
  |
  |-> GraphClient.log_action(domain, action, outcome, resolution, concepts, tags)
  |     --> Neo4j: MERGE Domain, Action, Outcome, Resolution, Concept nodes + edges
  |     --> Returns {action_id, nodes_created, edges_created}
  |
  |-> embed_text("Action: ... Outcome: ... Resolution: ...")
  |     --> OpenAI /v1/embeddings --> vector[1024]
  |
  --> VectorClient.upsert([VectorRecord])
        --> Qdrant upsert --> count
  |
  v
Client <-- LearnResponse {action_id, nodes_created, edges_created, vectors_upserted}
```

### Flow 3: `/memory/stream` (Async Ingestion)

```
Client POST /memory/stream {source, payload, timestamp}
  |
  v
main.py --> RAGEngine.ingest_event(GenericEventIngest)
  |
  --> Redis XADD firekeep:event_stream {source, payload(json), timestamp(iso)}
        --> Returns stream message ID
  |
  v
Client <-- StreamResponse {event_id, queued: true}
```

### Flow 4: Sleep Cycle (Background, Periodic)

```
Celery Beat timer (every 300s)
  |
  v
run_sleep_cycle()
  |
  |-> _consume_redis_batch()
  |     --> XREADGROUP firekeep_workers/worker-1 from firekeep:event_stream
  |     --> XACK each message
  |     --> Returns list[{id, source, payload, timestamp}]
  |
  |-> _extract_knowledge(events)
  |     --> Format events as prompt text
  |     --> POST /v1/chat/completions (system + user prompt)
  |     --> Parse JSON array from response
  |     --> Validate each item as SleepCycleExtraction
  |     --> Returns list[SleepCycleExtraction]
  |
  --> _write_knowledge(extractions)
        |-> For each extraction:
        |     |-> Neo4j: MERGE Domain, Action, Outcome, Resolution, Concept + edges
        |     |-> OpenAI /v1/embeddings --> vector[1024]
        |     --> Qdrant upsert point
        --> Returns {nodes_created, edges_created, vectors_upserted}
```

---

## 13. Docker Compose Topology

**File**: `docker-compose.yml`

> **This is the v1 standalone compose, not the deployed topology.** The real stack is the **root** `docker-compose.yml` (13 containers): `neo4j`, `qdrant` (v1.13.2), `redis`, `ollama`, `ollama-pull` (init), `cortex-api` (published `0.0.0.0:8100:8000`), `cortex-mcp` (8080), `cortex-worker` (`celery worker --pool=solo`), a **separate** `cortex-beat` (celery beat), plus `bridge`, `sentinel`, `relay`, and `dashboard`. Cortex builds from `cortex/Dockerfile` with the repo root as build context. Infra ports bind to `127.0.0.1`; the four data volumes are `external: true` and named `firekeepcortex_{neo4j,qdrant,redis,ollama}_data`. There is **no** `symdex` service — code intelligence ships client-side as the stdio `firekeep-symdex` MCP server. The single-`app` / `worker` YAML below is retained only as the v1 reference.

```yaml
version: "3.8"

services:
  app:
    build: .
    ports:
      - "${PORT:-8000}:8000"
    env_file: .env
    depends_on:
      neo4j:
        condition: service_healthy
      qdrant:
        condition: service_started
      redis:
        condition: service_healthy
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000

  worker:
    build: .
    env_file: .env
    depends_on:
      neo4j:
        condition: service_healthy
      qdrant:
        condition: service_started
      redis:
        condition: service_healthy
    command: celery -A app.workers.sleep_cycle worker --beat --loglevel=info

  neo4j:
    image: neo4j:5-community
    ports:
      - "7474:7474"
      - "7687:7687"
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:-firekeepcortex}
      NEO4J_PLUGINS: '["apoc"]'
    volumes:
      - neo4j_data:/data
    healthcheck:
      test: ["CMD", "cypher-shell", "-u", "neo4j", "-p", "${NEO4J_PASSWORD:-firekeepcortex}", "RETURN 1"]
      interval: 10s
      timeout: 5s
      retries: 5

  qdrant:
    image: qdrant/qdrant:v1.7.4
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - qdrant_data:/qdrant/storage

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  ollama:
    image: ollama/ollama:latest
    ports:
      - "11434:11434"
    volumes:
      - ollama_data:/root/.ollama
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

volumes:
  neo4j_data:
  qdrant_data:
  redis_data:
  ollama_data:
```

### Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## 14. Security Considerations

### v1 Threat Model (Single-Tenant, Local Network)

| Concern | Mitigation |
|---|---|
| Authentication | **Implemented (post-v1)** — scope-based per-key auth via a shared pure-ASGI validator (`auth/asgi.py`, `FirekeepKeyAuthMiddleware`) on Cortex REST and every MCP surface, opt-in with `AUTH_ENABLED=true` (fail-closed if Redis DB 7 is unreachable). Default off on the personal VPS. Keys minted by `deploy/bootstrap-keys.sh`; scopes include `memory:read/write`, `session:read/write`, `admin`. |
| Cypher injection | All queries use parameterized variables (`$id`, `$props`). Node labels and edge types come from enums, never user input. |
| Payload injection | `GenericEventIngest.payload` is JSON-serialized before Redis storage. Never interpolated into queries. |
| LLM prompt injection | Extraction prompt is system-controlled. User data goes in user message. Extractions validated via Pydantic before graph writes. |
| Redis exposure | Bind to internal Docker network only. No external port mapping in production. |
| Neo4j credentials | Loaded from env vars, never hardcoded. `.env` in `.gitignore`. |
| Denial of service | `max_length` constraints on all string fields. `top_k` capped at 100. Redis stream capped at 10K messages. |

### `.gitignore` Requirements

```
.env
__pycache__/
*.pyc
.venv/
```

---

## 15. Observability Plan

### Logging

- **Library**: Python `logging` module with `structlog` for structured JSON logs in production.
- **Logger hierarchy**: `firekeepcortex`, `firekeepcortex.graph`, `firekeepcortex.vector`, `firekeepcortex.rag`, `firekeepcortex.sleep_cycle`.
- **Log levels**: DEBUG for query details, INFO for operations, WARNING for recoverable errors, ERROR for failures.

### Key Metrics (future Prometheus integration)

| Metric | Type | Description |
|---|---|---|
| `firekeep_recall_duration_seconds` | Histogram | Time to complete a /recall request |
| `firekeep_learn_duration_seconds` | Histogram | Time to complete a /learn request |
| `firekeep_stream_events_total` | Counter | Total events pushed to Redis stream |
| `firekeep_sleep_cycle_runs_total` | Counter | Total sleep cycle executions |
| `firekeep_sleep_cycle_events_processed` | Counter | Events consumed per cycle |
| `firekeep_sleep_cycle_extractions` | Counter | Knowledge extractions produced |
| `firekeep_embedding_duration_seconds` | Histogram | Time per embedding call |
| `firekeep_graph_query_duration_seconds` | Histogram | Time per Neo4j query |
| `firekeep_vector_search_duration_seconds` | Histogram | Time per Qdrant search |

### Health Endpoint

`GET /health` returns status of all backing services. Suitable for Docker healthcheck and load balancer probes.

---

## 16. Acceptance Criteria

### API

- [ ] `POST /memory/recall` returns `RecallResponse` with merged results from both Qdrant and Neo4j.
- [ ] `POST /memory/recall` with tags filters graph results to matching domains/concepts.
- [ ] `POST /memory/recall` returns a Markdown block suitable for LLM system prompt injection.
- [ ] `POST /memory/learn` creates the full node subgraph (Domain, Action, Outcome, Resolution, Concepts) in Neo4j.
- [ ] `POST /memory/learn` upserts a vector to Qdrant with correct metadata payload.
- [ ] `POST /memory/stream` pushes an event to the Redis stream and returns the stream message ID.
- [ ] `GET /health` returns connectivity status of Neo4j, Qdrant, and Redis.
- [ ] All endpoints return `ErrorResponse` on failure with appropriate HTTP status codes.

### Sleep Cycle

- [ ] Worker reads batches of up to 50 events from the Redis stream.
- [ ] Worker sends events to the LLM and parses the JSON array response.
- [ ] Worker writes extracted knowledge to both Neo4j and Qdrant.
- [ ] Worker retries up to 3 times on transient failures.
- [ ] Invalid LLM responses (non-JSON, malformed) are logged and skipped without crashing.
- [ ] Individual extraction failures do not block remaining extractions.

### Data Integrity

- [ ] Graph nodes use deterministic IDs for Domain (`domain:<name>`) and Concept (`concept:<name>`) for MERGE idempotency.
- [ ] Action, Outcome, Resolution nodes use UUID v4 IDs.
- [ ] Vector payloads include `graph_node_id` linking back to the Neo4j Action node.
- [ ] All graph nodes have `created_at` and `updated_at` timestamps.

### Infrastructure

- [ ] `docker-compose up` brings up all services (app, worker, neo4j, qdrant, redis, ollama).
- [ ] Neo4j healthcheck passes before app starts.
- [ ] Qdrant collection is auto-created on first startup.
- [ ] Redis consumer group is auto-created on first worker run.

---

## Appendix A: Dependency Versions (`requirements.txt`)

```
fastapi>=0.109.0,<1.0
uvicorn[standard]>=0.27.0,<1.0
pydantic>=2.5.0,<3.0
pydantic-settings>=2.1.0,<3.0
neo4j>=5.15.0,<6.0
qdrant-client>=1.7.0,<2.0
redis>=5.0.0,<6.0
celery>=5.3.0,<6.0
openai>=1.10.0,<2.0
structlog>=24.1.0
```

---

## Appendix B: File Tree

```
FirekeepCortex/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── .gitignore
├── requirements.txt
├── docs/
│   └── ARCHITECTURE.md        # This document
├── app/
│   ├── __init__.py
│   ├── config.py              # Pydantic BaseSettings, env parsing
│   ├── main.py                # FastAPI app factory, lifespan, routes, DI
│   ├── models.py              # Pydantic request/response/domain models
│   ├── exceptions.py          # Custom exception hierarchy
│   ├── db/
│   │   ├── __init__.py
│   │   ├── graph.py           # Neo4j async client, all Cypher queries
│   │   └── vector.py          # Qdrant async client, collection management
│   ├── engine/
│   │   ├── __init__.py
│   │   └── rag.py             # RAG engine: embed, dual-retrieve, merge, format
│   └── workers/
│       ├── __init__.py
│       └── sleep_cycle.py     # Celery task: consume, extract, write
└── tests/
    ├── __init__.py
    ├── conftest.py            # Shared fixtures (mock clients, test settings)
    ├── test_models.py         # Pydantic model validation tests
    ├── test_api.py            # FastAPI endpoint integration tests
    ├── test_graph.py          # Neo4j client unit tests (mocked driver)
    ├── test_vector.py         # Qdrant client unit tests (mocked client)
    ├── test_rag.py            # RAG engine tests (merge logic, formatting)
    └── test_sleep_cycle.py    # Sleep cycle tests (extraction parsing, writes)
```

---

## Appendix C: Testing Strategy

### Test Pyramid

| Layer | What | How |
|---|---|---|
| Unit | Models, merge algorithm, Markdown formatting, extraction parsing | Pure Python, no external deps |
| Integration (mocked) | Graph/Vector clients, RAG engine | Mock Neo4j driver, Qdrant client, OpenAI client |
| Integration (live) | Full pipeline with real services | `docker-compose -f docker-compose.test.yml up` |

### Key Test Fixtures (`tests/conftest.py`)

```python
import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.graph import GraphClient
from app.db.vector import VectorClient
from app.engine.rag import RAGEngine
from app.main import create_app


@pytest.fixture
def test_settings() -> Settings:
    """Settings with test-safe defaults."""
    return Settings(
        neo4j_uri="bolt://localhost:7687",
        neo4j_password="test",
        qdrant_host="localhost",
        redis_url="redis://localhost:6379/15",  # DB 15 for tests
        llm_base_url="http://localhost:11434/v1",
        debug=True,
    )


@pytest.fixture
def mock_graph() -> AsyncMock:
    """Mocked GraphClient."""
    graph = AsyncMock(spec=GraphClient)
    graph.is_connected.return_value = True
    graph.log_action.return_value = {
        "action_id": "test-uuid",
        "nodes_created": 3,
        "edges_created": 2,
    }
    graph.find_related_actions.return_value = []
    graph.find_by_text_search.return_value = []
    return graph


@pytest.fixture
def mock_vector() -> AsyncMock:
    """Mocked VectorClient."""
    vector = AsyncMock(spec=VectorClient)
    vector.is_connected.return_value = True
    vector.upsert.return_value = 1
    vector.search.return_value = []
    return vector


@pytest.fixture
def mock_engine(test_settings, mock_graph, mock_vector) -> RAGEngine:
    """RAGEngine with mocked dependencies."""
    engine = RAGEngine(test_settings, mock_graph, mock_vector)
    engine.embed_text = AsyncMock(return_value=[0.1] * 1024)
    engine.embed_texts = AsyncMock(return_value=[[0.1] * 1024])
    return engine


@pytest.fixture
def test_client(test_settings, mock_graph, mock_vector, mock_engine) -> TestClient:
    """FastAPI TestClient with mocked dependencies."""
    app = create_app()
    app.state.graph = mock_graph
    app.state.vector = mock_vector
    app.state.engine = mock_engine
    app.state.settings = test_settings
    return TestClient(app)
```

---

*End of Architecture Specification*
