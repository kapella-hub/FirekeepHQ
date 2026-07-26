"""Tests for re-embedding worker (app.workers.reembed)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# reembed_all task
# ---------------------------------------------------------------------------


class TestReembedAll:
    """Tests for the reembed_all Celery task.

    We use reembed_all.run() which calls the bound task with self=task instance.
    We patch update_state on the task to capture progress reports.
    """

    @patch("app.workers.reembed._embed_texts_sync")
    @patch("app.workers.reembed.QdrantClient")
    def test_reembed_all_completes(self, mock_qdrant_cls, mock_embed):
        """Full re-embed run with mocked Qdrant and embedding API."""
        from app.workers.reembed import reembed_all

        mock_client = MagicMock()
        mock_qdrant_cls.return_value = mock_client

        mock_collection_info = MagicMock()
        mock_collection_info.points_count = 2
        mock_client.get_collection.return_value = mock_collection_info

        record1 = MagicMock()
        record1.id = "id-1"
        record1.payload = {"text": "Hello world"}

        record2 = MagicMock()
        record2.id = "id-2"
        record2.payload = {"text": "Goodbye world"}

        mock_client.scroll.side_effect = [
            ([record1, record2], None),
        ]

        mock_embed.return_value = [[0.1] * 768, [0.2] * 768]

        with patch.object(reembed_all, "update_state"):
            result = reembed_all.run(new_model="test-model", batch_size=50)

        assert result["status"] == "completed"
        assert result["reembedded"] == 2
        mock_client.update_vectors.assert_called_once()
        mock_client.close.assert_called_once()

    @patch("app.workers.reembed.QdrantClient")
    def test_reembed_all_empty_collection(self, mock_qdrant_cls):
        """Re-embed on empty collection returns immediately."""
        from app.workers.reembed import reembed_all

        mock_client = MagicMock()
        mock_qdrant_cls.return_value = mock_client

        mock_collection_info = MagicMock()
        mock_collection_info.points_count = 0
        mock_client.get_collection.return_value = mock_collection_info

        with patch.object(reembed_all, "update_state"):
            result = reembed_all.run(new_model=None, batch_size=50)

        assert result["status"] == "completed"
        assert result["reembedded"] == 0
        mock_client.close.assert_called_once()

    @patch("app.workers.reembed._embed_texts_sync")
    @patch("app.workers.reembed.QdrantClient")
    def test_reembed_all_reports_progress(self, mock_qdrant_cls, mock_embed):
        """Task reports progress via update_state."""
        from app.workers.reembed import reembed_all

        mock_client = MagicMock()
        mock_qdrant_cls.return_value = mock_client

        mock_collection_info = MagicMock()
        mock_collection_info.points_count = 1
        mock_client.get_collection.return_value = mock_collection_info

        record = MagicMock()
        record.id = "id-1"
        record.payload = {"text": "test text"}
        mock_client.scroll.side_effect = [
            ([record], None),
        ]

        mock_embed.return_value = [[0.1] * 768]

        mock_update = MagicMock()
        with patch.object(reembed_all, "update_state", mock_update):
            reembed_all.run(new_model="test-model", batch_size=50)

        # Should have called update_state at least twice (initial 0 + after batch)
        assert mock_update.call_count >= 2
        # Check that PROGRESS state was used
        for call in mock_update.call_args_list:
            assert call.kwargs.get("state") == "PROGRESS"

    @patch("app.workers.reembed._embed_texts_sync")
    @patch("app.workers.reembed.QdrantClient")
    def test_reembed_all_skips_empty_text(self, mock_qdrant_cls, mock_embed):
        """Records with empty text payload are skipped."""
        from app.workers.reembed import reembed_all

        mock_client = MagicMock()
        mock_qdrant_cls.return_value = mock_client

        mock_collection_info = MagicMock()
        mock_collection_info.points_count = 2
        mock_client.get_collection.return_value = mock_collection_info

        record_with_text = MagicMock()
        record_with_text.id = "id-1"
        record_with_text.payload = {"text": "Has text"}

        record_empty = MagicMock()
        record_empty.id = "id-2"
        record_empty.payload = {"text": ""}

        mock_client.scroll.side_effect = [
            ([record_with_text, record_empty], None),
        ]

        mock_embed.return_value = [[0.1] * 768]

        with patch.object(reembed_all, "update_state"):
            result = reembed_all.run(new_model="test-model", batch_size=50)

        assert result["reembedded"] == 1
        mock_embed.assert_called_once()
        assert len(mock_embed.call_args[0][0]) == 1

    @patch("app.workers.reembed.QdrantClient")
    def test_reembed_all_closes_client_on_error(self, mock_qdrant_cls):
        """Qdrant client is closed even if an error occurs."""
        from app.workers.reembed import reembed_all

        mock_client = MagicMock()
        mock_qdrant_cls.return_value = mock_client

        mock_client.get_collection.side_effect = RuntimeError("connection failed")

        with patch.object(reembed_all, "update_state"):
            with pytest.raises(RuntimeError, match="connection failed"):
                reembed_all.run(new_model="test-model", batch_size=50)

        mock_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# _embed_texts_sync
# ---------------------------------------------------------------------------


class TestEmbedTextsSync:
    @patch("app.workers.reembed.httpx.post")
    def test_embed_texts_sync_success(self, mock_post):
        from app.workers.reembed import _embed_texts_sync

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2]},
                {"index": 1, "embedding": [0.3, 0.4]},
            ]
        }
        mock_post.return_value = mock_response

        result = _embed_texts_sync(
            ["hello", "world"],
            base_url="http://localhost:11434/v1",
            model="test-model",
            api_key="test-key",
        )

        assert len(result) == 2
        assert result[0] == [0.1, 0.2]
        assert result[1] == [0.3, 0.4]

    @patch("app.workers.reembed.httpx.post")
    def test_embed_texts_sync_sorts_by_index(self, mock_post):
        from app.workers.reembed import _embed_texts_sync

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"index": 1, "embedding": [0.3, 0.4]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ]
        }
        mock_post.return_value = mock_response

        result = _embed_texts_sync(
            ["hello", "world"],
            base_url="http://localhost:11434/v1",
            model="test-model",
            api_key="",
        )

        assert result[0] == [0.1, 0.2]
        assert result[1] == [0.3, 0.4]
