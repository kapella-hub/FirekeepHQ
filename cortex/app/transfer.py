"""Transfer router for FirekeepCortex — export/import API."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.models import ImportResponse
from auth.middleware import require_scope

if TYPE_CHECKING:
    from app.db.graph import Neo4jClient
    from app.db.vector import VectorClient

logger = logging.getLogger(__name__)

_MAX_IMPORT_ITEMS = 10000


def create_transfer_router(graph: Neo4jClient, vector: VectorClient) -> APIRouter:
    """Create the transfer router with injected dependencies."""
    router = APIRouter(prefix="/memory", tags=["transfer"])

    @router.get("/export")
    async def memory_export(
        namespace: str | None = Query(default=None, description="Filter by namespace"),
        format: str = Query(default="jsonl", description="Export format (jsonl)"),
        identity: dict = Depends(require_scope("admin")),
    ) -> StreamingResponse:
        """Export all memories as JSONL stream. Admin-scoped: this streams every
        memory in the deployment, so `memory:read` — which every agent key holds —
        is not a sufficient gate for it."""

        async def _generate() -> AsyncIterator[str]:
            # Export vector memories
            async for point in vector.scroll_all(namespace=namespace):
                line = {
                    "type": "memory",
                    "text": point.get("text", ""),
                    "metadata": point.get("metadata", {}),
                    "namespace": point.get("namespace", "default"),
                    "tags": point.get("tags", []),
                    "source": point.get("source", ""),
                    "created_at": point.get("created_at", ""),
                }
                yield json.dumps(line, default=str) + "\n"

            # Export graph data (nodes and edges)
            graph_data = await graph.export_graph()
            for node in graph_data.get("nodes", []):
                line = {
                    "type": "node",
                    "id": node.get("id", ""),
                    "label": node.get("label", ""),
                    "properties": node.get("properties", {}),
                }
                yield json.dumps(line, default=str) + "\n"

            for edge in graph_data.get("edges", []):
                line = {
                    "type": "edge",
                    "source": edge.get("source", ""),
                    "target": edge.get("target", ""),
                    "rel_type": edge.get("type", ""),
                }
                yield json.dumps(line, default=str) + "\n"

        return StreamingResponse(
            _generate(),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": "attachment; filename=firekeep-export.jsonl"},
        )

    @router.post("/import", response_model=ImportResponse)
    async def memory_import(
        request: Request,
        identity: dict = Depends(require_scope("admin")),
    ) -> ImportResponse:
        """Import memories from JSONL or JSON array. Admin-scoped: this bulk-writes
        memories AND arbitrary Neo4j labels and relationship types — `label` and
        `rel_type` are passed through to `merge_knowledge_nodes` verbatim — so it can
        author graph structure no other route exposes."""
        content_type = request.headers.get("content-type", "")
        body = await request.body()

        if not body:
            raise HTTPException(status_code=422, detail="Empty request body")

        # Parse items from body
        items: list[dict] = []
        errors: list[str] = []

        body_text = body.decode("utf-8", errors="replace")

        if "application/x-ndjson" in content_type or "ndjson" in content_type:
            # Parse JSONL
            for i, line in enumerate(body_text.strip().split("\n")):
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    errors.append(f"Line {i + 1}: invalid JSON: {exc}")
        else:
            # Try JSON array first, then JSONL
            try:
                parsed = json.loads(body_text)
                if isinstance(parsed, list):
                    items = parsed
                elif isinstance(parsed, dict):
                    items = [parsed]
                else:
                    raise HTTPException(status_code=422, detail="Expected JSON array or JSONL")
            except json.JSONDecodeError:
                # Fallback to JSONL
                for i, line in enumerate(body_text.strip().split("\n")):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        errors.append(f"Line {i + 1}: invalid JSON: {exc}")

        if len(items) > _MAX_IMPORT_ITEMS:
            raise HTTPException(
                status_code=422,
                detail=f"Import exceeds maximum of {_MAX_IMPORT_ITEMS} items",
            )

        imported_memories = 0
        imported_nodes = 0
        imported_edges = 0

        # Collect graph items for batch processing
        graph_nodes: list[dict] = []
        graph_edges: list[dict] = []

        for idx, item in enumerate(items):
            item_type = item.get("type", "memory")

            if item_type == "memory":
                text = item.get("text", "")
                if not text:
                    errors.append(f"Item {idx}: memory has no text")
                    continue
                try:
                    metadata = item.get("metadata", {})
                    metadata["domain"] = metadata.get("domain") or item.get("domain", "general")
                    metadata["tags"] = item.get("tags", metadata.get("tags", []))
                    metadata["source"] = item.get("source", metadata.get("source", "import"))
                    metadata["timestamp"] = item.get("created_at", "")
                    ns = item.get("namespace", "default")
                    await vector.upsert(text=text, metadata=metadata, namespace=ns)
                    imported_memories += 1
                except Exception as exc:
                    errors.append(f"Item {idx}: failed to import memory: {exc}")

            elif item_type == "node":
                node_id = item.get("id")
                label = item.get("label", "Entity")
                properties = item.get("properties", {})
                if not node_id:
                    errors.append(f"Item {idx}: node has no id")
                    continue
                graph_nodes.append({
                    "id": node_id,
                    "label": label,
                    "properties": properties,
                })

            elif item_type == "edge":
                source = item.get("source")
                target = item.get("target")
                rel_type = item.get("rel_type") or item.get("edge_type", "RELATED_TO")
                if not source or not target:
                    errors.append(f"Item {idx}: edge missing source or target")
                    continue
                graph_edges.append({
                    "source": source,
                    "target": target,
                    "type": rel_type,
                })

            else:
                errors.append(f"Item {idx}: unknown type '{item_type}'")

        # Batch merge graph data
        if graph_nodes or graph_edges:
            try:
                await graph.merge_knowledge_nodes(
                    nodes=graph_nodes,
                    edges=graph_edges,
                )
                imported_nodes = len(graph_nodes)
                imported_edges = len(graph_edges)
            except Exception as exc:
                errors.append(f"Graph import failed: {exc}")

        return ImportResponse(
            status="completed",
            imported_memories=imported_memories,
            imported_nodes=imported_nodes,
            imported_edges=imported_edges,
            errors=errors,
        )

    return router
