"""Similarity is not disagreement — a restatement must not be recorded as
having been contradicted.

WHY THIS EXISTS. `contradiction.py` decides supersession on cosine similarity
alone: no negation check, no entailment check, no LLM anywhere in the path.
Both of its failure modes were reproduced against the live store.

FALSE POSITIVE (the common case). "The sweepprobe widget service listens on
TCP port 9931 for inbound telemetry", then the SAME fact reworded — the second
learn returned `superseded: [A1]` and A1's history showed
`status=superseded, contradicted_count=1`, for a memory nothing contradicted.
That number is not decorative: it feeds `compute_confidence`, the
`(1+confirmed)/(1+contradicted)` factor in recall scoring, and the memory
agent's tie-breaks — so restating a fact permanently demotes the earlier copy
as if someone had disputed it.

FALSE NEGATIVE. "The archive job runs nightly at 02:00 UTC", then "We removed
all scheduling from the archive job; it is now triggered manually and never
runs overnight" — `superseded: []`, both left active, and a later recall
returned the STALE fact at rank 1 (81%) and its correction at rank 2 (76%) with
no signal that they conflict. That is not fixed here and cannot be fixed by a
threshold: detecting it needs an NLI/LLM judgement on the write path. What is
fixed is the CLAIM — the module, the log lines and the stored payload now say
"near-duplicate", so nobody builds on this expecting stale facts to retire.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.contradiction import SUPERSEDE_REASON, detect_and_supersede


@pytest.mark.asyncio
async def test_supersession_does_not_count_as_a_contradiction():
    """The false positive, at the call that records it."""
    vector = MagicMock()
    vector.find_similar = AsyncMock(return_value=[{"id": "old-id", "score": 0.93}])
    vector.update_status = AsyncMock()
    graph = MagicMock()
    graph.create_supersession = AsyncMock()

    result = await detect_and_supersede(
        vector=vector, graph=graph,
        new_text="the widget service listens on port 9931",
        new_vector_id="new-id", new_graph_id="g", domain="infra",
        workspace_id="ws-1",
    )

    assert result == ["old-id"]
    kwargs = vector.update_status.await_args.kwargs
    assert kwargs["count_as_contradiction"] is False
    assert kwargs["reason"] == SUPERSEDE_REASON


@pytest.mark.asyncio
async def test_update_status_skips_the_contradiction_bookkeeping():
    """The flag has to actually change the write, not just be passed.

    `update_status(status="superseded")` unconditionally incremented
    `contradicted_count` AND re-derived `confidence` downward.
    """
    from app.db.vector import VectorClient

    settings = MagicMock()
    settings.QDRANT_COLLECTION = "c"
    client = VectorClient.__new__(VectorClient)
    client._collection = "c"
    client._client = MagicMock()
    client._client.retrieve = AsyncMock(
        return_value=[MagicMock(payload={"contradicted_count": 0, "confirmed_count": 0})]
    )
    client._client.set_payload = AsyncMock()

    await client.update_status(
        "old-id", "superseded", superseded_by="new-id", count_as_contradiction=False,
    )

    payload = client._client.set_payload.await_args.kwargs["payload"]
    assert "contradicted_count" not in payload
    assert "confidence" not in payload
    assert payload["status"] == "superseded"
    assert payload["superseded_by"] == "new-id"


@pytest.mark.asyncio
async def test_a_deliberate_contradiction_still_counts():
    """The flag is opt-out, not a removal. `/memory/deprecate` and the memory
    agent's contradiction pass reach the same method and must keep recording a
    real contradiction — otherwise the fix would delete the signal instead of
    correcting who emits it."""
    from app.db.vector import VectorClient

    client = VectorClient.__new__(VectorClient)
    client._collection = "c"
    client._client = MagicMock()
    client._client.retrieve = AsyncMock(
        return_value=[MagicMock(payload={"contradicted_count": 2, "confirmed_count": 0})]
    )
    client._client.set_payload = AsyncMock()

    await client.update_status("old-id", "superseded", superseded_by="new-id")

    payload = client._client.set_payload.await_args.kwargs["payload"]
    assert payload["contradicted_count"] == 3
    assert "confidence" in payload
