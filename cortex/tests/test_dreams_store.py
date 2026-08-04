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
