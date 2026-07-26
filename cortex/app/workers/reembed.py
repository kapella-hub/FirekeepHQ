"""Re-embedding worker — Celery task that re-embeds all vectors using a new model.

Scrolls through all Qdrant points, generates new embeddings via the
configured (or overridden) embedding model, and updates vectors in place.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import PointVectors

from app.config import get_settings
from app.workers.sleep_cycle import celery_app

logger = logging.getLogger(__name__)


def _embed_texts_sync(
    texts: list[str],
    base_url: str,
    model: str,
    api_key: str,
) -> list[list[float]]:
    """Generate embeddings for a batch of texts using a synchronous HTTP call."""
    url = f"{base_url}/embeddings"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    response = httpx.post(
        url,
        json={"model": model, "input": texts},
        headers=headers,
        timeout=60.0,
    )
    response.raise_for_status()
    data = response.json()
    sorted_data = sorted(data["data"], key=lambda x: x["index"])
    return [item["embedding"] for item in sorted_data]


@celery_app.task(name="app.workers.reembed.reembed_all", bind=True)
def reembed_all(self, new_model: str | None = None, batch_size: int = 50) -> dict[str, Any]:
    """Re-embed all vectors using the current or specified embedding model.

    Reports progress via Celery task state updates.
    """
    settings = get_settings()
    model = new_model or settings.EMBEDDING_MODEL

    client = QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
    collection = settings.QDRANT_COLLECTION

    try:
        # Get total count
        collection_info = client.get_collection(collection)
        total = collection_info.points_count or 0

        if total == 0:
            return {"status": "completed", "reembedded": 0}

        self.update_state(state="PROGRESS", meta={"current": 0, "total": total})

        reembedded = 0
        offset = None

        while True:
            # Scroll through points in batches
            records, next_offset = client.scroll(
                collection_name=collection,
                limit=batch_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )

            if not records:
                break

            # Extract texts from payloads
            texts = []
            point_ids = []
            for record in records:
                text = record.payload.get("text", "") if record.payload else ""
                if text:
                    texts.append(text)
                    point_ids.append(record.id)

            # Generate new embeddings
            if texts:
                new_vectors = _embed_texts_sync(
                    texts,
                    base_url=settings.LLM_BASE_URL,
                    model=model,
                    api_key=settings.LLM_API_KEY,
                )

                # Update vectors in Qdrant
                points = [
                    PointVectors(id=pid, vector=vec)
                    for pid, vec in zip(point_ids, new_vectors)
                ]
                client.update_vectors(
                    collection_name=collection,
                    points=points,
                )

                reembedded += len(texts)

            self.update_state(
                state="PROGRESS",
                meta={"current": reembedded, "total": total},
            )

            if next_offset is None:
                break
            offset = next_offset

        return {"status": "completed", "reembedded": reembedded}

    finally:
        client.close()
