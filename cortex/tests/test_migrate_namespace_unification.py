"""Tests for the SP0 namespace-unification migration (spec C1).

The script is loaded via importlib by file path because both the repo root
and cortex/ contain a ``scripts/`` directory (shell hooks vs python scripts),
which makes bare ``import scripts.x`` ambiguous.
"""

from __future__ import annotations

import importlib.util
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "scripts", "migrate_namespace_unification.py",
)
_spec = importlib.util.spec_from_file_location("migrate_namespace_unification", _SCRIPT_PATH)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


def _point(pid: str, namespace: str) -> MagicMock:
    p = MagicMock()
    p.id = pid
    p.payload = {"namespace": namespace, "text": "t"}
    return p


class TestQdrantMigration:
    @pytest.mark.asyncio
    async def test_retags_firekeepbridge_points_to_default(self):
        client = AsyncMock()
        client.scroll = AsyncMock(
            side_effect=[([_point("a", "firekeepbridge"), _point("b", "firekeepbridge")], None)]
        )

        result = await mig.migrate_qdrant_namespace(client, "firekeep_memory")

        assert result["updated"] == 2
        client.set_payload.assert_awaited_once()
        kwargs = client.set_payload.await_args.kwargs
        assert kwargs["collection_name"] == "firekeep_memory"
        assert kwargs["payload"] == {"namespace": "default"}
        assert set(kwargs["points"]) == {"a", "b"}

    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self):
        client = AsyncMock()
        client.scroll = AsyncMock(side_effect=[([_point("a", "firekeepbridge")], None)])

        result = await mig.migrate_qdrant_namespace(client, "firekeep_memory", dry_run=True)

        assert result["updated"] == 1
        assert result["dry_run"] is True
        client.set_payload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_idempotent_second_run_finds_nothing(self):
        # The scroll filter targets namespace="firekeepbridge"; after the first
        # run no points match, so nothing is written.
        client = AsyncMock()
        client.scroll = AsyncMock(side_effect=[([], None)])

        result = await mig.migrate_qdrant_namespace(client, "firekeep_memory")

        assert result["updated"] == 0
        client.set_payload.assert_not_awaited()


class TestNeo4jMigration:
    def _result(self, record: dict) -> AsyncMock:
        r = AsyncMock()
        r.single = AsyncMock(return_value=record)
        return r

    @pytest.mark.asyncio
    async def test_relinks_domains_and_deletes_orphan_namespace(self):
        session = AsyncMock()
        session.run = AsyncMock(
            side_effect=[self._result({"relinked": 3}), self._result({"deleted": 1})]
        )

        result = await mig.migrate_neo4j_namespace(session)

        assert result["relinked"] == 3
        assert result["orphan_deleted"] == 1
        assert session.run.await_count == 2
        # First call re-links, second deletes the orphaned Namespace node
        first_query = session.run.await_args_list[0].args[0]
        assert "CONTAINS" in first_query
        assert "MERGE" in first_query
        second_query = session.run.await_args_list[1].args[0]
        assert "DETACH DELETE" in second_query

    @pytest.mark.asyncio
    async def test_dry_run_only_counts(self):
        session = AsyncMock()
        session.run = AsyncMock(side_effect=[self._result({"relinked": 3})])

        result = await mig.migrate_neo4j_namespace(session, dry_run=True)

        assert result["relinked"] == 3
        assert result["dry_run"] is True
        assert session.run.await_count == 1
        query = session.run.await_args_list[0].args[0]
        assert "DELETE" not in query
        assert "MERGE" not in query
