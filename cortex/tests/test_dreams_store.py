import pytest

from app.dreams import store
from app.dreams.select import Candidate
from app.dreams.synthesize import Insight


class FakeVector:
    def __init__(self):
        self.points = {}

    async def upsert_point(self, point_id, text, payload):
        self.points[point_id] = {"text": text, "payload": payload}
        return point_id


def _members():
    return [
        Candidate(id=f"m{i}", text=f"e{i}", vector=[1.0], payload={
            "workspace_id": "ws1", "namespace": "default", "project": "firekeep",
            "member_id": "member-1", "agent_id": "a1", "domain": "infra",
        })
        for i in range(4)
    ]


def _insight():
    return Insight(content="durable lesson", memory_type="procedural", source_ids=["m0", "m1"])


def test_dream_id_is_deterministic_and_id_not_derived_from_text():
    a = store.dream_point_id("k1")
    assert a == store.dream_point_id("k1")
    assert a != store.dream_point_id("k2")


def test_profile_id_is_stable_per_member_and_workspace():
    assert store.profile_point_id("mem1", "ws1") == store.profile_point_id("mem1", "ws1")
    assert store.profile_point_id("mem1", "ws1") != store.profile_point_id("mem2", "ws1")
    assert store.profile_point_id("mem1", "ws1") != store.profile_point_id("mem1", "ws2")


def test_payload_puts_provenance_TOP_LEVEL():
    p = store.build_dream_payload(_insight(), _members(), cluster_key="k", run_id="r")
    for key in ("source", "dream_run_id", "dreamed_from", "memory_type", "status",
                "workspace_id", "namespace", "project", "member_id"):
        assert key in p, f"{key} must be top-level"
    assert p["source"] == "dream"
    assert p["dreamed_from"] == ["m0", "m1"]


def test_payload_memory_type_is_procedural_and_also_nested():
    p = store.build_dream_payload(_insight(), _members(), cluster_key="k", run_id="r")
    assert p["memory_type"] == "procedural"
    assert p["metadata"]["memory_type"] == "procedural"


def test_payload_never_marks_a_dream_reference():
    bad = Insight(content="c", memory_type="reference", source_ids=["m0"])
    p = store.build_dream_payload(bad, _members(), cluster_key="k", run_id="r")
    assert p["memory_type"] == "procedural"


def test_payload_inherits_partition_from_members():
    p = store.build_dream_payload(_insight(), _members(), cluster_key="k", run_id="r")
    assert (p["workspace_id"], p["namespace"], p["project"]) == ("ws1", "default", "firekeep")


def test_payload_refuses_heterogeneous_cluster():
    members = _members()
    members[0].payload["workspace_id"] = "ws2"
    with pytest.raises(ValueError, match="workspace"):
        store.build_dream_payload(_insight(), members, cluster_key="k", run_id="r")


def test_payload_starts_active_and_unconfirmed():
    p = store.build_dream_payload(_insight(), _members(), cluster_key="k", run_id="r")
    assert p["status"] == "active"
    assert p["confirmed_count"] == 0


@pytest.mark.asyncio
async def test_write_dream_is_idempotent_same_cluster_one_point():
    v = FakeVector()
    for _ in range(3):
        await store.write_dream(v, _insight(), _members(), cluster_key="k", run_id="r")
    assert len(v.points) == 1


def test_dream_point_id_default_index_matches_bare_cluster_key():
    """Fix-round review minor: dream_point_id(cluster_key, index=0) must
    reproduce the pre-fix (no-index) id exactly — every existing caller/test
    only ever passes one positional arg."""
    assert store.dream_point_id("k1") == store.dream_point_id("k1", 0)
    assert store.dream_point_id("k1") == store.dream_point_id("k1", index=0)


def test_dream_point_id_distinguishes_insight_index():
    a = store.dream_point_id("k1", index=0)
    b = store.dream_point_id("k1", index=1)
    assert a != b


@pytest.mark.asyncio
async def test_write_dream_index_writes_distinct_points_same_cluster_key_in_payload():
    """Fix-round review minor: multiple insights from ONE cluster must land
    as distinct points (via `index`), while the stored `dream_cluster_key`
    provenance always names the real cluster — never a synthetic "key:i"
    value, which would make the point unfindable by its true cluster key."""
    v = FakeVector()
    insight_a = Insight(content="lesson A", memory_type="procedural", source_ids=["m0"])
    insight_b = Insight(content="lesson B", memory_type="procedural", source_ids=["m1"])
    id_a = await store.write_dream(v, insight_a, _members(), cluster_key="k", run_id="r", index=0)
    id_b = await store.write_dream(v, insight_b, _members(), cluster_key="k", run_id="r", index=1)

    assert id_a != id_b
    assert len(v.points) == 2
    assert v.points[id_a]["payload"]["dream_cluster_key"] == "k"
    assert v.points[id_b]["payload"]["dream_cluster_key"] == "k"
    assert v.points[id_a]["text"] == "lesson A"
    assert v.points[id_b]["text"] == "lesson B"


# --- VectorClient.upsert_point error wrapping (final-review Minor 6) --------

@pytest.mark.asyncio
async def test_upsert_point_does_not_double_wrap_a_vector_store_error():
    """`upsert` has carried an `except VectorStoreError: raise` re-raise since
    SP0; `upsert_point` — the write path EVERY dream and profile goes through
    — did not. `_embed` raises VectorStoreError of its own (notably the
    context-length case, which the embed path has to report precisely because
    it is the one a caller can act on), and re-wrapping it produced
    "Failed to upsert point <id>: Failed to embed ...: <real cause>" —
    the diagnostic buried one level down in a background Celery task whose
    only other trace is a log line.
    """
    from unittest.mock import AsyncMock, MagicMock

    from app.config import get_settings
    from app.db.vector import VectorClient
    from app.exceptions import VectorStoreError

    client = VectorClient(get_settings())
    client._embed = AsyncMock(side_effect=VectorStoreError("Failed to embed: context length"))
    client._client = MagicMock()

    with pytest.raises(VectorStoreError) as exc:
        await client.upsert_point("pid", "text", {})

    assert str(exc.value) == "Failed to embed: context length"
    assert "Failed to upsert point" not in str(exc.value)


# --------------------------------------------------------------------------
# Sampling provenance. synthesize() sends at most
# DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS (5) of a cluster's members to the
# model — a prompt cap, not a change of what the dream is about. The stored
# point must therefore be able to say "summarised from 5 of 23", never imply
# it read 23.
# --------------------------------------------------------------------------


def test_payload_records_cluster_size_and_how_many_were_actually_read():
    """The honesty requirement. Twelve members in the cluster, five put in
    front of the model: both numbers are recorded, and they disagree, which is
    the whole point — a reader can tell a sampled dream from a complete one."""
    members = [
        Candidate(id=f"m{i}", text=f"e{i}", vector=[1.0], payload={
            "workspace_id": "ws1", "namespace": "default", "project": "firekeep",
            "member_id": "member-1", "agent_id": "a1", "domain": "infra",
        })
        for i in range(12)
    ]
    ins = Insight(content="lesson", memory_type="procedural",
                  source_ids=["m0", "m3"], sample_size=5)
    p = store.build_dream_payload(ins, members, cluster_key="k", run_id="r")

    assert p["dream_cluster_size"] == 12
    assert p["dream_sampled_count"] == 5
    # dreamed_from keeps its own, older meaning: what the model CITED. It was
    # already a subset of the cluster before sampling existed, so sampling does
    # not narrow it — and conflating it with either count would lose the
    # citation link.
    assert p["dreamed_from"] == ["m0", "m3"]


def test_an_unsampled_dream_reports_the_full_cluster_on_both_counts():
    """A cluster at or below the cap is sent whole, so the two numbers agree —
    "4 of 4". This is the shape every pre-cap dream had and must keep."""
    p = store.build_dream_payload(
        Insight(content="c", memory_type="procedural", source_ids=["m0"], sample_size=4),
        _members(), cluster_key="k", run_id="r",
    )
    assert p["dream_cluster_size"] == p["dream_sampled_count"] == 4


def test_an_insight_with_no_recorded_sample_size_reports_the_cluster_size():
    """sample_size==0 means "not recorded" (a hand-built Insight), not "the
    model saw nothing". Writing a literal 0 would claim a dream was
    synthesized from no episodes at all, which is strictly less true than the
    pre-sampling reality it is standing in for."""
    p = store.build_dream_payload(_insight(), _members(), cluster_key="k", run_id="r")
    assert p["dream_sampled_count"] == 4
    assert p["dream_cluster_size"] == 4


def test_no_derived_sampled_boolean_is_stored():
    """`sampled_count < cluster_size` is recomputable by any reader. A stored
    copy is a second source of truth that can disagree with the numbers as
    soon as one writer sets one field and not the other — the failure mode this
    package has already paid for elsewhere (memory_type in two places)."""
    p = store.build_dream_payload(_insight(), _members(), cluster_key="k", run_id="r")
    assert "dream_sampled" not in p
    assert "dreamed_sampled" not in p
