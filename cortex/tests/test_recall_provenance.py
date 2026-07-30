"""Recall must say who wrote a memory and when.

`upsert` PROMOTES agent_id / session_id / project to top-level payload fields and
EXCLUDES them from the nested `metadata` sub-dict (`_PROMOTED_PAYLOAD_KEYS` /
`_EXCLUDED_FROM_NESTED_METADATA`). `search` projects a fixed list of top-level keys
plus `**payload["metadata"]` — and that list never named the promoted three.

So the promotion moved them OUT of the one place the reader looked. Every memory
written since carries its author in Qdrant and reports none at recall: a caller
cannot tell whether a result came from a teammate, from a CI bot, or from the
session currently reading it. That last case is what makes it more than cosmetic —
it is the difference between "memory helped" and a session retrieving its own
output, and it silently invalidated a measurement of exactly that.

The round-trip test is the honest shape here. Asserting that `search` names some
key proves only that the reader and the test agree; feeding back the payload the
WRITER actually produced is what catches a promotion the reader never learned about.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.db.vector import VectorClient
from app.engine.rag import RAGEngine

_format_markdown = RAGEngine._format_markdown


@pytest.fixture()
def mock_qdrant_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def vector_client(mock_qdrant_client) -> VectorClient:
    client = VectorClient(
        Settings(
            QDRANT_HOST="localhost",
            QDRANT_PORT=6333,
            QDRANT_COLLECTION="test_collection",
            EMBEDDING_DIM=768,
            LLM_BASE_URL="http://localhost:11434/v1",
            EMBEDDING_MODEL="test-embed",
        )
    )
    client._client = mock_qdrant_client
    client._http_client = AsyncMock()
    return client


async def _write_then_read(client, qdrant, metadata: dict) -> dict:
    """Round-trip: upsert `metadata`, then search over the payload it produced."""
    qdrant.retrieve = AsyncMock(return_value=[])
    with patch.object(client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768):
        await client.upsert("a memory", metadata)

        written = qdrant.upsert.call_args.kwargs["points"][0].payload

        point = MagicMock()
        point.id = "p1"
        point.score = 0.77
        point.payload = written
        results = MagicMock()
        results.points = [point]
        qdrant.query_points = AsyncMock(return_value=results)

        found = await client.search("a memory", top_k=1)
    return found[0]["metadata"]


# --- the projection -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_written_contributor_survives_the_round_trip(
    vector_client, mock_qdrant_client
):
    md = await _write_then_read(
        vector_client,
        mock_qdrant_client,
        {"source": "action_log", "agent_id": "alice", "session_id": "sess-42"},
    )
    assert md["agent_id"] == "alice"
    assert md["session_id"] == "sess-42"


@pytest.mark.asyncio
async def test_project_survives_the_round_trip(vector_client, mock_qdrant_client):
    """`project` was readable as a Qdrant FILTER and never returned to the caller."""
    md = await _write_then_read(
        vector_client,
        mock_qdrant_client,
        {"source": "action_log", "project": "firekeep"},
    )
    assert md["project"] == "firekeep"


@pytest.mark.asyncio
async def test_unattributed_writes_report_the_unknown_sentinel(
    vector_client, mock_qdrant_client
):
    """Absent, not silently blank: `upsert` stores "unknown", so recall must say so
    rather than omitting the key and leaving a caller unable to distinguish
    "nobody recorded this" from "the reader dropped it"."""
    md = await _write_then_read(vector_client, mock_qdrant_client, {"source": "x"})
    assert md["agent_id"] == "unknown"
    assert md["session_id"] == "unknown"
    assert md["project"] is None


@pytest.mark.asyncio
async def test_legacy_nested_provenance_is_not_clobbered(
    vector_client, mock_qdrant_client
):
    """Records written before the promotion hold agent_id in NESTED metadata only.

    Reading the promoted keys with `payload.get(k)` would overwrite those with
    None — turning a fix for new memories into data loss for the ~3.9K old ones.
    """
    point = MagicMock()
    point.id = "old"
    point.score = 0.5
    point.payload = {"text": "an old memory", "metadata": {"agent_id": "alice"}}
    results = MagicMock()
    results.points = [point]
    mock_qdrant_client.query_points = AsyncMock(return_value=results)

    with patch.object(
        vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
    ):
        found = await vector_client.search("q", top_k=1)

    assert found[0]["metadata"]["agent_id"] == "alice"


@pytest.mark.asyncio
async def test_nested_metadata_still_wins_for_its_own_keys(
    vector_client, mock_qdrant_client
):
    """The projection must not shadow unrelated caller metadata."""
    md = await _write_then_read(
        vector_client,
        mock_qdrant_client,
        {"source": "x", "agent_id": "alice", "custom": "kept"},
    )
    assert md["custom"] == "kept"
    assert md["agent_id"] == "alice"


# --- what an agent actually reads ---------------------------------------------
#
# `memory_recall` (mcp_server.py) returns ONLY `context_block` — sources and their
# metadata are discarded at the MCP boundary. Fixing the projection alone would
# therefore surface provenance to REST callers and to no agent at all.


def _entry(content="m", raw=0.8, **md):
    return {"content": content, "score": 1.0, "store": "vector",
            "metadata": {"raw_score": raw, **md}}


def test_the_rendered_line_names_the_contributor_and_the_date():
    out = _format_markdown([_entry(agent_id="alice", timestamp="2026-07-12T09:30:00Z")])
    assert "alice" in out, out
    assert "2026-07-12" in out, out


def test_the_rendered_line_drops_the_clock():
    """A date answers "is this stale?". The time is per-line noise in a token budget."""
    out = _format_markdown([_entry(agent_id="alice", timestamp="2026-07-12T09:30:00Z")])
    assert "09:30" not in out, out


def test_an_unknown_contributor_is_not_rendered():
    """Most memories predate attribution. "unknown" on every line teaches an agent
    to skip the suffix entirely, which costs the lines that do carry a name."""
    out = _format_markdown([_entry(agent_id="unknown", timestamp="2026-07-12T00:00:00Z")])
    assert "unknown" not in out, out
    assert "2026-07-12" in out, "the date is still known and still useful"


def test_the_legacy_sentinel_is_not_rendered():
    out = _format_markdown([_entry(agent_id="legacy-pre-team-continuity")])
    assert "legacy" not in out, out


def test_an_entry_with_no_provenance_renders_as_before():
    """Graph entries carry neither field. They must not gain a dangling separator."""
    out = _format_markdown([_entry()])
    line = [ln for ln in out.splitlines() if ln.startswith("1.")][0]
    assert line == "1. [80%] (vector) m", line


def test_the_session_id_is_auditable_but_not_rendered():
    """A session id is a 32-char hex — useful to an auditor reading `sources`,
    pure noise in a line an LLM reads. It belongs in metadata only."""
    out = _format_markdown([_entry(agent_id="alice", session_id="deadbeef" * 4)])
    assert "deadbeef" not in out, out


def test_provenance_survives_a_status_label():
    """The suffix must not be swallowed by, or swallow, the lifecycle label."""
    entry = _entry(agent_id="alice")
    entry["_lifecycle_status"] = "superseded"
    out = _format_markdown([entry])
    assert "[SUPERSEDED]" in out, out
    assert "alice" in out, out
