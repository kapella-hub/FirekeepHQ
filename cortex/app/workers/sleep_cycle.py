"""Sleep Cycle worker — background Celery task for cognitive consolidation.

Pops raw event batches from Redis, sends them through an LLM for knowledge
extraction, and writes the resulting nodes/edges to Neo4j.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
from datetime import timedelta
from typing import Any

import httpx
import redis
from celery import Celery
from neo4j import GraphDatabase

from app.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Celery app — settings loaded lazily via get_settings()
# ---------------------------------------------------------------------------


def _create_celery_app() -> Celery:
    """Create and configure the Celery app with settings loaded at call time."""
    s = get_settings()
    _app = Celery(
        "firekeep_cortex",
        broker=s.CELERY_BROKER_URL,
        backend=s.CELERY_RESULT_BACKEND,
    )
    _app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        include=[
            "app.workers.gc",
            "app.workers.memory_agent",
            "app.workers.reembed",
            "app.workers.migrate_namespaces",
            "app.workers.skill_synthesis",
            "app.workers.agent_gateway_sweep",
            "app.workers.backfill",
            "app.collectors.confluence",
            "app.owm",
            "app.dreams.task",
        ],
        beat_schedule={
            "sleep-cycle-consolidation": {
                "task": "app.workers.sleep_cycle.process_event_batch",
                "schedule": 60.0,
            },
            "memory-gc": {
                "task": "app.workers.gc.prune_memories",
                "schedule": timedelta(hours=s.GC_SCHEDULE_HOURS),
            },
            "memory-agent": {
                "task": "app.workers.memory_agent.run_memory_agent",
                "schedule": timedelta(hours=s.AGENT_SCHEDULE_HOURS),
            },
            "skill-synthesis-pass": {
                "task": "app.workers.skill_synthesis.skill_synthesis_pass",
                "schedule": timedelta(hours=s.SKILL_AGENT_SCHEDULE_HOURS),
            },
            "agent-gateway-sweep": {
                "task": "app.workers.agent_gateway_sweep.sweep_overdue_actions_task",
                "schedule": 60.0,
            },
            "memory-backfill-drain": {
                "task": "app.workers.backfill.drain_backfill_queue",
                "schedule": 60.0,
            },
            "confluence-collector": {
                "task": "app.collectors.confluence.run_confluence_collector",
                "schedule": timedelta(hours=s.CONFLUENCE_COLLECTOR_SCHEDULE_HOURS),
            },
            "owm-scoring": {
                "task": "app.owm.run_owm_scoring",
                "schedule": timedelta(hours=s.OWM_SCHEDULE_HOURS),
            },
            "dream-tick": {
                "task": "app.dreams.task.run_dream_tick",
                "schedule": timedelta(minutes=s.DREAM_TICK_MINUTES),
            },
        },
    )
    return _app


celery_app = _create_celery_app()

# ---------------------------------------------------------------------------
# System prompt — used verbatim for LLM knowledge extraction
# ---------------------------------------------------------------------------

CONSOLIDATION_SYSTEM_PROMPT = (
    "You are the cognitive consolidation engine for Firekeep Cortex. "
    "Your job is to analyze the following batch of raw logs, errors, or events, "
    "deduplicate the noise, and extract a structured Knowledge Graph.\n\n"
    "## Node Type Definitions\n"
    "- **Concept**: A technical term, abstraction, or domain entity "
    "(e.g. a class name, protocol, algorithm, configuration key).\n"
    "- **Action**: Something that was done — a function call, deployment, "
    "configuration change, or user operation.\n"
    "- **Outcome**: The result of an action — success, failure, error message, "
    "performance metric, or observable behavior.\n"
    "- **Resolution**: A fix applied to address a negative outcome — a code change, "
    "config update, rollback, or workaround.\n\n"
    "## Edge Type Definitions\n"
    "- **RELATES_TO**: General association between two concepts.\n"
    "- **CAUSED**: An action or event that led to an outcome.\n"
    "- **RESOLVED_BY**: An outcome (typically negative) that was fixed by a resolution.\n"
    "- **UTILIZES**: An action or resolution that uses a concept or tool.\n\n"
    "## Naming Conventions\n"
    "- Use lowercase_underscore for all node IDs (e.g. \"redis_connection_pool\", "
    "\"deploy_staging\", \"timeout_error\").\n"
    "- Be consistent — use the same ID for the same concept across events.\n"
    "- Keep descriptions concise but informative.\n\n"
    "## Memory Type Classification\n"
    "For each node, set a 'memory_type' property in its properties object:\n"
    "- **reference**: General knowledge, patterns, or stable facts that remain valid indefinitely.\n"
    "- **procedural**: Workflows, processes, or how-to knowledge (e.g. deployment steps, config procedures).\n"
    "- **episodic**: Specific events, incidents, or one-time occurrences (default if unclear).\n"
    "- **transient**: Temporary or work-in-progress context that will become stale quickly.\n\n"
    "## Output Format\n"
    "You must return ONLY a valid JSON object. "
    "No markdown fencing, no explanations. Output valid JSON only.\n\n"
    "Example:\n"
    "{\n"
    '  "nodes": [\n'
    '    {"id": "redis_connection_pool", "label": "Concept", '
    '"properties": {"description": "Redis connection pooling mechanism", "memory_type": "reference"}},\n'
    '    {"id": "increase_pool_size", "label": "Action", '
    '"properties": {"description": "Increased Redis pool size from 10 to 50", "memory_type": "procedural"}},\n'
    '    {"id": "connection_timeout_resolved", "label": "Outcome", '
    '"properties": {"description": "Connection timeouts stopped after pool resize", "memory_type": "episodic"}},\n'
    '    {"id": "pool_size_fix", "label": "Resolution", '
    '"properties": {"description": "Set REDIS_POOL_SIZE=50 in production config", "memory_type": "procedural"}}\n'
    "  ],\n"
    '  "edges": [\n'
    '    {"source": "increase_pool_size", "target": "redis_connection_pool", '
    '"type": "UTILIZES"},\n'
    '    {"source": "increase_pool_size", "target": "connection_timeout_resolved", '
    '"type": "CAUSED"},\n'
    '    {"source": "connection_timeout_resolved", "target": "pool_size_fix", '
    '"type": "RESOLVED_BY"}\n'
    "  ]\n"
    "}"
)

# ---------------------------------------------------------------------------
# Cached Neo4j driver — reused across batch calls
# ---------------------------------------------------------------------------

_neo4j_driver = None
_neo4j_driver_lock = threading.Lock()


def _get_neo4j_driver():
    """Return a module-level Neo4j driver, creating it on first call."""
    global _neo4j_driver
    if _neo4j_driver is not None:
        return _neo4j_driver
    with _neo4j_driver_lock:
        if _neo4j_driver is None:
            s = get_settings()
            _neo4j_driver = GraphDatabase.driver(
                s.NEO4J_URI,
                auth=(s.NEO4J_USER, s.NEO4J_PASSWORD),
                max_connection_pool_size=s.NEO4J_POOL_SIZE,
            )
            atexit.register(_close_neo4j_driver)
    return _neo4j_driver


def _close_neo4j_driver():
    """Close the cached Neo4j driver on process exit."""
    global _neo4j_driver
    if _neo4j_driver is not None:
        _neo4j_driver.close()
        _neo4j_driver = None


# ---------------------------------------------------------------------------
# Cached Redis client — reused across batch calls
# ---------------------------------------------------------------------------

_redis_client: redis.Redis | None = None


def _get_redis_client() -> redis.Redis:
    """Return a module-level Redis client, creating it on first call."""
    global _redis_client
    if _redis_client is None:
        s = get_settings()
        _redis_client = redis.from_url(s.REDIS_URL, decode_responses=True)
        atexit.register(_close_redis_client)
    return _redis_client


def _close_redis_client():
    """Close the cached Redis client on process exit."""
    global _redis_client
    if _redis_client is not None:
        _redis_client.close()
        _redis_client = None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only nodes that have required fields."""
    valid = []
    for node in nodes:
        if (
            isinstance(node, dict)
            and isinstance(node.get("id"), str)
            and isinstance(node.get("label"), str)
            and isinstance(node.get("properties"), dict)
        ):
            valid.append(node)
        else:
            logger.warning("Dropping invalid node: %s", node)
    return valid


def _validate_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only edges that have required fields."""
    valid = []
    for edge in edges:
        if (
            isinstance(edge, dict)
            and isinstance(edge.get("source"), str)
            and isinstance(edge.get("target"), str)
            and isinstance(edge.get("type"), str)
        ):
            valid.append(edge)
        else:
            logger.warning("Dropping invalid edge: %s", edge)
    return valid


# ---------------------------------------------------------------------------
# Neo4j sync write — mirrors Neo4jClient.merge_knowledge_nodes but sync
# ---------------------------------------------------------------------------

def _write_to_neo4j(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> int:
    """Write knowledge nodes and edges using a synchronous Neo4j session."""
    count = 0
    driver = _get_neo4j_driver()
    with driver.session() as session:
        with session.begin_transaction() as tx:
            # Merge nodes grouped by label
            if nodes:
                label_groups: dict[str, list[dict[str, Any]]] = {}
                for node in nodes:
                    label = node.get("label", "Entity")
                    label_groups.setdefault(label, []).append(node)

                for label, group in label_groups.items():
                    safe_label = "".join(
                        c for c in label if c.isalnum() or c == "_"
                    )
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

            # Merge edges grouped by relationship type
            if edges:
                rel_groups: dict[str, list[dict[str, Any]]] = {}
                for edge in edges:
                    rel_type = edge.get("type", "RELATED_TO")
                    rel_groups.setdefault(rel_type, []).append(edge)

                for rel_type, group in rel_groups.items():
                    safe_type = "".join(
                        c for c in rel_type if c.isalnum() or c == "_"
                    )
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

    return count


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.workers.sleep_cycle.process_event_batch",
    bind=True,
    max_retries=3,
)
def process_event_batch(self) -> dict[str, Any]:
    """Pop a batch of events from Redis, extract knowledge via LLM, write to Neo4j."""
    try:
        return _process_batch()
    except httpx.HTTPError as e:
        logger.warning("Transient LLM failure, retrying: %s", e)
        raise self.retry(exc=e, countdown=60, max_retries=3)
    except Exception:
        logger.exception("Unhandled error in process_event_batch")
        return {"status": "error", "nodes": 0, "edges": 0}


def _process_batch() -> dict[str, Any]:
    """Core batch processing logic, separated for clarity."""
    settings = get_settings()
    redis_client = _get_redis_client()

    # Pop up to REDIS_BATCH_SIZE items using a pipeline for efficiency
    pipe = redis_client.pipeline()
    for _ in range(settings.REDIS_BATCH_SIZE):
        pipe.rpop(settings.REDIS_STREAM_KEY)
    results = pipe.execute()
    raw_items: list[str] = [r for r in results if r is not None]

    if not raw_items:
        return {"status": "empty", "nodes": 0, "edges": 0}

    # Parse events and build batch text
    events: list[dict[str, Any]] = []
    for raw in raw_items:
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed event JSON: %.200s", raw)
            events.append({"raw": raw})

    # Truncate individual event payloads to limit LLM prompt injection surface
    _MAX_EVENT_CHARS = 2000
    truncated_events = []
    for evt in events:
        serialized = json.dumps(evt, default=str)
        if len(serialized) > _MAX_EVENT_CHARS:
            serialized = serialized[:_MAX_EVENT_CHARS] + "...(truncated)"
        truncated_events.append(serialized)

    batch_text = "\n---\n".join(truncated_events)

    # Call LLM
    try:
        llm_response = httpx.post(
            f"{settings.LLM_BASE_URL}/chat/completions",
            json={
                "model": settings.LLM_MODEL,
                "messages": [
                    {"role": "system", "content": CONSOLIDATION_SYSTEM_PROMPT},
                    {"role": "user", "content": batch_text},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"} if settings.LLM_API_KEY else {},
            timeout=120.0,
        )
        llm_response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("LLM API call failed: %s", exc)
        _send_to_dlq(redis_client, raw_items, settings)
        return {"status": "llm_error", "nodes": 0, "edges": 0}

    # Parse LLM JSON
    try:
        msg = llm_response.json()["choices"][0]["message"]
        content = msg.get("content") or ""
        # Fallback: some models (e.g. qwen3) put output in a reasoning field
        if not content.strip():
            content = msg.get("reasoning") or ""
        knowledge = json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        logger.error("Failed to parse LLM response: %s", exc)
        _send_to_dlq(redis_client, raw_items, settings)
        return {"status": "parse_error", "nodes": 0, "edges": 0}

    # Validate structure
    raw_nodes = knowledge.get("nodes", [])
    raw_edges = knowledge.get("edges", [])

    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        logger.error("LLM returned invalid structure: nodes/edges not lists")
        _send_to_dlq(redis_client, raw_items, settings)
        return {"status": "validation_error", "nodes": 0, "edges": 0}

    nodes = _validate_nodes(raw_nodes)
    edges = _validate_edges(raw_edges)

    if not nodes and not edges:
        logger.warning("No valid nodes or edges extracted from batch")
        return {"status": "empty_extraction", "nodes": 0, "edges": 0}

    # Write to Neo4j
    try:
        written = _write_to_neo4j(nodes, edges)
    except Exception as exc:
        logger.error("Neo4j write failed: %s", exc)
        _send_to_dlq(redis_client, raw_items, settings)
        return {"status": "neo4j_error", "nodes": 0, "edges": 0}

    logger.info(
        "Sleep Cycle processed batch: %d events -> %d nodes, %d edges (%d written)",
        len(events),
        len(nodes),
        len(edges),
        written,
    )
    return {
        "status": "ok",
        "nodes": len(nodes),
        "edges": len(edges),
        "written": written,
    }


def _send_to_dlq(
    redis_client: redis.Redis,
    items: list[str],
    settings: Any | None = None,
) -> None:
    """Push failed batch items to the dead-letter queue."""
    if settings is None:
        settings = get_settings()
    dlq_key = f"{settings.REDIS_STREAM_KEY}:dlq"
    try:
        for item in items:
            redis_client.lpush(dlq_key, item)
        # Cap DLQ size
        redis_client.ltrim(dlq_key, 0, settings.DLQ_MAX_SIZE - 1)
        logger.info("Sent %d items to DLQ (%s)", len(items), dlq_key)
    except Exception:
        logger.exception("Failed to send items to DLQ")
