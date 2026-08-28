"""`legacy_unscoped` has to survive the whole read path, not just be stamped.

Identity-v2 D4 quarantines the graph's un-splittable legacy: chain nodes whose
id is a bare `_content_hash(text)` may have MERGEd rows from more than one
workspace before anything could tell them apart, so the migration stamps them
`legacy_unscoped: true` and `RAGEngine._scope_verdict` denies them under every
`unattributed` setting.

That deny reads the flag off the row the Cypher returned. Task 5's review found
the four read-path RETURN clauses never selected it, which made the whole
mechanism inert: the migration would stamp a property no query ever asked for,
`_format_graph_entries` would never set the metadata key, and `_scope_verdict`
would receive `legacy_unscoped=False` for every row in the store. These tests
pin both halves — the projection and the end-to-end deny — so the stamp cannot
go quiet again.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.db.graph import Neo4jClient
from app.engine.rag import RAGEngine
from app.models import ContextQuery


@pytest.fixture()
def settings() -> Settings:
    return Settings(NEO4J_URI="bolt://localhost:7687", NEO4J_USER="neo4j",
                    NEO4J_PASSWORD="test")


def _records(rows: list[dict]):
    result = MagicMock()

    async def _aiter():
        for r in rows:
            yield r

    result.__aiter__ = lambda self: _aiter()
    return result


def _return_aliases(query: str) -> list[str]:
    """The column names a Cypher RETURN clause actually projects.

    Enough of a parser for the four read-path queries, all of which use the
    ``expr AS alias`` form. It exists so the end-to-end tests below FAIL when
    a clause stops selecting `legacy_unscoped`: a plain mock hands back
    whatever the test wrote regardless of the query, which is precisely how a
    stamped-but-unselected property stayed invisible for a whole task.
    """
    tail = query.rsplit("RETURN", 1)[1]
    for terminator in ("ORDER BY", "LIMIT"):
        tail = tail.split(terminator)[0]
    # Splitting on every comma also splits INSIDE expressions like
    # `coalesce(x, [])`, which is harmless here only because the fragment
    # carrying the ` AS alias` is always the last one: a nested comma yields
    # leading fragments with no ` AS ` at all, and those are dropped below.
    aliases = []
    for item in tail.split(","):
        parts = item.strip().split(" AS ")
        if len(parts) == 2:
            aliases.append(parts[1].strip())
    return aliases


def _projecting_session(nodes: list[dict]):
    """A `session.run` that projects each node through the query's RETURN.

    `nodes` are the node properties as they sit in Neo4j; what reaches the
    caller is only what the Cypher asked for.
    """

    async def _run(query, *args, **kwargs):
        aliases = _return_aliases(query)
        return _records([{a: node.get(a) for a in aliases} for node in nodes])

    return AsyncMock(side_effect=_run)


@pytest.fixture()
def graph(settings: Settings) -> Neo4jClient:
    client = Neo4jClient(settings)
    session = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    driver = MagicMock()
    driver.session.return_value = ctx
    client._driver = driver
    client._mock_session = session
    return client


LEGACY_ROW = {
    "name": "migration",
    "description": "ran the freeze migration against the live store",
    "label": "Action",
    "memory_ids": [],
    "distance": 1,
    "score": 0.9,
    "legacy_unscoped": True,
}
CLEAN_ROW = {**LEGACY_ROW, "name": "deploy",
             "description": "recreated the cortex containers",
             "legacy_unscoped": False}


# ---------------------------------------------------------------------------
# The projection: every read-path RETURN selects the flag
# ---------------------------------------------------------------------------


class TestCypherSelectsTheFlag:
    async def test_fulltext(self, graph):
        graph._mock_session.run = AsyncMock(return_value=_records([LEGACY_ROW]))
        rows = await graph._query_related_fulltext(["migration"], 5)
        query = graph._mock_session.run.call_args.args[0]
        assert "legacy_unscoped" in query.split("RETURN", 1)[1]
        assert rows[0]["legacy_unscoped"] is True

    async def test_multihop_fulltext(self, graph):
        graph._mock_session.run = AsyncMock(return_value=_records([LEGACY_ROW]))
        rows = await graph._query_related_multihop_fulltext(["migration"], 5, None, 3, 0.5)
        query = graph._mock_session.run.call_args.args[0]
        assert "legacy_unscoped" in query.split("RETURN", 1)[1]
        assert rows[0]["legacy_unscoped"] is True

    async def test_contains_fallback(self, graph):
        graph._mock_session.run = AsyncMock(return_value=_records([LEGACY_ROW]))
        rows = await graph._query_related_contains(["migration"], 5)
        query = graph._mock_session.run.call_args.args[0]
        assert "legacy_unscoped" in query.split("RETURN", 1)[1]
        assert rows[0]["legacy_unscoped"] is True

    async def test_resolutions(self, graph):
        graph._mock_session.run = AsyncMock(return_value=_records([{
            "resolution": "restart the collector", "error": "timeout",
            "id": "e1", "memory_ids": [], "legacy_unscoped": True}]))
        rows = await graph.query_resolutions("timeout")
        query = graph._mock_session.run.call_args.args[0]
        assert "legacy_unscoped" in query.split("RETURN", 1)[1]
        assert rows[0]["legacy_unscoped"] is True

    def test_resolutions_reads_both_nodes_of_the_pair(self, graph):
        """The row is an (Outcome, Resolution) pair and `memory_ids` unions
        both, so either node being legacy taints the row."""
        import inspect

        src = inspect.getsource(Neo4jClient.query_resolutions)
        clause = src.split("RETURN", 1)[1].split('"""', 1)[0]
        assert "r.legacy_unscoped" in clause and "o.legacy_unscoped" in clause


# ---------------------------------------------------------------------------
# End to end: a stamped node cannot reach a recall
# ---------------------------------------------------------------------------


class TestStampedNodeIsDeniedEndToEnd:
    @pytest.fixture()
    def engine(self, graph) -> RAGEngine:
        vector = AsyncMock()
        vector.search = AsyncMock(return_value=[])
        return RAGEngine(graph=graph, vector=vector,
                         settings=Settings(RERANK_ENABLED=False))

    async def test_a_stamped_row_never_reaches_the_response(self, graph, engine):
        graph._mock_session.run = _projecting_session([LEGACY_ROW, CLEAN_ROW])
        resp = await engine.recall(ContextQuery(task="how did the migration go"),
                                   workspace_id="ws-1")
        assert CLEAN_ROW["description"] in resp.context_block
        assert LEGACY_ROW["description"] not in resp.context_block

    async def test_the_deny_holds_with_no_scope_declared(self, graph, engine):
        """`unattributed` is a lever over *unattributable* rows; a legacy node's
        own identity is untrustworthy, so no policy rescues it."""
        graph._mock_session.run = _projecting_session([LEGACY_ROW])
        resp = await engine.recall(ContextQuery(task="how did the migration go"))
        assert LEGACY_ROW["description"] not in resp.context_block

    async def test_an_unstamped_row_still_arrives(self, graph, engine):
        graph._mock_session.run = _projecting_session([CLEAN_ROW])
        resp = await engine.recall(ContextQuery(task="how did the deploy go"),
                                   workspace_id="ws-1")
        assert CLEAN_ROW["description"] in resp.context_block

    async def test_a_stamped_resolution_is_denied_too(self, graph, engine):
        """The resolutions leg is a second entry point into the same gate —
        it fires on error-shaped tasks and builds its own graph entries."""
        graph._mock_session.run = _projecting_session([{
            "resolution": "restart the collector after the config change",
            "error": "connection timeout", "id": "e1", "memory_ids": [],
            "legacy_unscoped": True}])
        resp = await engine.recall(ContextQuery(task="fix the connection timeout error"),
                                   workspace_id="ws-1")
        assert "restart the collector after the config change" not in resp.context_block
