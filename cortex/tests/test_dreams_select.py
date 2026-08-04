from datetime import datetime, timedelta, timezone

import pytest

from app.dreams import select as sel

NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _payload(**kw):
    base = {
        "status": "active",
        "source": "action_log",
        "memory_type": "episodic",
        "confirmed_count": 0,
        "timestamp": (NOW - timedelta(days=10)).isoformat(),
        "workspace_id": "ws1",
        "namespace": "default",
        "project": "firekeep",
    }
    base.update(kw)
    return base


def _ok(p):
    return sel.is_candidate(p, now=NOW, min_age_days=2, owm_floor=0.35, owm_prior_n=5)


def test_plain_episodic_memory_is_a_candidate():
    assert _ok(_payload())


def test_missing_memory_type_is_treated_as_episodic():
    p = _payload()
    del p["memory_type"]
    assert _ok(p)


@pytest.mark.parametrize("field,value", [
    ("status", "superseded"),
    ("status", "archived"),
    ("source", "corpus"),
    ("source", "dream"),
    ("memory_type", "reference"),
    ("memory_type", "skill"),
    ("confirmed_count", 1),
])
def test_excluded_shapes(field, value):
    assert not _ok(_payload(**{field: value}))


def test_too_fresh_is_excluded():
    assert not _ok(_payload(timestamp=(NOW - timedelta(hours=6)).isoformat()))


def test_unparseable_timestamp_is_excluded_not_crashing():
    assert not _ok(_payload(timestamp="not-a-date"))


def test_owm_condemned_memory_is_excluded():
    assert not _ok(_payload(owm_efficacy=0.2, owm_n=8))


def test_owm_proven_memory_is_excluded_too():
    # A memory with a demonstrated track record keeps its own rank; consolidating
    # it would hand its position to a memory with no history.
    assert not _ok(_payload(owm_efficacy=0.8, owm_n=8))


def test_owm_low_n_is_not_excluded():
    assert _ok(_payload(owm_efficacy=0.2, owm_n=2))


def test_partition_key_normalises_none_to_empty_string():
    assert sel.partition_key(_payload(project=None)) == ("ws1", "default", "")


def _cand(i, vec, **kw):
    return sel.Candidate(id=f"m{i}", text=f"t{i}", vector=vec, payload=_payload(**kw))


def test_partition_never_mixes_workspaces():
    cands = [_cand(1, [1.0, 0.0]), _cand(2, [1.0, 0.0], workspace_id="ws2")]
    parts = sel.partition(cands)
    assert len(parts) == 2
    for members in parts.values():
        assert len({c.payload["workspace_id"] for c in members}) == 1


def test_cluster_groups_similar_and_drops_undersized():
    near = [_cand(i, [1.0, 0.02 * i]) for i in range(4)]
    lone = [_cand(9, [0.0, 1.0])]
    clusters = sel.cluster(near + lone, threshold=0.9, min_size=4)
    assert len(clusters) == 1
    assert {c.id for c in clusters[0]} == {"m0", "m1", "m2", "m3"}


def test_cluster_is_deterministic_regardless_of_input_order():
    a = [_cand(i, [1.0, 0.02 * i]) for i in range(5)]
    first = sel.cluster(a, threshold=0.9, min_size=4)
    second = sel.cluster(list(reversed(a)), threshold=0.9, min_size=4)
    assert [[c.id for c in cl] for cl in first] == [[c.id for c in cl] for cl in second]


def test_cluster_key_is_stable_and_order_independent():
    c = [_cand(1, [1.0, 0.0]), _cand(2, [1.0, 0.0])]
    assert sel.cluster_key(c) == sel.cluster_key(list(reversed(c)))


def test_select_clusters_never_spans_a_partition():
    # Identical vectors, different projects: must NOT cluster together.
    cands = [_cand(i, [1.0, 0.0]) for i in range(4)] + [
        _cand(i + 10, [1.0, 0.0], project="other") for i in range(4)
    ]
    clusters = sel.select_clusters(cands, threshold=0.9, min_size=4, max_clusters=10)
    assert len(clusters) == 2
    for cl in clusters:
        assert len({c.payload["project"] for c in cl}) == 1


def test_select_clusters_respects_max():
    cands = []
    for p in range(3):
        cands += [_cand(p * 10 + i, [1.0, 0.01 * i], project=f"p{p}") for i in range(4)]
    assert len(sel.select_clusters(cands, threshold=0.9, min_size=4, max_clusters=2)) == 2


def test_cosine_of_orthogonal_is_zero_and_zero_vector_is_safe():
    assert sel.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert sel.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
