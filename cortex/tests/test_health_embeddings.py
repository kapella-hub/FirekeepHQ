"""/health must distinguish "the stack is up" from "your memories are searchable".

install.sh stopped blocking on the ~3.3GB Ollama pull — it returns as soon as the
services are up and finishes the download in the background. That is a much better
install (it used to sit silent for up to 15 minutes, and `firekeep init` wrapped it
in a 600s timeout, so the documented happy path timed out ON SUCCESS) and it creates
a real degraded window: until the model lands, every write returns HTTP 200 with
status="partial", is stored, is queued for backfill, and is NOT recallable.

Something has to be able to say that. /health is the one surface every client can
already read without a credential, so the signal lives there and `firekeep doctor`
renders it.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


class TestEmbeddingsHealthRow:
    def test_ready_is_reported_connected(self, test_client, mock_graph, mock_vector, mock_redis):
        mock_graph.ping = AsyncMock()
        mock_vector.ping = AsyncMock()
        mock_vector.memory_count = AsyncMock(return_value=0)
        mock_vector.embeddings_ready = AsyncMock(return_value=(True, "mxbai-embed-large (1024-dim)"))
        mock_redis.ping = AsyncMock()

        body = test_client.get("/health").json()
        assert body["services"]["embeddings"]["status"] == "connected"
        assert "1024-dim" in body["services"]["embeddings"]["detail"]
        assert body["status"] == "ok"

    def test_warming_is_reported_but_does_not_degrade_the_stack(
        self, test_client, mock_graph, mock_vector, mock_redis
    ):
        """The decision worth defending. "warming" is a transient state on the
        documented happy path that resolves with no intervention, and a health
        page that goes red minutes after a successful install is a health page
        people learn to ignore. The row still says exactly what is true."""
        mock_graph.ping = AsyncMock()
        mock_vector.ping = AsyncMock()
        mock_vector.memory_count = AsyncMock(return_value=0)
        mock_vector.embeddings_ready = AsyncMock(
            return_value=(False, "model 'mxbai-embed-large' is not pulled yet")
        )
        mock_redis.ping = AsyncMock()

        body = test_client.get("/health").json()
        assert body["services"]["embeddings"]["status"] == "warming"
        assert "not pulled yet" in body["services"]["embeddings"]["detail"]
        assert body["status"] == "ok", "warming must not read as degraded"

    def test_a_real_outage_still_degrades(
        self, test_client, mock_graph, mock_vector, mock_redis
    ):
        """Guard against the leniency above swallowing an actual failure."""
        mock_graph.ping = AsyncMock(side_effect=RuntimeError("Neo4j down"))
        mock_vector.ping = AsyncMock()
        mock_vector.memory_count = AsyncMock(return_value=0)
        mock_vector.embeddings_ready = AsyncMock(return_value=(False, "warming"))
        mock_redis.ping = AsyncMock()

        body = test_client.get("/health").json()
        assert body["status"] == "degraded"

    def test_a_probe_that_raises_cannot_500_the_health_endpoint(
        self, test_client, mock_graph, mock_vector, mock_redis
    ):
        """A health endpoint that can fail is worse than no health endpoint: every
        client reads it to decide whether the server is alive."""
        mock_graph.ping = AsyncMock()
        mock_vector.ping = AsyncMock()
        mock_vector.memory_count = AsyncMock(return_value=0)
        mock_vector.embeddings_ready = AsyncMock(side_effect=RuntimeError("kaboom"))
        mock_redis.ping = AsyncMock()

        resp = test_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["services"]["embeddings"]["status"] == "warming"


class TestEmbeddingsReadyProbe:
    """The probe itself, on the real VectorClient."""

    @pytest.mark.asyncio
    async def test_a_working_embed_is_ready(self, monkeypatch):
        from app.db.vector import VectorClient

        client = VectorClient.__new__(VectorClient)
        client._embedding_model = "mxbai-embed-large"
        monkeypatch.setattr(
            client, "_embed_post", AsyncMock(return_value=[0.0] * 1024), raising=False
        )
        ready, detail = await client.embeddings_ready()
        assert ready is True
        assert "1024-dim" in detail

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message",
        [
            'model "mxbai-embed-large" not found, try pulling it first',
            "404 page not found",
            "model not found",
        ],
    )
    async def test_a_missing_model_says_so_specifically(self, monkeypatch, message):
        """The first-install shape: ollama answers, the model is not there yet.
        Worth separating from "the endpoint is down" — one resolves itself and the
        other needs a human."""
        from app.db.vector import VectorClient

        client = VectorClient.__new__(VectorClient)
        client._embedding_model = "mxbai-embed-large"
        monkeypatch.setattr(
            client, "_embed_post", AsyncMock(side_effect=RuntimeError(message)), raising=False
        )
        ready, detail = await client.embeddings_ready()
        assert ready is False
        assert "not pulled yet" in detail

    @pytest.mark.asyncio
    async def test_any_other_failure_reports_the_reason_and_never_raises(self, monkeypatch):
        from app.db.vector import VectorClient

        client = VectorClient.__new__(VectorClient)
        client._embedding_model = "mxbai-embed-large"
        monkeypatch.setattr(
            client, "_embed_post",
            AsyncMock(side_effect=ConnectionError("connection refused")),
            raising=False,
        )
        ready, detail = await client.embeddings_ready()
        assert ready is False
        assert "connection refused" in detail

    @pytest.mark.asyncio
    async def test_an_empty_vector_is_not_ready(self, monkeypatch):
        """A 200 carrying no vector would otherwise read as success while every
        write silently landed unsearchable."""
        from app.db.vector import VectorClient

        client = VectorClient.__new__(VectorClient)
        client._embedding_model = "mxbai-embed-large"
        monkeypatch.setattr(client, "_embed_post", AsyncMock(return_value=[]), raising=False)
        ready, detail = await client.embeddings_ready()
        assert ready is False
        assert "empty vector" in detail
