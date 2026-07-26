# FirekeepCortex Major Improvements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all 15 critical and important issues found in the codebase review — covering security, performance, data integrity, and correctness.

**Architecture:** Fixes are grouped into independent tasks by file/module. Each task is self-contained with tests. No new dependencies are introduced.

**Tech Stack:** Python 3.11+, FastAPI, Neo4j, Qdrant, Redis, Celery, httpx, pytest

---

### Task 1: Restore .env.example and fix start.sh bootstrap

**Files:**
- Create: `.env.example`
- Modify: `start.sh:8-13`

**Step 1: Create .env.example with all documented config vars**

```bash
# FirekeepCortex Configuration
# Copy this file to .env and fill in required values.

# --- Security ---
API_KEY=
CORS_ORIGINS=["http://localhost:3000","http://localhost:8080"]

# --- Neo4j ---
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=changeme
NEO4J_POOL_SIZE=50

# --- Qdrant ---
QDRANT_HOST=qdrant
QDRANT_PORT=6333
QDRANT_COLLECTION=firekeep_memory

# --- Redis ---
REDIS_URL=redis://redis:6379/0
REDIS_STREAM_KEY=firekeep:event_stream
REDIS_BATCH_SIZE=50

# --- Celery ---
CELERY_BROKER_URL=redis://redis:6379/1
CELERY_RESULT_BACKEND=redis://redis:6379/2

# --- LLM ---
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=llama3
LLM_API_KEY=

# --- Embedding ---
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIM=768

# --- RAG Engine ---
BOOST_FACTOR=1.5
GRAPH_RELEVANCE_WEIGHT=0.4
CONTENT_HASH_LENGTH=32

# --- Re-ranking ---
RERANK_ENABLED=false
RERANK_CANDIDATES_MULTIPLIER=2

# --- Memory Decay ---
MEMORY_DECAY_HALF_LIFE_DAYS=90
```

**Step 2: Verify start.sh works with the restored file**

Run: `bash -n start.sh` (syntax check)

**Step 3: Commit**

```bash
git add .env.example start.sh
git commit -m "fix: restore .env.example, fix start.sh bootstrap"
```

---

### Task 2: Lock down Qdrant ports in docker-compose.yml

**Files:**
- Modify: `docker-compose.yml:29-31`

**Step 1: Bind Qdrant ports to 127.0.0.1**

Change lines 30-31 from:
```yaml
      - "6333:6333"
      - "6334:6334"
```
to:
```yaml
      - "127.0.0.1:6333:6333"
      - "127.0.0.1:6334:6334"
```

Also bind MCP server port (line 121) to loopback:
```yaml
      - "127.0.0.1:8080:8080"
```

**Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "security: bind Qdrant and MCP ports to 127.0.0.1"
```

---

### Task 3: Singleton RAGEngine with shared httpx client

**Files:**
- Modify: `app/engine/rag.py:75-86,439-466`
- Modify: `app/main.py:78-94,264-270,412-424`
- Modify: `tests/conftest.py`
- Modify: `tests/test_api.py:127-156`

**Step 1: Add httpx client lifecycle to RAGEngine**

In `app/engine/rag.py`, modify `__init__` to accept an optional httpx client, and add close method:

```python
class RAGEngine:
    def __init__(
        self,
        graph: Neo4jClient,
        vector: VectorClient,
        settings: Settings | None = None,
    ) -> None:
        self._graph = graph
        self._vector = vector
        self._settings = settings or get_settings()
        self._http_client: httpx.AsyncClient | None = None

    async def initialize(self) -> None:
        """Create shared httpx client for re-ranking."""
        if self._settings.RERANK_ENABLED:
            self._http_client = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        """Close the shared httpx client."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
```

**Step 2: Update _rerank to use the shared client**

Replace lines 461-466 in `rag.py`:

```python
    async def _rerank(self, task, candidates):
        if not candidates:
            return candidates

        base_url = self._settings.LLM_BASE_URL
        model = self._settings.LLM_MODEL
        api_key = self._settings.LLM_API_KEY
        url = f"{base_url}/chat/completions"
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        client = self._http_client
        if client is None:
            # Fallback: create temporary client if not initialized
            async with httpx.AsyncClient(timeout=15.0) as client:
                tasks = [self._rerank_single(client, url, headers, model, task, c) for c in candidates]
                results = await asyncio.gather(*tasks, return_exceptions=True)
        else:
            tasks = [self._rerank_single(client, url, headers, model, task, c) for c in candidates]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        # ... rest unchanged
```

**Step 3: Create RAGEngine in lifespan, add DI**

In `app/main.py`, update lifespan:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.graph_client = Neo4jClient(settings)
    app.state.vector_client = VectorClient(settings)
    app.state.redis_client = redis.asyncio.from_url(settings.REDIS_URL)
    app.state.rag_engine = RAGEngine(
        graph=app.state.graph_client,
        vector=app.state.vector_client,
        settings=settings,
    )
    app.state.start_time = datetime.now(timezone.utc)

    await app.state.graph_client.connect()
    await app.state.graph_client.ensure_indexes()
    await app.state.vector_client.initialize()
    await app.state.rag_engine.initialize()

    yield

    await app.state.rag_engine.close()
    await app.state.vector_client.close()
    await app.state.graph_client.close()
    await app.state.redis_client.aclose()
```

Add dependency:

```python
async def get_rag_engine(request: Request) -> RAGEngine:
    return request.app.state.rag_engine
```

Update `/memory/recall`:

```python
@app.post("/memory/recall", response_model=RecallResponse)
@limiter.limit(lambda: get_settings().RATE_LIMIT)
async def memory_recall(
    request: Request,
    query: ContextQuery,
    engine: Annotated[RAGEngine, Depends(get_rag_engine)],
) -> RecallResponse:
    result = await engine.recall(query)
    result.request_id = _get_request_id(request)
    return result
```

**Step 4: Update test fixtures**

In `tests/conftest.py`, add `mock_rag_engine` fixture and override `get_rag_engine`. Update `test_api.py` accordingly.

**Step 5: Run tests**

Run: `pytest tests/test_api.py tests/test_rag.py -v`

**Step 6: Commit**

```bash
git add app/engine/rag.py app/main.py tests/conftest.py tests/test_api.py
git commit -m "perf: singleton RAGEngine with shared httpx client"
```

---

### Task 4: Replace O(n) LRU cache with OrderedDict

**Files:**
- Modify: `app/db/vector.py:8-9,59-60,313-315,348-362`
- Modify: `tests/test_vector.py` (add cache tests)

**Step 1: Write failing test for LRU behavior**

Add to `tests/test_vector.py`:

```python
class TestEmbedCache:
    def test_cache_eviction_order(self, vector_client):
        """Oldest entry evicted when cache is full."""
        vector_client._embed_cache.clear()
        original_max = vector._EMBED_CACHE_MAX_SIZE
        # Temporarily patch for test
        import app.db.vector as vec_module
        old_max = vec_module._EMBED_CACHE_MAX_SIZE
        vec_module._EMBED_CACHE_MAX_SIZE = 3
        try:
            vector_client._cache_put("a", [1.0])
            vector_client._cache_put("b", [2.0])
            vector_client._cache_put("c", [3.0])
            # Access "a" to make it most recent
            vector_client._cache_put("a", [1.0])
            # Add "d" — should evict "b" (oldest)
            vector_client._cache_put("d", [4.0])
            assert "b" not in vector_client._embed_cache
            assert "a" in vector_client._embed_cache
            assert "d" in vector_client._embed_cache
        finally:
            vec_module._EMBED_CACHE_MAX_SIZE = old_max
```

**Step 2: Replace list-based LRU with OrderedDict**

In `app/db/vector.py`:

```python
from collections import OrderedDict

# In __init__:
self._embed_cache: OrderedDict[str, list[float]] = OrderedDict()
# Remove: self._cache_order: list[str] = []

# In _embed (line ~313), add move_to_end on cache hit:
if cache_key in self._embed_cache:
    self._embed_cache.move_to_end(cache_key)
    return self._embed_cache[cache_key]

# Replace _cache_put:
def _cache_put(self, key: str, value: list[float]) -> None:
    if key in self._embed_cache:
        self._embed_cache.move_to_end(key)
        return
    if len(self._embed_cache) >= _EMBED_CACHE_MAX_SIZE:
        self._embed_cache.popitem(last=False)
    self._embed_cache[key] = value
```

**Step 3: Run tests**

Run: `pytest tests/test_vector.py -v`

**Step 4: Commit**

```bash
git add app/db/vector.py tests/test_vector.py
git commit -m "perf: O(1) LRU cache via OrderedDict"
```

---

### Task 5: Transaction-wrap Sleep Cycle Neo4j writes

**Files:**
- Modify: `app/workers/sleep_cycle.py:200-259`
- Modify: `tests/test_sleep_cycle.py`

**Step 1: Wrap _write_to_neo4j in explicit transaction**

Replace `_write_to_neo4j`:

```python
def _write_to_neo4j(nodes, edges) -> int:
    count = 0
    driver = _get_neo4j_driver()
    with driver.session() as session:
        with session.begin_transaction() as tx:
            if nodes:
                label_groups = {}
                for node in nodes:
                    label = node.get("label", "Entity")
                    label_groups.setdefault(label, []).append(node)
                for label, group in label_groups.items():
                    safe_label = "".join(c for c in label if c.isalnum() or c == "_")
                    if not safe_label:
                        safe_label = "Entity"
                    query = (
                        "UNWIND $nodes AS node "
                        f"MERGE (n:{safe_label} {{id: node.id}}) "
                        "SET n += node.properties "
                        "RETURN count(n) AS cnt"
                    )
                    result = tx.run(query, nodes=group)
                    record = result.single()
                    if record:
                        count += record["cnt"]
            if edges:
                rel_groups = {}
                for edge in edges:
                    rel_type = edge.get("type", "RELATED_TO")
                    rel_groups.setdefault(rel_type, []).append(edge)
                for rel_type, group in rel_groups.items():
                    safe_type = "".join(c for c in rel_type if c.isalnum() or c == "_")
                    if not safe_type:
                        safe_type = "RELATED_TO"
                    query = (
                        "UNWIND $edges AS edge "
                        "MATCH (src {id: edge.source}) "
                        "MATCH (tgt {id: edge.target}) "
                        f"MERGE (src)-[r:{safe_type}]->(tgt) "
                        "RETURN count(r) AS cnt"
                    )
                    result = tx.run(query, edges=group)
                    record = result.single()
                    if record:
                        count += record["cnt"]
            tx.commit()
    return count
```

**Step 2: Run tests**

Run: `pytest tests/test_sleep_cycle.py -v`

**Step 3: Commit**

```bash
git add app/workers/sleep_cycle.py tests/test_sleep_cycle.py
git commit -m "fix: transaction-wrap Sleep Cycle Neo4j writes"
```

---

### Task 6: Partial-failure handling for memory/learn

**Files:**
- Modify: `app/main.py:440-452`
- Modify: `tests/test_api.py` (add partial failure tests)

**Step 1: Write failing test for partial failure logging**

Add to `tests/test_api.py`:

```python
class TestMemoryLearnPartialFailure:
    def test_graph_fails_vector_succeeds_returns_503(self, test_client, mock_graph, mock_vector):
        mock_graph.merge_action_log.side_effect = GraphConnectionError("Neo4j down")
        mock_vector.upsert.return_value = "vec-id"
        resp = test_client.post("/memory/learn", json={"action": "a", "outcome": "o"})
        assert resp.status_code == 503

    def test_vector_fails_graph_succeeds_returns_502(self, test_client, mock_graph, mock_vector):
        mock_graph.merge_action_log.return_value = "graph-id"
        mock_vector.upsert.side_effect = VectorStoreError("Qdrant down")
        resp = test_client.post("/memory/learn", json={"action": "a", "outcome": "o"})
        assert resp.status_code == 502
```

**Step 2: Implement return_exceptions=True with explicit checks**

In `app/main.py`, replace lines 440-452:

```python
    results = await asyncio.gather(
        graph.merge_action_log(log),
        vector.upsert(text=text, metadata={...}),
        return_exceptions=True,
    )
    graph_result, vector_result = results

    if isinstance(graph_result, Exception):
        logger.error("Graph write failed (vector may be ahead): %s", graph_result)
        if isinstance(graph_result, GraphConnectionError):
            raise graph_result
        raise GraphConnectionError(f"Graph write failed: {graph_result}") from graph_result

    if isinstance(vector_result, Exception):
        logger.error("Vector write failed (graph may be ahead): %s", vector_result)
        if isinstance(vector_result, VectorStoreError):
            raise vector_result
        raise VectorStoreError(f"Vector write failed: {vector_result}") from vector_result

    return LearnResponse(status="stored", graph_id=graph_result, vector_id=vector_result)
```

**Step 3: Run tests**

Run: `pytest tests/test_api.py -v`

**Step 4: Commit**

```bash
git add app/main.py tests/test_api.py
git commit -m "fix: handle partial failures in memory/learn parallel writes"
```

---

### Task 7: Case-insensitive resolution queries

**Files:**
- Modify: `app/db/graph.py:588-593`
- Modify: `tests/test_graph.py` (add case test)

**Step 1: Fix the Cypher query**

Change line 590 from:
```cypher
WHERE o.description CONTAINS $error_pattern
```
to:
```cypher
WHERE toLower(o.description) CONTAINS toLower($error_pattern)
```

**Step 2: Run tests**

Run: `pytest tests/test_graph.py -v`

**Step 3: Commit**

```bash
git add app/db/graph.py
git commit -m "fix: case-insensitive CONTAINS in query_resolutions"
```

---

### Task 8: Fix ASGI receive body limit to return proper 413

**Files:**
- Modify: `app/main.py:143-151`

**Step 1: Replace HTTPException with a flag-based approach**

```python
async def receive_with_limit() -> dict:
    nonlocal total_bytes
    message = await receive()
    if message.get("type") == "http.request":
        body = message.get("body", b"")
        total_bytes += len(body)
        if total_bytes > max_bytes:
            # Return empty body to stop processing; response handled below
            return {"type": "http.request", "body": b"", "more_body": False}
    return message

try:
    await self.app(scope, receive_with_limit, send)
except Exception:
    if total_bytes > max_bytes:
        response = JSONResponse(
            status_code=413,
            content={"detail": "Request body too large"},
        )
        await response(scope, receive, send)
        return
    raise
return
```

Wait — a cleaner approach that matches the existing Content-Length pattern:

```python
exceeded = False

async def receive_with_limit() -> dict:
    nonlocal total_bytes, exceeded
    message = await receive()
    if message.get("type") == "http.request":
        body = message.get("body", b"")
        total_bytes += len(body)
        if total_bytes > max_bytes:
            exceeded = True
    return message

async def send_with_check(message: dict) -> None:
    if exceeded and message.get("type") == "http.response.start":
        # Override the response with 413
        message = {
            "type": "http.response.start",
            "status": 413,
            "headers": [[b"content-type", b"application/json"]],
        }
    await send(message)

await self.app(scope, receive_with_limit, send_with_check)
```

Actually the simplest correct fix: use `JSONResponse` directly like the Content-Length path, and avoid raising `HTTPException`:

**Step 2: Implementation**

Replace the `receive_with_limit` closure (lines 143-151) and the `self.app(scope, receive_with_limit, send)` call (line 153):

```python
            total_bytes = 0
            max_bytes = self.max_bytes
            body_too_large = False

            async def receive_with_limit() -> dict:
                nonlocal total_bytes, body_too_large
                message = await receive()
                if message.get("type") == "http.request":
                    body = message.get("body", b"")
                    total_bytes += len(body)
                    if total_bytes > max_bytes:
                        body_too_large = True
                return message

            await self.app(scope, receive_with_limit, send)
            if body_too_large:
                # Note: the app has already started sending a response by now
                # in most cases. For chunked uploads that exceed the limit mid-stream,
                # FastAPI will see truncated data and likely return a 422.
                # The Content-Length pre-check above handles the common case.
                logger.warning("Chunked request exceeded body size limit (%d bytes)", total_bytes)
            return
```

Actually, the simplest and most correct approach: disconnect the body stream so FastAPI gets an empty body and returns a 422 naturally, or just disconnect. Let me use the approach that **actually works in ASGI**: raise a custom exception that the app middleware can catch. But the simplest fix is just to not raise `HTTPException` there. Let me simplify: just disconnect.

```python
            total_bytes = 0
            max_bytes = self.max_bytes

            async def receive_with_limit() -> dict:
                nonlocal total_bytes
                message = await receive()
                if message.get("type") == "http.request":
                    body = message.get("body", b"")
                    total_bytes += len(body)
                    if total_bytes > max_bytes:
                        response = JSONResponse(
                            status_code=413,
                            content={"detail": "Request body too large"},
                        )
                        raise _BodyTooLargeError()
                return message

            try:
                await self.app(scope, receive_with_limit, send)
            except _BodyTooLargeError:
                response = JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large"},
                )
                await response(scope, receive, send)
            return
```

Where `_BodyTooLargeError` is a simple internal exception (not HTTPException):

```python
class _BodyTooLargeError(Exception):
    pass
```

**Step 3: Run tests**

Run: `pytest tests/test_api.py -v`

**Step 4: Commit**

```bash
git add app/main.py
git commit -m "fix: proper 413 response for chunked body exceeding limit"
```

---

### Task 9: Fix rerank regex and error handling

**Files:**
- Modify: `app/engine/rag.py:508-513`
- Modify: `tests/test_rag.py` (add rerank tests)

**Step 1: Write failing tests**

```python
class TestRerankSingle:
    @pytest.mark.asyncio
    async def test_parses_plain_decimal(self):
        # Mock response returning "0.75"
        result = RAGEngine._parse_rerank_score("0.75")
        assert result == 0.75

    @pytest.mark.asyncio
    async def test_non_numeric_returns_none(self):
        result = RAGEngine._parse_rerank_score("Not relevant")
        assert result is None

    @pytest.mark.asyncio
    async def test_score_in_sentence(self):
        result = RAGEngine._parse_rerank_score("I'd rate this 0.85")
        assert result == 0.85
```

**Step 2: Extract score parsing and fix regex**

```python
@staticmethod
def _parse_rerank_score(text: str) -> float | None:
    """Parse a relevance score from LLM response text."""
    text = text.strip()
    # Try to find a decimal between 0 and 1
    match = re.search(r"\b(0(?:\.\d+)?|1(?:\.0+)?)\b", text)
    if match:
        return float(match.group(1))
    # Try parsing the whole text as a float
    try:
        val = float(text)
        if 0.0 <= val <= 1.0:
            return val
    except ValueError:
        pass
    return None
```

Update `_rerank_single` to use the helper and return original score on failure:

```python
    text = data["choices"][0]["message"]["content"].strip()
    score = RAGEngine._parse_rerank_score(text)
    if score is not None:
        return score
    logger.warning("Could not parse rerank score from LLM response: %r", text)
    return candidate["score"]  # Keep original
```

**Step 3: Run tests**

Run: `pytest tests/test_rag.py -v`

**Step 4: Commit**

```bash
git add app/engine/rag.py tests/test_rag.py
git commit -m "fix: robust rerank score parsing, fallback to original score"
```

---

### Task 10: Config validation and CORS fix

**Files:**
- Modify: `app/config.py:6-12,17,37,46`
- Modify: `tests/test_models.py` (add config tests)

**Step 1: Add validators and fix CORS default**

```python
from pydantic import field_validator

class Settings(BaseSettings):
    # ...
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:8080"]
    # ...

    @field_validator("CONTENT_HASH_LENGTH")
    @classmethod
    def validate_hash_length(cls, v: int) -> int:
        if v < 16:
            raise ValueError("CONTENT_HASH_LENGTH must be >= 16 to avoid collisions")
        return v
```

Add startup warning for empty secrets (in lifespan or config):

```python
    def model_post_init(self, __context):
        import logging
        _log = logging.getLogger("app.config")
        if not self.NEO4J_PASSWORD:
            _log.warning("NEO4J_PASSWORD is empty — set via environment or .env")
        if not self.LLM_API_KEY:
            _log.warning("LLM_API_KEY is empty — set via environment or .env")
```

**Step 2: Run tests**

Run: `pytest tests/ -v`

**Step 3: Commit**

```bash
git add app/config.py tests/test_models.py
git commit -m "fix: CORS wildcard, config validation, secret warnings"
```

---

### Task 11: Celery beat separation in docker-compose

**Files:**
- Modify: `docker-compose.yml:95-112`

**Step 1: Split celery-worker into worker + beat**

```yaml
  celery-worker:
    build:
      context: .
      dockerfile: Dockerfile
    restart: unless-stopped
    command: celery -A app.workers.sleep_cycle worker --loglevel=info
    env_file:
      - .env
    depends_on:
      redis:
        condition: service_healthy
      neo4j:
        condition: service_healthy
    deploy:
      resources:
        limits:
          memory: 512m
          cpus: "0.5"

  celery-beat:
    build:
      context: .
      dockerfile: Dockerfile
    restart: unless-stopped
    command: celery -A app.workers.sleep_cycle beat --loglevel=info
    env_file:
      - .env
    depends_on:
      redis:
        condition: service_healthy
    deploy:
      resources:
        limits:
          memory: 128m
          cpus: "0.1"
```

**Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "infra: separate Celery worker and beat processes"
```

---

### Task 12: Fix MCP server httpx client shutdown

**Files:**
- Modify: `app/mcp_server.py:226-242`

**Step 1: Fix _shutdown_client to not create fire-and-forget tasks**

```python
def _shutdown_client() -> None:
    """Close the httpx client on process exit."""
    global _client
    if _client is not None and not _client.is_closed:
        try:
            loop = asyncio.get_event_loop()
            if not loop.is_running():
                loop.run_until_complete(_client.aclose())
            # If loop is running, we can't safely close async client from atexit.
            # The process is exiting anyway — let the OS reclaim the socket.
        except RuntimeError:
            pass
        _client = None
```

**Step 2: Commit**

```bash
git add app/mcp_server.py
git commit -m "fix: MCP server httpx client shutdown leak"
```

---

### Task 13: Narrow fulltext fallback exception handling

**Files:**
- Modify: `app/db/graph.py:528-539`

**Step 1: Narrow the except clause**

```python
        try:
            async with driver.session() as session:
                result = await session.run(
                    query, search_terms=search_terms, limit=limit
                )
                return [dict(record) async for record in result]
        except neo4j.exceptions.ClientError:
            # Fulltext index may not exist — fall back gracefully
            logger.debug("Fulltext index not available, falling back to CONTAINS search")
            return None
```

Import `neo4j.exceptions` is already available since `import neo4j` is at the top of the file.

**Step 2: Run tests**

Run: `pytest tests/test_graph.py -v`

**Step 3: Commit**

```bash
git add app/db/graph.py
git commit -m "fix: narrow fulltext fallback to ClientError only"
```

---

### Task 14: Add public health methods to VectorClient and Neo4jClient

**Files:**
- Modify: `app/db/vector.py` (add `ping` and `memory_count`)
- Modify: `app/db/graph.py` (add `ping`)
- Modify: `app/main.py:370-401` (use public methods)

**Step 1: Add public methods to VectorClient**

```python
    async def ping(self) -> None:
        """Check connectivity by listing collections."""
        await self._client.get_collections()

    async def memory_count(self) -> int | None:
        """Return the number of points in the collection, or None on error."""
        try:
            info = await self._client.get_collection(self._collection)
            return info.points_count
        except Exception:
            return None
```

**Step 2: Add public ping to Neo4jClient**

```python
    async def ping(self) -> None:
        """Verify Neo4j connectivity."""
        driver = self._ensure_driver()
        await driver.verify_connectivity()
```

**Step 3: Update health endpoint**

Replace the private attribute access in `main.py` health endpoint:

```python
    # Neo4j
    try:
        await graph.ping()
        services["graph"] = ServiceStatus(status="connected")
    except Exception as exc:
        ...

    # Qdrant
    try:
        await vector.ping()
        services["qdrant"] = ServiceStatus(status="connected")
    except Exception as exc:
        ...

    # Memory count
    memory_count = await vector.memory_count()
```

**Step 4: Update tests** (health tests use mock_graph.ping / mock_vector.ping instead of _ensure_driver / _client)

**Step 5: Run tests**

Run: `pytest tests/test_api.py -v`

**Step 6: Commit**

```bash
git add app/db/vector.py app/db/graph.py app/main.py tests/test_api.py
git commit -m "refactor: public ping/memory_count methods, remove private access in health"
```

---

### Task 15: Add feedback endpoint tests and cache settings in middleware

**Files:**
- Modify: `tests/test_api.py` (add feedback tests)
- Modify: `app/main.py:167-172` (cache settings in middleware)

**Step 1: Add feedback endpoint tests**

```python
class TestMemoryFeedback:
    def test_feedback_success(self, test_client, mock_vector):
        mock_qdrant = AsyncMock()
        mock_qdrant.set_payload = AsyncMock()
        mock_vector._client = mock_qdrant
        mock_vector._collection = "test"

        resp = test_client.post(
            "/memory/feedback",
            json={"memory_ids": ["id1", "id2"], "useful": True, "comment": "helpful"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "recorded"
        assert body["updated"] == 2

    def test_feedback_partial_failure(self, test_client, mock_vector):
        mock_qdrant = AsyncMock()
        call_count = 0
        async def set_payload_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("fail")
        mock_qdrant.set_payload = AsyncMock(side_effect=set_payload_side_effect)
        mock_vector._client = mock_qdrant
        mock_vector._collection = "test"

        resp = test_client.post(
            "/memory/feedback",
            json={"memory_ids": ["id1", "id2"], "useful": False},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1

    def test_feedback_missing_fields(self, test_client):
        resp = test_client.post("/memory/feedback", json={})
        assert resp.status_code == 422
```

**Step 2: Cache settings in APIKeyMiddleware**

```python
class APIKeyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._settings = get_settings()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if self._settings.API_KEY is not None:
                # ... use self._settings.API_KEY ...
```

**Step 3: Run tests**

Run: `pytest tests/test_api.py -v`

**Step 4: Commit**

```bash
git add app/main.py tests/test_api.py
git commit -m "test: add feedback endpoint coverage, cache middleware settings"
```

---

## Task Dependency Graph

```
Task 1  (start.sh)          — independent
Task 2  (docker ports)       — independent
Task 3  (RAGEngine singleton)— independent
Task 4  (LRU cache)          — independent
Task 5  (sleep cycle tx)     — independent
Task 6  (learn partial fail) — independent
Task 7  (case-insensitive)   — independent
Task 8  (ASGI 413)           — independent
Task 9  (rerank regex)       — after Task 3
Task 10 (config validation)  — independent
Task 11 (celery beat split)  — after Task 2
Task 12 (MCP shutdown)       — independent
Task 13 (fulltext exception) — independent
Task 14 (public health)      — independent
Task 15 (feedback tests)     — independent
```

Most tasks are fully independent and can be parallelized.
