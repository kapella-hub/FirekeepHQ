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


def test_select_clusters_partitions_once(monkeypatch):
    """Partitioning must happen exactly once, not once per partition."""
    cands = []
    for p in range(3):
        cands += [_cand(p * 10 + i, [1.0, 0.01 * i], project=f"p{p}") for i in range(4)]

    orig = sel.partition
    calls = []
    monkeypatch.setattr(sel, "partition", lambda c: (calls.append(1), orig(c))[1])

    sel.select_clusters(cands, threshold=0.9, min_size=4, max_clusters=10)
    assert len(calls) == 1, f"partition() called {len(calls)} times, expected 1"


def test_is_candidate_accepts_naive_now():
    """is_candidate must handle naive (tz-unaware) datetime without raising."""
    p = _payload()
    naive_now = datetime(2026, 8, 4)  # No tzinfo
    assert sel.is_candidate(p, now=naive_now, min_age_days=2, owm_floor=0.35, owm_prior_n=5)


def test_already_consolidated_memory_is_not_a_candidate():
    """The design spec's "not already consolidated" criterion, which had no
    implementation anywhere until final-review I2+I3. The ledger arrives as a
    plain set argument — is_candidate stays PURE and never learns what Redis
    is."""
    p = _payload()
    assert sel.is_candidate(
        p, now=NOW, min_age_days=2, owm_floor=0.35, owm_prior_n=5,
        memory_id="m7", consolidated={"m1", "m2"},
    )
    assert not sel.is_candidate(
        p, now=NOW, min_age_days=2, owm_floor=0.35, owm_prior_n=5,
        memory_id="m7", consolidated={"m1", "m7"},
    )


def test_consolidated_ledger_defaults_leave_every_existing_caller_unchanged():
    """Both new parameters default to "no ledger", so a caller that knows
    nothing about consolidation (every pre-existing one, and every other test
    in this file) keeps its exact previous meaning."""
    p = _payload()
    assert _ok(p)
    assert sel.is_candidate(p, now=NOW, min_age_days=2, owm_floor=0.35, owm_prior_n=5,
                            consolidated={"m7"})  # no memory_id -> no exclusion


def test_dream_profile_source_is_excluded_even_if_memory_type_is_forged():
    """Fix-round review I4: a profile payload (source="dream_profile") must
    never be selectable as a dreaming candidate, even in the adversarial case
    where its memory_type has been forced to "episodic" — is_candidate's own
    defence must not rest solely on memory_type, since
    profile.build_profile_payload's memory_type="reference" is a SEPARATE
    mechanism that already blocks this in practice."""
    p = _payload(source="dream_profile", memory_type="episodic")
    assert not _ok(p)


# --------------------------------------------------------------------------
# sample_cluster — the prompt cap (DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS)
#
# WHY it exists, measured on the live production store 2026-08-04 (VPS,
# qwen3:4b, 4 vCPU, native /api/chat, DREAM_SYNTH_TIMEOUT_SECONDS=45s), 526
# candidates forming 20 clusters sized [23,16,10,10,10,9,8,8,7,6,6,5]:
# an uncapped 23-member cluster (~15,000 chars) never completed, which is why
# the real tick wrote zero insights. Repeating the SAME capped prompt 3x each
# showed latency is variance-dominated, not size-dominated: cap 4 spanned
# 12.0-42.9s, cap 5 gave 29.8-35.8s, cap 6 gave 25.1-35.8s. Every cap in 4..6
# fits the 45s budget; the uncapped cluster never does. Capping also improved
# output (4 -> 1 insight, 6 -> 3 specific ones, uncapped -> 0), which is the
# finding that survives repetition. See config.py for the full table.
# --------------------------------------------------------------------------


def _c(i, vector):
    return sel.Candidate(id=f"m{i:02d}", text=f"e{i}", vector=list(vector), payload={})


def test_cluster_at_or_below_the_cap_is_returned_completely_unchanged():
    """The load-bearing no-op case. A cluster that already fits must not just
    contain the same members — it must be the SAME ORDER, so build_messages
    emits a byte-identical prompt and the cap cannot perturb the behaviour of
    the clusters that were already working (only 1 of 20 on the live store,
    but that one must not regress)."""
    members = [_c(i, [1.0, float(i)]) for i in range(4)]
    assert sel.sample_cluster(members, 5) == members
    assert sel.sample_cluster(members, 4) == members
    # Also true of the degenerate "no cap" setting.
    assert sel.sample_cluster(members, 0) == members
    assert sel.sample_cluster(members, -1) == members


def test_no_cap_setting_sends_the_whole_cluster():
    members = [_c(i, [1.0, 0.0]) for i in range(30)]
    assert len(sel.sample_cluster(members, 0)) == 30


def test_cap_is_honoured_and_does_not_mutate_the_input():
    members = [_c(i, [1.0, float(i) / 100.0]) for i in range(23)]
    before = list(members)
    got = sel.sample_cluster(members, 5)
    assert len(got) == 5
    assert members == before, "sample_cluster must not reorder or shorten its input"
    assert all(m in members for m in got)


def test_sample_is_deterministic_across_repeated_calls_and_input_order():
    """The whole module's reproducibility property. Same cluster, any input
    ordering, same sample — otherwise two runs of the same store could produce
    different dreams from the same cluster_key."""
    members = [_c(i, [1.0, float(i) / 10.0]) for i in range(12)]
    a = sel.sample_cluster(members, 5)
    b = sel.sample_cluster(members, 5)
    c = sel.sample_cluster(list(reversed(members)), 5)
    assert [m.id for m in a] == [m.id for m in b] == [m.id for m in c]


def test_sample_is_returned_in_id_order():
    """Prompt position is not a ranking signal — returning centrality order
    would imply to the model that index 0 matters more, which nothing here
    supports. id order also matches cluster_key's own convention."""
    members = [_c(i, [1.0, float(i) / 10.0]) for i in range(12)]
    got = sel.sample_cluster(members, 5)
    assert [m.id for m in got] == sorted(m.id for m in got)


def test_sample_picks_the_members_NEAREST_THE_CENTROID_not_the_first_k():
    """The centrality claim is a computed one, so it has to be provable.

    Nine members sit tightly around [1, 0]; two outliers sit far off it. The
    centroid is dominated by the tight group, so the outliers must be the ones
    dropped — even though one of them sorts FIRST by id and would therefore be
    kept by any arbitrary-but-stable ordering.
    """
    tight = [_c(i, [1.0, 0.01 * i]) for i in range(2, 11)]
    outliers = [_c(0, [0.0, 1.0]), _c(99, [-1.0, 0.2])]
    members = outliers[:1] + tight + outliers[1:]

    got = [m.id for m in sel.sample_cluster(members, 5)]

    assert "m00" not in got, "the far outlier sorting first by id must not be kept"
    assert "m99" not in got
    assert set(got) <= {m.id for m in tight}


def test_a_member_with_no_vector_sinks_but_is_not_excluded():
    """Scoring 0.0 is the honest answer for a member whose vector is unusable
    among usable ones — it sinks below every real match without needing a
    special case, and a cluster of ONLY such members can still be sampled (next
    test), because a cluster must always be able to produce a prompt."""
    members = [_c(i, [1.0, 0.0]) for i in range(1, 6)] + [_c(0, [])]
    got = [m.id for m in sel.sample_cluster(members, 3)]
    assert "m00" not in got


def test_unusable_vectors_degrade_to_stable_id_order_not_a_crash():
    """Missing or ragged vectors mean no centroid can be computed. The
    fallback is plain id order, which is arbitrary-but-stable and documented
    as exactly that rather than dressed up as representative."""
    ragged = [
        sel.Candidate(id="m03", text="c", vector=[1.0, 2.0], payload={}),
        sel.Candidate(id="m01", text="a", vector=[1.0], payload={}),
        sel.Candidate(id="m02", text="b", vector=[], payload={}),
        sel.Candidate(id="m04", text="d", vector=[3.0, 4.0, 5.0], payload={}),
    ]
    got = sel.sample_cluster(ragged, 2)
    assert [m.id for m in got] == ["m01", "m02"]

    novectors = [sel.Candidate(id=f"m{i}", text="x", vector=[], payload={}) for i in range(6)]
    assert len(sel.sample_cluster(novectors, 3)) == 3


def test_zero_centroid_degrades_instead_of_dividing_by_zero():
    """Antipodal members average to the zero vector, whose norm is 0. Cosine
    against it is undefined, so this must fall back rather than raise."""
    members = [
        _c(1, [1.0, 0.0]), _c(2, [-1.0, 0.0]),
        _c(3, [0.0, 1.0]), _c(4, [0.0, -1.0]),
    ]
    got = sel.sample_cluster(members, 2)
    assert [m.id for m in got] == ["m01", "m02"]


def test_centroid_is_the_component_wise_mean_and_refuses_ragged_input():
    assert sel.centroid([[1.0, 3.0], [3.0, 1.0]]) == [2.0, 2.0]
    assert sel.centroid([]) is None
    assert sel.centroid([[]]) is None
    assert sel.centroid([[1.0], [1.0, 2.0]]) is None
