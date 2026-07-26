# Namespace Normalization & Health Cache Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate namespace fragmentation by normalizing all incoming namespaces, cache health check responses to reduce backend load by ~35K calls/day, and add a one-time migration task to consolidate existing duplicate namespaces.

**Architecture:** A shared `normalize_namespace()` function applied via Pydantic `field_validator` on all 3 request models. Health caching uses a module-level dict with TTL. Migration is a Celery task that updates both Qdrant payloads and Neo4j Namespace nodes. Response models are intentionally NOT normalized — they reflect what was stored, not what the user typed.

**Tech Stack:** Pydantic validators, FastAPI, Celery, Neo4j (Cypher), Qdrant (scroll + set_payload)

**Trade-offs:**
- Health cache returns stale `uptime_seconds` and status for up to 10s — acceptable for a health check.
- Neo4j migration is idempotent (safe to re-run) but not transactional per-group — partial completion is handled by re-running.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `app/models.py` | Modify | Add `normalize_namespace()` + `field_validator` on 3 request models |
| `app/main.py` | Modify | Add health response TTL cache (~15 lines) |
| `app/workers/migrate_namespaces.py` | Create | One-time Celery task to consolidate namespaces |
| `tests/test_models.py` | Modify | Add namespace normalization tests |
| `tests/test_api.py` | Modify | Add health cache tests |
| `tests/test_migrate_namespaces.py` | Create | Tests for migration task |

---

## Chunk 1: Namespace Normalization

### Task 1: Namespace Normalization Function + Model Validators

**Files:**
- Modify: `app/models.py:1-103`
- Modify: `tests/test_models.py:363-439`

- [ ] **Step 1: Write failing tests for namespace normalization**

Add to `tests/test_models.py` in the `TestNamespaceValidation` class:

```python
# --- Add these tests to class TestNamespaceValidation ---

def test_namespace_hyphens_normalized_to_underscores(self):
    """Hyphens in namespace should be normalized to underscores."""
    q = ContextQuery(task="test", namespace="automation-portal")
    assert q.namespace == "automation_portal"

def test_namespace_uppercase_normalized_to_lowercase(self):
    """Uppercase namespace should be normalized to lowercase."""
    q = ContextQuery(task="test", namespace="AutomationPortal")
    assert q.namespace == "automationportal"

def test_namespace_mixed_case_hyphens_normalized(self):
    """Mixed case with hyphens should be fully normalized."""
    q = ContextQuery(task="test", namespace="My-Agent-1")
    assert q.namespace == "my_agent_1"

def test_namespace_normalization_on_action_log(self):
    """ActionLog namespace should be normalized."""
    log = ActionLog(action="a", outcome="o", namespace="Tenant-B")
    assert log.namespace == "tenant_b"

def test_namespace_normalization_on_event_ingest(self):
    """GenericEventIngest namespace should be normalized."""
    event = GenericEventIngest(source="s", payload={}, namespace="Tenant-C")
    assert event.namespace == "tenant_c"

def test_namespace_default_unchanged(self):
    """Default namespace 'default' should pass through unchanged."""
    q = ContextQuery(task="test")
    assert q.namespace == "default"

def test_namespace_already_normalized_unchanged(self):
    """Already normalized namespace should pass through unchanged."""
    q = ContextQuery(task="test", namespace="my_agent_1")
    assert q.namespace == "my_agent_1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_models.py::TestNamespaceValidation -v`
Expected: 5 FAIL (hyphens, uppercase, mixed, action_log, event_ingest), 2 PASS (default, already_normalized)

- [ ] **Step 3: Implement namespace normalization in models.py**

Add the `normalize_namespace` function and `field_validator` to all 3 request models in `app/models.py`:

```python
# Add to imports (line 6):
from pydantic import BaseModel, Field, field_validator, model_validator

# Add after imports, before ContextQuery class (around line 8):

def normalize_namespace(ns: str) -> str:
    """Normalize a namespace: lowercase, hyphens to underscores."""
    return ns.lower().strip().replace("-", "_")
```

Then add a `field_validator` to each of the 3 request models (`ContextQuery`, `ActionLog`, `GenericEventIngest`):

```python
@field_validator("namespace")
@classmethod
def _normalize_namespace(cls, v: str) -> str:
    return normalize_namespace(v)
```

For `ContextQuery`, add the validator after `model_config`.
For `ActionLog`, add the validator after `model_config`.
For `GenericEventIngest`, add it after the existing `_validate_payload_size` validator.

Note: The `strip()` in `normalize_namespace` is effectively dead code at the API level (the `pattern` regex rejects whitespace before the validator runs), but it's useful for the migration task which calls this function directly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_models.py::TestNamespaceValidation -v`
Expected: ALL PASS

- [ ] **Step 5: Fix broken existing tests**

Some existing tests expect un-normalized namespaces (e.g., `namespace="my-agent-1"` should now become `"my_agent_1"`). Update:

In `tests/test_models.py`:
- `test_valid_namespace_with_hyphens`: assert should be `"my_agent_1"` not `"my-agent-1"`
- `test_namespace_on_all_request_models`: `"tenant-A"` becomes `"tenant_a"`, `"tenant-B"` -> `"tenant_b"`, `"tenant-C"` -> `"tenant_c"`
- `test_serialization_roundtrip` in `TestContextQuery`: the expected dict should have normalized namespace

Note: Response model tests (`test_recall_response_custom_namespace`, `test_learn_response_custom_namespace`) should NOT change — response models intentionally preserve the stored namespace as-is.

Run: `pytest tests/test_models.py -v`
Expected: ALL PASS

- [ ] **Step 6: Run full test suite to check for regressions**

Run: `pytest tests/ -v`
Expected: ALL PASS (some tests may need namespace assertion updates)

- [ ] **Step 7: Commit**

```bash
git add app/models.py tests/test_models.py
git commit -m "feat: normalize namespaces on all request models (lowercase, hyphens to underscores)"
```

---

## Chunk 2: Health Check Caching

### Task 2: Add TTL Cache to Health Endpoint

**Files:**
- Modify: `app/main.py:417-468`
- Modify: `tests/test_api.py:22-80`

- [ ] **Step 1: Write failing test for health cache behavior**

Add to `tests/test_api.py` in the `TestHealthEndpoint` class:

```python
def test_health_caches_response(self, test_client, mock_graph, mock_vector, mock_redis):
    """Second health call within TTL should not re-probe backends."""
    mock_graph.ping = AsyncMock()
    mock_vector.ping = AsyncMock()
    mock_vector.memory_count = AsyncMock(return_value=42)
    mock_redis.ping = AsyncMock()

    # First call — probes everything
    resp1 = test_client.get("/health")
    assert resp1.status_code == 200

    # Reset mocks to track second call
    mock_graph.ping.reset_mock()
    mock_vector.ping.reset_mock()
    mock_vector.memory_count.reset_mock()
    mock_redis.ping.reset_mock()

    # Second call — should use cache, no backend calls
    resp2 = test_client.get("/health")
    assert resp2.status_code == 200
    assert resp2.json() == resp1.json()

    mock_graph.ping.assert_not_called()
    mock_vector.ping.assert_not_called()
    mock_redis.ping.assert_not_called()

def test_health_cache_expires(self, test_client, mock_graph, mock_vector, mock_redis):
    """Health cache should expire after TTL."""
    from unittest.mock import patch
    from datetime import datetime, timezone, timedelta

    mock_graph.ping = AsyncMock()
    mock_vector.ping = AsyncMock()
    mock_vector.memory_count = AsyncMock(return_value=42)
    mock_redis.ping = AsyncMock()

    # First call
    test_client.get("/health")

    # Simulate time passing beyond TTL
    from app import main as main_module
    main_module._health_cache_time = datetime.now(timezone.utc) - timedelta(seconds=30)

    mock_graph.ping.reset_mock()

    # Third call — cache expired, should re-probe
    resp = test_client.get("/health")
    assert resp.status_code == 200
    mock_graph.ping.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py::TestHealthEndpoint::test_health_caches_response -v`
Expected: FAIL (no caching exists yet)

- [ ] **Step 3: Implement health cache in main.py**

Add module-level cache variables near the top of `app/main.py` (after imports, around line 65):

```python
# Health check cache (10-second TTL to reduce backend probing)
_health_cache: HealthResponse | None = None
_health_cache_time: datetime | None = None
_HEALTH_CACHE_TTL_SECONDS = 10.0
```

Then modify the `health()` endpoint (lines 417-468):

```python
@app.get("/health", response_model=HealthResponse)
async def health(
    graph: Annotated[Neo4jClient, Depends(get_graph)],
    vector: Annotated[VectorClient, Depends(get_vector)],
    redis_client: Annotated[redis.asyncio.Redis, Depends(get_redis)],
) -> HealthResponse:
    """Return service health status by probing each backend (cached for 10s)."""
    global _health_cache, _health_cache_time

    now = datetime.now(timezone.utc)
    if (
        _health_cache is not None
        and _health_cache_time is not None
        and (now - _health_cache_time).total_seconds() < _HEALTH_CACHE_TTL_SECONDS
    ):
        return _health_cache

    services: dict[str, ServiceStatus] = {}

    # Redis
    try:
        await redis_client.ping()
        services["redis"] = ServiceStatus(status="connected")
    except Exception as exc:
        logger.warning("Redis health check failed: %s", exc)
        services["redis"] = ServiceStatus(status="disconnected", detail="Service unreachable")

    # Neo4j
    try:
        await graph.ping()
        services["graph"] = ServiceStatus(status="connected")
    except Exception as exc:
        logger.warning("Neo4j health check failed: %s", exc)
        services["graph"] = ServiceStatus(status="disconnected", detail="Service unreachable")

    # Qdrant
    try:
        await vector.ping()
        services["qdrant"] = ServiceStatus(status="connected")
    except Exception as exc:
        logger.warning("Qdrant health check failed: %s", exc)
        services["qdrant"] = ServiceStatus(status="disconnected", detail="Service unreachable")

    # Uptime
    uptime_seconds: float | None = None
    try:
        start_time = app.state.start_time
        uptime_seconds = (now - start_time).total_seconds()
    except AttributeError:
        pass

    # Memory count from Qdrant
    memory_count = await vector.memory_count()

    all_connected = all(s.status == "connected" for s in services.values())
    result = HealthResponse(
        status="ok" if all_connected else "degraded",
        services=services,
        version="0.6.0",
        uptime_seconds=uptime_seconds,
        memory_count=memory_count,
    )

    _health_cache = result
    _health_cache_time = now
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_api.py::TestHealthEndpoint -v`
Expected: ALL PASS

Note: The cache is module-level, so tests that run in sequence within the same process may share state. Each test should either reset the cache or account for it. Add a fixture or reset in conftest if needed:

```python
# Add inside the test_client fixture in conftest.py, just before `yield client` (line 119):
from app import main as main_module
main_module._health_cache = None
main_module._health_cache_time = None
```

- [ ] **Step 5: Run full test suite**

Run: `pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_api.py tests/conftest.py
git commit -m "perf: cache health endpoint response for 10s to reduce backend probing"
```

---

## Chunk 3: Namespace Migration Task

### Task 3: One-Time Celery Migration Task

**Files:**
- Create: `app/workers/migrate_namespaces.py`
- Create: `tests/test_migrate_namespaces.py`
- Modify: `app/workers/sleep_cycle.py:43-46` (add to include list)

- [ ] **Step 1: Write failing test for migration logic**

Create `tests/test_migrate_namespaces.py`:

```python
"""Tests for namespace migration task."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest

from app.models import normalize_namespace
from app.workers.migrate_namespaces import migrate_namespaces


class TestNormalizeNamespace:
    """Tests for the shared normalize_namespace function from models.py."""

    def test_lowercase(self):
        assert normalize_namespace("AutomationPortal") == "automationportal"

    def test_hyphens_to_underscores(self):
        assert normalize_namespace("automation-portal") == "automation_portal"

    def test_mixed(self):
        assert normalize_namespace("My-Agent-1") == "my_agent_1"

    def test_already_normalized(self):
        assert normalize_namespace("my_agent") == "my_agent"

    def test_strips_whitespace(self):
        assert normalize_namespace("  my-ns  ") == "my_ns"


class TestMigrateNamespaces:
    @patch("app.workers.migrate_namespaces._migrate_qdrant")
    @patch("app.workers.migrate_namespaces._migrate_neo4j")
    def test_runs_both_migrations(self, mock_neo4j, mock_qdrant):
        """Task should migrate both Qdrant and Neo4j."""
        mock_qdrant.return_value = {"updated": 5, "errors": 0}
        mock_neo4j.return_value = {"merged": 3, "errors": 0}

        result = migrate_namespaces()

        mock_qdrant.assert_called_once()
        mock_neo4j.assert_called_once()
        assert result["status"] == "completed"
        assert result["qdrant"]["updated"] == 5
        assert result["neo4j"]["merged"] == 3

    @patch("app.workers.migrate_namespaces._migrate_qdrant")
    @patch("app.workers.migrate_namespaces._migrate_neo4j")
    def test_handles_qdrant_failure(self, mock_neo4j, mock_qdrant):
        """If Qdrant fails, Neo4j should still run."""
        mock_qdrant.side_effect = Exception("Qdrant down")
        mock_neo4j.return_value = {"merged": 2, "errors": 0}

        result = migrate_namespaces()

        assert result["status"] == "partial"
        assert "error" in result["qdrant"]
        assert result["neo4j"]["merged"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_migrate_namespaces.py -v`
Expected: FAIL (module does not exist)

- [ ] **Step 3: Implement migration task**

Create `app/workers/migrate_namespaces.py`:

```python
"""One-time namespace migration task.

Normalizes all existing namespace values in both Qdrant and Neo4j
to match the new convention: lowercase, hyphens to underscores.

Usage:
    celery -A app.workers.sleep_cycle call app.workers.migrate_namespaces.migrate_namespaces
"""

from __future__ import annotations

import logging
from typing import Any

from app.models import normalize_namespace
from app.workers.sleep_cycle import celery_app
from app.config import get_settings

logger = logging.getLogger(__name__)


def _migrate_qdrant() -> dict[str, Any]:
    """Scroll all Qdrant points and update namespace payloads."""
    from qdrant_client import QdrantClient

    settings = get_settings()
    client = QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
    collection = settings.QDRANT_COLLECTION

    updated = 0
    errors = 0
    offset = None

    while True:
        result = client.scroll(
            collection_name=collection,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points, next_offset = result

        if not points:
            break

        for point in points:
            payload = point.payload or {}
            old_ns = payload.get("namespace", "default")
            new_ns = normalize_namespace(old_ns)
            if old_ns != new_ns:
                try:
                    client.set_payload(
                        collection_name=collection,
                        payload={"namespace": new_ns},
                        points=[point.id],
                    )
                    updated += 1
                    logger.info("Qdrant: %s -> %s (point %s)", old_ns, new_ns, point.id)
                except Exception as exc:
                    errors += 1
                    logger.error("Qdrant update failed for %s: %s", point.id, exc)

        if next_offset is None:
            break
        offset = next_offset

    logger.info("Qdrant migration complete: %d updated, %d errors", updated, errors)
    return {"updated": updated, "errors": errors}


def _migrate_neo4j() -> dict[str, Any]:
    """Merge duplicate Namespace nodes in Neo4j."""
    from neo4j import GraphDatabase

    settings = get_settings()
    driver = GraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )

    merged = 0
    errors = 0

    with driver.session() as session:
        # Get all namespace nodes
        result = session.run("MATCH (ns:Namespace) RETURN ns.name AS name")
        namespaces = [record["name"] for record in result]

        # Group by normalized name
        groups: dict[str, list[str]] = {}
        for ns in namespaces:
            normalized = normalize_namespace(ns)
            groups.setdefault(normalized, []).append(ns)

        for normalized, originals in groups.items():
            if len(originals) <= 1 and originals[0] == normalized:
                continue  # Already correct, no merge needed

            try:
                # Rename single namespace node
                if len(originals) == 1:
                    session.run(
                        "MATCH (ns:Namespace {name: $old_name}) "
                        "SET ns.name = $new_name",
                        old_name=originals[0],
                        new_name=normalized,
                    )
                    merged += 1
                    logger.info("Neo4j: renamed %s -> %s", originals[0], normalized)
                else:
                    # Multiple duplicates: merge all edges into one canonical node
                    # Keep or create the canonical node
                    session.run(
                        "MERGE (canonical:Namespace {name: $canonical_name})",
                        canonical_name=normalized,
                    )
                    for original in originals:
                        if original == normalized:
                            continue
                        # Move CONTAINS edges from duplicate to canonical
                        session.run(
                            "MATCH (dup:Namespace {name: $dup_name})-[r:CONTAINS]->(target) "
                            "MATCH (canonical:Namespace {name: $canonical_name}) "
                            "MERGE (canonical)-[:CONTAINS]->(target) "
                            "DELETE r",
                            dup_name=original,
                            canonical_name=normalized,
                        )
                        # Delete the duplicate node (DETACH handles any remaining edges)
                        session.run(
                            "MATCH (dup:Namespace {name: $dup_name}) "
                            "DETACH DELETE dup",
                            dup_name=original,
                        )
                        merged += 1
                        logger.info("Neo4j: merged %s into %s", original, normalized)
            except Exception as exc:
                errors += 1
                logger.error("Neo4j merge failed for %s: %s", normalized, exc)

    driver.close()
    logger.info("Neo4j migration complete: %d merged, %d errors", merged, errors)
    return {"merged": merged, "errors": errors}


@celery_app.task(name="app.workers.migrate_namespaces.migrate_namespaces")
def migrate_namespaces() -> dict[str, Any]:
    """One-time task: normalize all namespace values in Qdrant and Neo4j.

    Safe to run multiple times (idempotent).
    """
    logger.info("Starting namespace migration...")
    result: dict[str, Any] = {}

    try:
        result["qdrant"] = _migrate_qdrant()
    except Exception as exc:
        logger.error("Qdrant migration failed: %s", exc)
        result["qdrant"] = {"error": str(exc)}

    try:
        result["neo4j"] = _migrate_neo4j()
    except Exception as exc:
        logger.error("Neo4j migration failed: %s", exc)
        result["neo4j"] = {"error": str(exc)}

    has_errors = (
        "error" in result.get("qdrant", {})
        or "error" in result.get("neo4j", {})
    )
    result["status"] = "partial" if has_errors else "completed"

    logger.info("Namespace migration finished: %s", result)
    return result
```

- [ ] **Step 4: Register task in Celery include list**

In `app/workers/sleep_cycle.py`, add `"app.workers.migrate_namespaces"` to the `include` list (line 43-46):

```python
include=[
    "app.workers.gc",
    "app.workers.memory_agent",
    "app.workers.reembed",
    "app.workers.migrate_namespaces",
],
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_migrate_namespaces.py -v`
Expected: ALL PASS

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add app/workers/migrate_namespaces.py tests/test_migrate_namespaces.py app/workers/sleep_cycle.py
git commit -m "feat: add one-time namespace migration task for Qdrant + Neo4j consolidation"
```

---

## Post-Implementation

### Running the Migration

After deploying, trigger the one-time migration:

```bash
# From any machine with Celery access:
celery -A app.workers.sleep_cycle call app.workers.migrate_namespaces.migrate_namespaces

# Or from Python:
from app.workers.migrate_namespaces import migrate_namespaces
migrate_namespaces.delay()
```

### Verification

After migration completes, check stats:

```bash
curl -s http://localhost:8100/memory/stats | python3 -m json.tool | grep -A 30 namespace_counts
```

Expected: Duplicate namespace keys (`automation-portal`, `automationportal`, `AutomationPortal`) should be consolidated into a single normalized key (`automation_portal`).

### CLAUDE.md Update

Add to the Key Design Decisions section:

```
- **Namespace normalization**: All incoming namespaces are normalized (lowercase, hyphens to underscores) via Pydantic model validators. Migration task consolidates pre-existing data.
- **Health check caching**: `/health` response is cached for 10 seconds to reduce backend probing (~35K fewer calls/day).
```
