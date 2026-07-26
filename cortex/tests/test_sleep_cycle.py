"""Tests for Sleep Cycle worker (app.workers.sleep_cycle)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


from app.workers.sleep_cycle import (
    CONSOLIDATION_SYSTEM_PROMPT,
    _get_neo4j_driver,
    _process_batch,
    _send_to_dlq,
    _validate_edges,
    _validate_nodes,
    process_event_batch,
)


# ---------------------------------------------------------------------------
# _validate_nodes
# ---------------------------------------------------------------------------


class TestValidateNodes:
    def test_valid_nodes_pass(self):
        nodes = [
            {"id": "n1", "label": "Concept", "properties": {"desc": "test"}},
            {"id": "n2", "label": "Action", "properties": {}},
        ]
        result = _validate_nodes(nodes)
        assert len(result) == 2
        assert result == nodes

    def test_invalid_nodes_dropped(self):
        nodes = [
            {"id": "n1", "label": "Concept", "properties": {"desc": "valid"}},
            {"id": 123, "label": "Concept", "properties": {}},  # id not string
            {"label": "Concept", "properties": {}},  # missing id
            {"id": "n3", "properties": {}},  # missing label
            {"id": "n4", "label": "Concept"},  # missing properties
            {"id": "n5", "label": "Concept", "properties": "not a dict"},  # properties not dict
            "not a dict",  # not a dict at all
        ]
        result = _validate_nodes(nodes)
        assert len(result) == 1
        assert result[0]["id"] == "n1"

    def test_empty_list(self):
        assert _validate_nodes([]) == []


# ---------------------------------------------------------------------------
# _validate_edges
# ---------------------------------------------------------------------------


class TestValidateEdges:
    def test_valid_edges_pass(self):
        edges = [
            {"source": "n1", "target": "n2", "type": "RELATES_TO"},
            {"source": "n2", "target": "n3", "type": "CAUSED"},
        ]
        result = _validate_edges(edges)
        assert len(result) == 2
        assert result == edges

    def test_invalid_edges_dropped(self):
        edges = [
            {"source": "n1", "target": "n2", "type": "RELATES_TO"},  # valid
            {"source": "n1", "target": "n2"},  # missing type
            {"source": "n1", "type": "CAUSED"},  # missing target
            {"target": "n2", "type": "CAUSED"},  # missing source
            {"source": 1, "target": "n2", "type": "X"},  # source not string
            "not a dict",  # not a dict at all
        ]
        result = _validate_edges(edges)
        assert len(result) == 1
        assert result[0]["source"] == "n1"

    def test_empty_list(self):
        assert _validate_edges([]) == []


# ---------------------------------------------------------------------------
# CONSOLIDATION_SYSTEM_PROMPT
# ---------------------------------------------------------------------------


class TestConsolidationPrompt:
    def test_prompt_contains_key_instructions(self):
        assert "cognitive consolidation engine" in CONSOLIDATION_SYSTEM_PROMPT
        assert "Knowledge Graph" in CONSOLIDATION_SYSTEM_PROMPT
        assert '"nodes"' in CONSOLIDATION_SYSTEM_PROMPT
        assert '"edges"' in CONSOLIDATION_SYSTEM_PROMPT
        assert "valid JSON object" in CONSOLIDATION_SYSTEM_PROMPT

    def test_prompt_specifies_node_structure(self):
        assert '"id"' in CONSOLIDATION_SYSTEM_PROMPT
        assert '"label"' in CONSOLIDATION_SYSTEM_PROMPT
        assert '"properties"' in CONSOLIDATION_SYSTEM_PROMPT

    def test_prompt_specifies_edge_structure(self):
        assert '"source"' in CONSOLIDATION_SYSTEM_PROMPT
        assert '"target"' in CONSOLIDATION_SYSTEM_PROMPT
        assert '"type"' in CONSOLIDATION_SYSTEM_PROMPT

    def test_prompt_specifies_valid_labels(self):
        assert "Concept" in CONSOLIDATION_SYSTEM_PROMPT
        assert "Action" in CONSOLIDATION_SYSTEM_PROMPT
        assert "Outcome" in CONSOLIDATION_SYSTEM_PROMPT
        assert "Resolution" in CONSOLIDATION_SYSTEM_PROMPT

    def test_prompt_specifies_valid_edge_types(self):
        assert "RELATES_TO" in CONSOLIDATION_SYSTEM_PROMPT
        assert "CAUSED" in CONSOLIDATION_SYSTEM_PROMPT
        assert "RESOLVED_BY" in CONSOLIDATION_SYSTEM_PROMPT
        assert "UTILIZES" in CONSOLIDATION_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# _send_to_dlq
# ---------------------------------------------------------------------------


class TestSendToDlq:
    def test_pushes_items_to_dlq_key(self):
        mock_redis = MagicMock()
        mock_settings = MagicMock()
        mock_settings.REDIS_STREAM_KEY = "firekeep:event_stream"
        items = ['{"event": 1}', '{"event": 2}']
        _send_to_dlq(mock_redis, items, mock_settings)

        assert mock_redis.lpush.call_count == 2
        calls = mock_redis.lpush.call_args_list
        expected_dlq_key = "firekeep:event_stream:dlq"
        assert calls[0][0] == (expected_dlq_key, '{"event": 1}')
        assert calls[1][0] == (expected_dlq_key, '{"event": 2}')

    def test_empty_items_no_push(self):
        mock_redis = MagicMock()
        mock_settings = MagicMock()
        mock_settings.REDIS_STREAM_KEY = "firekeep:event_stream"
        _send_to_dlq(mock_redis, [], mock_settings)
        mock_redis.lpush.assert_not_called()

    def test_redis_failure_does_not_raise(self):
        """DLQ push failure should be logged, not propagated."""
        mock_redis = MagicMock()
        mock_redis.lpush.side_effect = RuntimeError("Redis down")
        mock_settings = MagicMock()
        mock_settings.REDIS_STREAM_KEY = "firekeep:event_stream"
        # Should not raise
        _send_to_dlq(mock_redis, ["item1"], mock_settings)


# ---------------------------------------------------------------------------
# process_event_batch / _process_batch
# ---------------------------------------------------------------------------


def _make_pipeline_mock(raw_items: list[str | None]) -> MagicMock:
    """Create a mock Redis client that uses pipeline().execute() pattern.

    The pipeline mock accumulates rpop calls and returns raw_items on execute().
    """
    mock_redis = MagicMock()
    mock_pipe = MagicMock()
    mock_pipe.rpop = MagicMock(return_value=mock_pipe)  # chaining
    mock_pipe.execute = MagicMock(return_value=raw_items)
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)
    return mock_redis


class TestProcessEventBatch:
    """Tests for _process_batch / process_event_batch.

    All tests patch _get_redis_client to inject a mock Redis client,
    since the module caches the client at module-level.
    """

    @patch("app.workers.sleep_cycle._write_to_neo4j")
    @patch("app.workers.sleep_cycle.httpx.post")
    @patch("app.workers.sleep_cycle._get_redis_client")
    def test_successful_batch(self, mock_get_redis, mock_httpx_post, mock_write_neo4j):
        # Setup Redis mock with pipeline pattern
        raw_events = [
            json.dumps({"source": "ci", "payload": {"build": "1"}}),
            json.dumps({"source": "ci", "payload": {"build": "2"}}),
        ]
        # Pipeline returns raw_events + None padding to fill REDIS_BATCH_SIZE
        pipeline_results = raw_events + [None] * 48  # default REDIS_BATCH_SIZE is 50
        mock_redis = _make_pipeline_mock(pipeline_results)
        mock_get_redis.return_value = mock_redis

        # Setup LLM response mock
        llm_response = MagicMock()
        llm_response.status_code = 200
        llm_response.raise_for_status = MagicMock()
        llm_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "nodes": [
                                    {"id": "n1", "label": "Concept", "properties": {"description": "CI build"}},
                                ],
                                "edges": [
                                    {"source": "n1", "target": "n1", "type": "RELATES_TO"},
                                ],
                            }
                        )
                    }
                }
            ]
        }
        mock_httpx_post.return_value = llm_response

        # Setup Neo4j mock
        mock_write_neo4j.return_value = 2

        result = _process_batch()

        assert result["status"] == "ok"
        assert result["nodes"] == 1
        assert result["edges"] == 1
        assert result["written"] == 2

    @patch("app.workers.sleep_cycle._get_redis_client")
    def test_empty_batch(self, mock_get_redis):
        # All None results from pipeline = no events
        mock_redis = _make_pipeline_mock([None] * 50)
        mock_get_redis.return_value = mock_redis

        result = _process_batch()
        assert result["status"] == "empty"
        assert result["nodes"] == 0

    @patch("app.workers.sleep_cycle.httpx.post")
    @patch("app.workers.sleep_cycle._get_redis_client")
    def test_llm_http_error_sends_to_dlq(self, mock_get_redis, mock_httpx_post):
        import httpx

        pipeline_results = ['{"event": 1}'] + [None] * 49
        mock_redis = _make_pipeline_mock(pipeline_results)
        mock_get_redis.return_value = mock_redis

        mock_httpx_post.side_effect = httpx.HTTPError("LLM down")

        result = _process_batch()
        assert result["status"] == "llm_error"
        # Items should be pushed to DLQ via the mock redis client
        mock_redis.lpush.assert_called()

    @patch("app.workers.sleep_cycle.httpx.post")
    @patch("app.workers.sleep_cycle._get_redis_client")
    def test_llm_parse_failure_sends_to_dlq(self, mock_get_redis, mock_httpx_post):
        pipeline_results = ['{"event": 1}'] + [None] * 49
        mock_redis = _make_pipeline_mock(pipeline_results)
        mock_get_redis.return_value = mock_redis

        llm_response = MagicMock()
        llm_response.raise_for_status = MagicMock()
        llm_response.json.return_value = {
            "choices": [{"message": {"content": "not valid json {{"}}]
        }
        mock_httpx_post.return_value = llm_response

        result = _process_batch()
        assert result["status"] == "parse_error"
        mock_redis.lpush.assert_called()

    @patch("app.workers.sleep_cycle.httpx.post")
    @patch("app.workers.sleep_cycle._get_redis_client")
    def test_invalid_structure_sends_to_dlq(self, mock_get_redis, mock_httpx_post):
        pipeline_results = ['{"event": 1}'] + [None] * 49
        mock_redis = _make_pipeline_mock(pipeline_results)
        mock_get_redis.return_value = mock_redis

        llm_response = MagicMock()
        llm_response.raise_for_status = MagicMock()
        llm_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({"nodes": "not a list", "edges": "not a list"})
                    }
                }
            ]
        }
        mock_httpx_post.return_value = llm_response

        result = _process_batch()
        assert result["status"] == "validation_error"

    @patch("app.workers.sleep_cycle.httpx.post")
    @patch("app.workers.sleep_cycle._get_redis_client")
    def test_empty_extraction(self, mock_get_redis, mock_httpx_post):
        """LLM returns valid JSON but with no valid nodes/edges."""
        pipeline_results = ['{"event": 1}'] + [None] * 49
        mock_redis = _make_pipeline_mock(pipeline_results)
        mock_get_redis.return_value = mock_redis

        llm_response = MagicMock()
        llm_response.raise_for_status = MagicMock()
        llm_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "nodes": [{"bad": "node"}],  # invalid node
                                "edges": [{"bad": "edge"}],  # invalid edge
                            }
                        )
                    }
                }
            ]
        }
        mock_httpx_post.return_value = llm_response

        result = _process_batch()
        assert result["status"] == "empty_extraction"

    @patch("app.workers.sleep_cycle._write_to_neo4j")
    @patch("app.workers.sleep_cycle.httpx.post")
    @patch("app.workers.sleep_cycle._get_redis_client")
    def test_neo4j_write_failure_sends_to_dlq(
        self, mock_get_redis, mock_httpx_post, mock_write_neo4j
    ):
        pipeline_results = ['{"event": 1}'] + [None] * 49
        mock_redis = _make_pipeline_mock(pipeline_results)
        mock_get_redis.return_value = mock_redis

        llm_response = MagicMock()
        llm_response.raise_for_status = MagicMock()
        llm_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "nodes": [{"id": "n1", "label": "Concept", "properties": {"d": "x"}}],
                                "edges": [],
                            }
                        )
                    }
                }
            ]
        }
        mock_httpx_post.return_value = llm_response
        mock_write_neo4j.side_effect = RuntimeError("Neo4j down")

        result = _process_batch()
        assert result["status"] == "neo4j_error"
        mock_redis.lpush.assert_called()

    @patch("app.workers.sleep_cycle.httpx.post")
    @patch("app.workers.sleep_cycle._get_redis_client")
    def test_llm_call_format(self, mock_get_redis, mock_httpx_post):
        """Verify the LLM API call uses the correct format."""
        pipeline_results = [
            json.dumps({"source": "ci", "payload": {"x": 1}}),
        ] + [None] * 49
        mock_redis = _make_pipeline_mock(pipeline_results)
        mock_get_redis.return_value = mock_redis

        # Make it fail on LLM so we can inspect the call
        import httpx
        mock_httpx_post.side_effect = httpx.HTTPError("fail")

        _process_batch()

        call_kwargs = mock_httpx_post.call_args
        json_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert json_body["messages"][0]["role"] == "system"
        assert json_body["messages"][0]["content"] == CONSOLIDATION_SYSTEM_PROMPT
        assert json_body["messages"][1]["role"] == "user"
        assert json_body["temperature"] == 0.1
        assert json_body["response_format"] == {"type": "json_object"}

    @patch("app.workers.sleep_cycle._get_redis_client")
    def test_malformed_event_json_handled(self, mock_get_redis):
        """Malformed JSON in Redis should not crash the batch."""
        pipeline_results = ["not valid json"] + [None] * 49
        mock_redis = _make_pipeline_mock(pipeline_results)
        mock_get_redis.return_value = mock_redis

        import httpx

        with patch("app.workers.sleep_cycle.httpx.post") as mock_post:
            mock_post.side_effect = httpx.HTTPError("fail")
            result = _process_batch()

        # Should still attempt LLM call with parsed events
        assert result["status"] == "llm_error"

    def test_process_event_batch_catches_unhandled_errors(self):
        """The Celery task wrapper catches all exceptions."""
        with patch("app.workers.sleep_cycle._process_batch") as mock_batch:
            mock_batch.side_effect = RuntimeError("unexpected")
            result = process_event_batch()
            assert result["status"] == "error"


# ---------------------------------------------------------------------------
# _get_neo4j_driver — pool size config
# ---------------------------------------------------------------------------


class TestGetNeo4jDriver:
    def test_passes_pool_size_config(self):
        """_get_neo4j_driver should pass NEO4J_POOL_SIZE to the driver."""
        import app.workers.sleep_cycle as mod

        # Reset cached driver so it gets recreated
        mod._neo4j_driver = None

        mock_settings = MagicMock()
        mock_settings.NEO4J_URI = "bolt://localhost:7687"
        mock_settings.NEO4J_USER = "neo4j"
        mock_settings.NEO4J_PASSWORD = "test"
        mock_settings.NEO4J_POOL_SIZE = 42

        with patch("app.workers.sleep_cycle.get_settings", return_value=mock_settings), \
             patch("app.workers.sleep_cycle.GraphDatabase.driver") as mock_driver_cls:
            mock_driver_cls.return_value = MagicMock()
            _get_neo4j_driver()

            mock_driver_cls.assert_called_once_with(
                "bolt://localhost:7687",
                auth=("neo4j", "test"),
                max_connection_pool_size=42,
            )

        # Clean up cached driver to avoid side effects
        mod._neo4j_driver = None


# ---------------------------------------------------------------------------
# _send_to_dlq — ltrim cap
# ---------------------------------------------------------------------------


class TestSendToDlqCap:
    def test_ltrim_called_to_cap_dlq_size(self):
        """_send_to_dlq should call ltrim to cap the DLQ at DLQ_MAX_SIZE."""
        mock_redis = MagicMock()
        mock_settings = MagicMock()
        mock_settings.REDIS_STREAM_KEY = "firekeep:event_stream"
        mock_settings.DLQ_MAX_SIZE = 500

        items = ['{"event": 1}', '{"event": 2}']
        _send_to_dlq(mock_redis, items, mock_settings)

        expected_dlq_key = "firekeep:event_stream:dlq"
        mock_redis.ltrim.assert_called_once_with(expected_dlq_key, 0, 499)
