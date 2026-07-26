"""Tests for namespace migration task."""

from __future__ import annotations

from unittest.mock import patch


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
