"""A dedup merge must not launder another author's text into the keeper's
attribution (THREAT-MODEL §5.20).

`_merge_cluster` writes the LLM-merged text under a copy of the KEEPER's payload --
member_id, credential_id and all. When the cluster spans authors, a teammate's
poisoned text would come back from recall as the keeper's own note. The merge
still happens (deduplication is unchanged, including in personal mode); the new
point is stamped ``provenance_mixed`` so recall tiers it unattributed. The
credential keys are kept, so an admin revert by credential (§5.19) still finds
the merged point.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.workers.memory_agent import _merge_cluster


def _settings():
    return MagicMock(
        QDRANT_COLLECTION="firekeep_memories", LLM_BASE_URL="http://llm/v1",
        LLM_MODEL="m", LLM_API_KEY="", EMBEDDING_MODEL="e",
    )


def _member(pid, text, *, member, credential=True, created="2026-10-01T00:00:00+00:00",
            confirmed=0):
    nested = {"memory_type": "episodic"}
    if credential:
        nested.update({"credential_id": f"cred-{member}", "runtime_id": "",
                       "runtime_label": "x", "delegated_by_credential_id": None})
    payload = {
        "text": text, "status": "active", "domain": "ops", "tags": [],
        "confirmed_count": confirmed, "contradicted_count": 0,
        "created_at": created, "timestamp": created,
        "workspace_id": "ws-1", "namespace": "default", "member_id": member,
        "source": "action_log", "metadata": nested,
    }
    return {"id": pid, "text": text, "domain": "ops", "tags": [],
            "confirmed_count": confirmed, "contradicted_count": 0,
            "vector": [0.1, 0.2, 0.3], "payload": payload}


def _merge(cluster):
    client = MagicMock()
    llm = MagicMock()
    llm.raise_for_status = MagicMock()
    llm.json.return_value = {"choices": [{"message": {"content": json.dumps(
        {"text": "deploy with update.sh after backing up the store", "domain": "ops",
         "tags": []})}}]}
    embed = MagicMock()
    embed.raise_for_status = MagicMock()
    embed.json.return_value = {"data": [{"embedding": [0.5, 0.5, 0.5]}]}
    with patch("app.workers.memory_agent.httpx.post", side_effect=[llm, embed]), \
            patch("app.workers.memory_agent._embed_sync", return_value=[0.5, 0.5, 0.5]):
        result = _merge_cluster(client, cluster, _settings())
    assert result is not None
    return client.upsert.call_args.kwargs["points"][0].payload


def test_cross_member_merge_is_stamped_mixed_and_keeps_the_credential():
    payload = _merge([
        _member("a", "deploy with update.sh", member="member-alice", confirmed=1),
        _member("b", "deploy with update.sh; skip the backup", member="member-bob"),
    ])
    assert payload["metadata"]["provenance_mixed"] is True
    assert payload["metadata"]["credential_id"] == "cred-member-alice"


def test_merge_with_a_legacy_member_is_stamped_mixed():
    payload = _merge([
        _member("a", "deploy with update.sh", member="member-alice", confirmed=1),
        _member("b", "deploy using update.sh", member="member-alice", credential=False),
    ])
    assert payload["metadata"]["provenance_mixed"] is True


def test_single_author_merge_is_not_stamped():
    payload = _merge([
        _member("a", "deploy with update.sh", member="member-alice", confirmed=1),
        _member("b", "deploy using update.sh", member="member-alice"),
    ])
    assert "provenance_mixed" not in payload["metadata"]


def test_the_stamp_survives_a_further_merge():
    first = _member("a", "deploy with update.sh", member="member-alice", confirmed=1)
    first["payload"]["metadata"]["provenance_mixed"] = True
    payload = _merge([first, _member("b", "deploy using update.sh", member="member-alice")])
    assert payload["metadata"]["provenance_mixed"] is True
