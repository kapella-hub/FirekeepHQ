from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import fakeredis
import httpx
import pytest

from app.dreams import task as dt

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _gate(**kw):
    args = dict(
        enabled=True,
        now=NOW,
        last_write_at=NOW - timedelta(minutes=45),
        idle_minutes=30,
        new_memories=100,
        min_new=25,
    )
    args.update(kw)
    return dt.should_run(**args)


def test_gate_opens_when_idle_and_work_exists():
    ok, reason = _gate()
    assert ok, reason


def test_gate_closed_when_disabled():
    ok, reason = _gate(enabled=False)
    assert not ok and "disabled" in reason


def test_gate_closed_while_recently_active():
    ok, reason = _gate(last_write_at=NOW - timedelta(minutes=2))
    assert not ok and "idle" in reason


def test_gate_closed_without_enough_new_memories():
    ok, reason = _gate(new_memories=3)
    assert not ok and "new" in reason


def test_gate_opens_when_never_written_before():
    ok, reason = _gate(last_write_at=None)
    assert ok, reason


def test_task_is_registered_on_beat_with_matching_name():
    from app.workers.sleep_cycle import celery_app

    name = "app.dreams.task.run_dream_tick"
    assert name in celery_app.tasks
    entry = celery_app.conf.beat_schedule["dream-tick"]
    assert entry["task"] == name
    # Fix-round review minor: this test previously couldn't catch the module
    # being removed from `include` — celery_app.tasks is populated because
    # THIS test module's own top-level `from app.dreams import task as dt`
    # already imported it directly, independent of sleep_cycle.py's
    # `include` list ever being consulted.
    assert "app.dreams.task" in celery_app.conf.include


def test_disabled_task_returns_status_without_building_clients(monkeypatch):
    monkeypatch.setattr(dt, "_build_clients", lambda: (_ for _ in ()).throw(
        AssertionError("must not build clients when disabled")))
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DREAM_ENABLED", "false")
    out = dt.run_dream_tick()
    assert out["status"] == "disabled"


# ---------------------------------------------------------------------------
# I5 — generation-backend reachability probe (pure predicate + probe tests)
# ---------------------------------------------------------------------------

def test_is_backend_unavailable_detects_connection_and_timeout_errors():
    assert dt._is_backend_unavailable(httpx.ConnectError("refused"))
    assert dt._is_backend_unavailable(httpx.ConnectTimeout("timed out"))


def test_is_backend_unavailable_detects_404():
    request = httpx.Request("GET", "http://x/models")
    response = httpx.Response(404, request=request)
    exc = httpx.HTTPStatusError("not found", request=request, response=response)
    assert dt._is_backend_unavailable(exc)


def test_is_backend_unavailable_false_for_a_reachable_backend_erroring():
    request = httpx.Request("GET", "http://x/models")
    response = httpx.Response(500, request=request)
    exc = httpx.HTTPStatusError("boom", request=request, response=response)
    assert not dt._is_backend_unavailable(exc)
    assert not dt._is_backend_unavailable(ValueError("unrelated"))


class _Settings:
    LLM_BASE_URL = "http://x/v1"


@pytest.mark.asyncio
async def test_generation_backend_available_true_on_2xx(monkeypatch):
    async def fake_get(self, url, **kw):
        return httpx.Response(200, request=httpx.Request("GET", url), json={"data": []})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert await dt._generation_backend_available(_Settings())


@pytest.mark.asyncio
async def test_generation_backend_available_false_on_connect_error(monkeypatch):
    async def fake_get(self, url, **kw):
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert not await dt._generation_backend_available(_Settings())


# ---------------------------------------------------------------------------
# run_one_unit end-to-end wiring — fakes standing in for Qdrant/Redis/LLM.
# Models: tests/test_dreams_profile.py and test_dreams_store.py's FakeVector,
# extended with the ._client.scroll(...) surface run_one_unit reads directly.
# ---------------------------------------------------------------------------

class _FakePoint:
    def __init__(self, id, payload, vector=None):
        self.id = id
        self.payload = payload
        self.vector = vector or [1.0, 0.0]


class _FakeScroll:
    """Single-page stand-in for AsyncQdrantClient.scroll: returns the full
    point list on every call and never sets a next offset. Both the
    vector-less activity scan and the with-vectors candidate scan read the
    SAME points at these test sizes (one page is enough either way)."""
    def __init__(self, points):
        self.points = points

    async def scroll(self, **kwargs):
        return self.points, None


class _FakeVector:
    def __init__(self, points):
        self._client = _FakeScroll(points)
        self.upserted: dict[str, dict] = {}

    async def upsert_point(self, point_id, text, payload):
        self.upserted[point_id] = {"text": text, "payload": payload}
        return point_id

    async def close(self):
        pass


def _dream_settings(**overrides):
    from app.config import get_settings

    base = dict(
        DREAM_ENABLED=True, DREAM_MIN_NEW_MEMORIES=1, DREAM_IDLE_MINUTES=1,
        DREAM_MIN_AGE_DAYS=2, DREAM_MIN_CLUSTER=4, DREAM_CLUSTER_THRESHOLD=0.9,
        DREAM_MAX_CLUSTERS_PER_RUN=5, DREAM_LOCK_TTL_SECONDS=60,
        DREAM_PROFILES_ENABLED=True,
    )
    base.update(overrides)
    return get_settings().model_copy(update=base)


def _candidate_payload(member_id, workspace_id, i, *, namespace="default", project="p"):
    return {
        "status": "active", "source": "action_log", "memory_type": "episodic",
        "confirmed_count": 0,
        "timestamp": (NOW - timedelta(days=10)).isoformat(),
        "workspace_id": workspace_id, "namespace": namespace, "project": project,
        "member_id": member_id,
        "text": f"memory {i} for {member_id} in {workspace_id}",
    }


@pytest.mark.asyncio
async def test_profile_grouping_respects_workspace_tenancy_boundary(monkeypatch):
    """C1 (CRITICAL): a member with memories in TWO different workspaces
    must produce two DISJOINT profile writes, one per workspace — never one
    profile whose synthesis input blends both, and never a scroll-order-
    dependent "which workspace wins" leaving stale duplicate points.
    Each group is below DREAM_MIN_CLUSTER (2 < 4), so no cluster forms and
    both ticks fall straight through to the profile branch."""
    points = (
        [_FakePoint(f"a{i}", _candidate_payload("mem1", "wsA", i)) for i in range(2)]
        + [_FakePoint(f"b{i}", _candidate_payload("mem1", "wsB", i)) for i in range(2)]
    )
    r = fakeredis.FakeStrictRedis()
    vector = _FakeVector(points)
    settings = _dream_settings()

    async def fake_build_clients():
        return r, vector, settings

    monkeypatch.setattr(dt, "_build_clients", fake_build_clients)
    monkeypatch.setattr(dt, "_generation_backend_available", AsyncMock(return_value=True))

    async def fake_synth_profile(member_id, memories, **kw):
        texts = sorted(m["text"] for m in memories)
        return f"PROFILE for {member_id}: " + " | ".join(texts)

    monkeypatch.setattr("app.dreams.profile.synthesize_profile", fake_synth_profile)

    out1 = await dt.run_one_unit()
    assert out1["status"] == "ok" and out1["unit"] == "profile"
    out2 = await dt.run_one_unit()
    assert out2["status"] == "ok" and out2["unit"] == "profile"

    assert len(vector.upserted) == 2, "each workspace must get its OWN point"
    records = list(vector.upserted.values())
    texts = [rec["text"] for rec in records]
    assert texts[0] != texts[1], "the two profiles must be disjoint, not one blended write"
    for text in texts:
        assert not ("wsA" in text and "wsB" in text), \
            "one profile's synthesis input must never mix both workspaces"
    workspaces = {rec["payload"]["workspace_id"] for rec in records}
    assert workspaces == {"wsA", "wsB"}


@pytest.mark.asyncio
async def test_profile_derives_namespace_and_project_from_group(monkeypatch):
    """I2: namespace/project must be read from the (now tenancy-homogeneous)
    candidate group, not hardcoded to "default"/None. project="finance" here
    would render as None if the old hardcoding were still in place."""
    points = [
        _FakePoint(f"c{i}", _candidate_payload(
            "mem2", "wsC", i, namespace="acme", project="finance"))
        for i in range(2)
    ]
    r = fakeredis.FakeStrictRedis()
    vector = _FakeVector(points)
    settings = _dream_settings()

    async def fake_build_clients():
        return r, vector, settings

    monkeypatch.setattr(dt, "_build_clients", fake_build_clients)
    monkeypatch.setattr(dt, "_generation_backend_available", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "app.dreams.profile.synthesize_profile", AsyncMock(return_value="a profile"))

    out = await dt.run_one_unit()
    assert out["status"] == "ok" and out["unit"] == "profile"
    assert len(vector.upserted) == 1
    payload = next(iter(vector.upserted.values()))["payload"]
    assert payload["namespace"] == "acme"
    assert payload["project"] == "finance"


@pytest.mark.asyncio
async def test_profile_group_missing_workspace_id_skips_synthesis_entirely(monkeypatch):
    """M5 (round 2): the workspace_id guard must run BEFORE synthesize_profile
    is awaited — a group that's always going to be discarded (no real
    workspace could ever match profile_point_id(member_id, "")) must not
    burn a full LLM call first. The original ordering awaited synthesis
    unconditionally and only checked workspace_id afterward."""
    points = [_FakePoint(f"m{i}", _candidate_payload("mem1", "", i)) for i in range(2)]
    r = fakeredis.FakeStrictRedis()
    vector = _FakeVector(points)
    settings = _dream_settings()

    async def fake_build_clients():
        return r, vector, settings

    monkeypatch.setattr(dt, "_build_clients", fake_build_clients)
    monkeypatch.setattr(dt, "_generation_backend_available", AsyncMock(return_value=True))
    synth_mock = AsyncMock(return_value="should never be produced")
    monkeypatch.setattr("app.dreams.profile.synthesize_profile", synth_mock)

    out = await dt.run_one_unit()
    assert out["status"] == "ok" and out["unit"] == "profile"
    assert out["written"] is False
    synth_mock.assert_not_awaited()
    assert not vector.upserted

    from app.dreams.state import DreamState

    # still marked done so the empty-workspace group isn't retried forever
    assert len(DreamState(r).done_set("profile")) == 1


@pytest.mark.asyncio
async def test_backend_unavailable_skips_unit_without_marking_anything_done(monkeypatch):
    """I5: when the generation backend is unreachable, the tick must not
    walk the backlog marking clusters/profiles done with zero insights, and
    must not stamp completion — there IS a backlog, it just couldn't be
    worked this tick."""
    points = [_FakePoint(f"m{i}", _candidate_payload("mem1", "ws1", i)) for i in range(4)]
    r = fakeredis.FakeStrictRedis()
    vector = _FakeVector(points)
    settings = _dream_settings()

    async def fake_build_clients():
        return r, vector, settings

    monkeypatch.setattr(dt, "_build_clients", fake_build_clients)
    monkeypatch.setattr(dt, "_generation_backend_available", AsyncMock(return_value=False))

    out = await dt.run_one_unit()
    assert out["status"] == "unavailable"

    from app.dreams.state import DreamState

    state = DreamState(r)
    assert state.done_set("cluster") == set()
    assert state.done_set("profile") == set()
    assert state.get_run().get("last_completed_at") is None
    assert not vector.upserted


# ---------------------------------------------------------------------------
# I2 + I3 — the persistent consolidated ledger: the spec's "not already
# consolidated" criterion, and the cure for cluster starvation.
# ---------------------------------------------------------------------------

def _cluster_points(project, cluster_index, *, dims, member_id="mem1", workspace_id="ws1"):
    """Four points forming exactly one cluster.

    Every member of cluster `cluster_index` carries the SAME one-hot vector,
    so within-cluster cosine is 1.0 (>= any threshold) and cross-cluster
    cosine is 0.0 — clusters that are unambiguous rather than
    threshold-sensitive, which is what lets these tests assert about WHICH
    clusters get selected without also testing the clustering maths.
    """
    vec = [0.0] * dims
    vec[cluster_index] = 1.0
    return [
        _FakePoint(
            f"{project}-c{cluster_index}-{i}",
            _candidate_payload(member_id, workspace_id, i, project=project),
            vector=list(vec),
        )
        for i in range(4)
    ]


async def _fake_synth(members, **kw):
    from app.dreams.synthesize import Insight

    return [Insight(content="an insight", memory_type="procedural",
                    source_ids=[m.id for m in members])]


async def _drive_run(max_ticks=25):
    """Tick until the run reports `complete`, collecting the cluster keys it
    spent its units on. Returns that list."""
    keys = []
    for _ in range(max_ticks):
        out = await dt.run_one_unit()
        if out["status"] == "complete":
            return keys
        assert out["status"] == "ok", out
        keys.append(out["cluster_key"])
    raise AssertionError(f"run did not complete within {max_ticks} ticks: {keys}")


def _starvation_fixture(monkeypatch, *, cap):
    """Two project buckets, two clusters each. `select_clusters` walks buckets
    in sorted key order, so bucket "alpha" is always reached first — with a
    cap of 2 the "beta" bucket is never in the returned list at all. That is
    the shape the live store has (a 297-memory project-less bucket ahead of
    every named-project bucket), just small enough to assert on.
    """
    dims = 4
    points = (
        _cluster_points("alpha", 0, dims=dims) + _cluster_points("alpha", 1, dims=dims)
        + _cluster_points("beta", 2, dims=dims) + _cluster_points("beta", 3, dims=dims)
    )
    r = fakeredis.FakeStrictRedis()
    vector = _FakeVector(points)
    # DREAM_MIN_NEW_MEMORIES=0 keeps the work-available gate open across the
    # run boundary: `new_memories` counts writes NEWER than last_completed_at,
    # and these fixtures' memories are all 10 days old, so run 2 would
    # otherwise be gated out by the very completion run 1 just stamped.
    settings = _dream_settings(
        DREAM_MAX_CLUSTERS_PER_RUN=cap, DREAM_MIN_NEW_MEMORIES=0,
        DREAM_PROFILES_ENABLED=False,
    )

    async def fake_build_clients():
        return r, vector, settings

    monkeypatch.setattr(dt, "_build_clients", fake_build_clients)
    monkeypatch.setattr(dt, "_generation_backend_available", AsyncMock(return_value=True))
    monkeypatch.setattr("app.dreams.synthesize.synthesize", _fake_synth)
    return r, vector, settings, points


@pytest.mark.asyncio
async def test_a_consolidated_memory_is_not_a_candidate_in_the_next_run(monkeypatch):
    """I2+I3 (a). The spec's §Candidate selection lists a "not already
    consolidated" criterion that had no implementation anywhere. A memory
    covered by a STORED dream must drop out of the candidate pool."""
    from app.dreams.state import DreamState

    r, vector, settings, _ = _starvation_fixture(monkeypatch, cap=2)

    out = await dt.run_one_unit()
    assert out["status"] == "ok" and out["unit"] == "cluster"
    consolidated = DreamState(r).consolidated_set()
    assert len(consolidated) == 4, "the whole cluster is consolidated, not just cited ids"

    still_candidates = {
        c.id for c in await dt._scroll_candidates(
            vector, settings, now=NOW, consolidated=consolidated,
        )
    }
    assert not (still_candidates & consolidated), \
        "a consolidated memory must never be offered as a candidate again"
    assert still_candidates, "the rest of the store must remain selectable"


@pytest.mark.asyncio
async def test_a_zero_insight_cluster_consolidates_nothing(monkeypatch):
    """The ledger records what was STORED, not what was attempted. A cluster
    the LLM could not synthesize is marked done for the run (so it is not
    retried every tick) but its members stay candidates — otherwise one bad
    synthesis would permanently retire four real memories."""
    from app.dreams.state import DreamState

    r, _vector, _settings, _ = _starvation_fixture(monkeypatch, cap=2)

    async def _no_insights(members, **kw):
        return []

    monkeypatch.setattr("app.dreams.synthesize.synthesize", _no_insights)

    out = await dt.run_one_unit()
    assert out["status"] == "ok" and out["insights"] == 0
    state = DreamState(r)
    assert state.consolidated_set() == set(), "nothing was stored, so nothing is consolidated"
    assert len(state.done_set("cluster")) == 1, "but it is done for THIS run"


@pytest.mark.asyncio
async def test_run_two_selects_different_clusters_than_run_one(monkeypatch):
    """I2+I3 (b) — THE starvation regression guard.

    Before the ledger, `select_clusters` returned the first
    DREAM_MAX_CLUSTERS_PER_RUN clusters in deterministic sorted-bucket order
    and `reset_progress()` wiped the per-run done-set at completion, so every
    run re-selected and re-synthesized the IDENTICAL prefix forever. Here the
    cap is 2 and "alpha" holds exactly 2 clusters: pre-fix, "beta" is never
    reached on ANY run, no matter how many times the pass is allowed to run.
    """
    from app.dreams.state import DreamState

    r, vector, _settings, _ = _starvation_fixture(monkeypatch, cap=2)

    run1 = await _drive_run()
    run2 = await _drive_run()

    assert len(run1) == 2, f"the cap must bound a run: {run1}"
    assert len(run2) == 2, f"the cap must bound a run: {run2}"
    assert not (set(run1) & set(run2)), \
        "run 2 re-selected a cluster run 1 already consolidated — starvation is back"

    # ...and the point of not starving: the second bucket, previously
    # unreachable at this cap, is actually consolidated.
    consolidated = DreamState(r).consolidated_set()
    assert {i for i in consolidated if i.startswith("alpha-")}, "alpha consolidated"
    assert {i for i in consolidated if i.startswith("beta-")}, \
        "the beta bucket was never reached — this is the exact live-store failure"
    assert len(consolidated) == 16, "every memory in both buckets is consolidated"

    # Four clusters, one insight each, one point each — no cluster synthesized twice.
    assert len(vector.upserted) == 4


@pytest.mark.asyncio
async def test_one_tick_does_at_most_one_unit_with_many_clusters_available(monkeypatch):
    """I6 (b) — the design spec's §Testing item, previously unwritten.

    The whole execution model rests on this: the worker is
    --concurrency=1 --pool=solo, so a tick that looped over available
    clusters would block every other periodic task (including the 60s
    agent-gateway sweeper) for as many LLM calls as there are clusters.
    Four clusters are available here; exactly one synthesis and one write may
    happen.
    """
    from app.dreams.select import cluster_key

    calls = []

    async def _counting_synth(members, **kw):
        calls.append(cluster_key(members))
        return await _fake_synth(members, **kw)

    _r, vector, _settings, _ = _starvation_fixture(monkeypatch, cap=20)
    monkeypatch.setattr("app.dreams.synthesize.synthesize", _counting_synth)

    out = await dt.run_one_unit()

    assert out["status"] == "ok" and out["unit"] == "cluster"
    assert len(calls) == 1, f"one tick synthesized {len(calls)} clusters"
    assert len(vector.upserted) == 1
    written = next(iter(vector.upserted.values()))
    assert written["payload"]["dream_cluster_key"] == out["cluster_key"] == calls[0]


# ---------------------------------------------------------------------------
# I5 — profile grouping scope must equal store.profile_point_id's scope.
# ---------------------------------------------------------------------------

def _multi_project_points(projects, *, member_id="mem1", workspace_id="ws1",
                          namespaces=None):
    """Two memories per project for one member in one workspace. Two is below
    DREAM_MIN_CLUSTER (4), so no cluster forms in any bucket and every tick
    falls straight through to the profile branch."""
    namespaces = namespaces or {}
    out = []
    for project in projects:
        for i in range(2):
            payload = _candidate_payload(
                member_id, workspace_id, i,
                project=project, namespace=namespaces.get(project, "default"),
            )
            # Per-project text, so "the profile was built from ALL of them"
            # is an assertion about content rather than a count of
            # indistinguishable strings.
            payload["text"] = f"memory {i} in project {project}"
            out.append(_FakePoint(f"{project}-{i}", payload))
    return out


def _profile_fixture(monkeypatch, points, *, synth=None):
    r = fakeredis.FakeStrictRedis()
    vector = _FakeVector(points)
    settings = _dream_settings()

    async def fake_build_clients():
        return r, vector, settings

    monkeypatch.setattr(dt, "_build_clients", fake_build_clients)
    monkeypatch.setattr(dt, "_generation_backend_available", AsyncMock(return_value=True))

    async def _default_synth(member_id, memories, **kw):
        return "PROFILE " + " | ".join(sorted(m["text"] for m in memories))

    monkeypatch.setattr("app.dreams.profile.synthesize_profile", synth or _default_synth)
    return r, vector, settings


@pytest.mark.asyncio
async def test_one_member_in_one_workspace_gets_one_profile_from_all_projects(monkeypatch):
    """I5. Grouping was (member_id, workspace_id, namespace, project) while
    store.profile_point_id encodes only (member_id, workspace_id) — so each
    of a member's project groups wrote to the SAME point, groups are
    processed largest-first, and the LAST (smallest) one won. The surviving
    profile was systematically the worst of the set, and every earlier group
    was a wasted LLM call. On the live store a member with buckets of
    297/140/63/25 ended up with the 25-memory profile.
    """
    calls = []

    async def _counting_synth(member_id, memories, **kw):
        calls.append(len(memories))
        return "PROFILE " + " | ".join(sorted(m["text"] for m in memories))

    points = _multi_project_points(["alpha", "beta", "gamma"])
    _r, vector, _settings = _profile_fixture(monkeypatch, points, synth=_counting_synth)

    out = await dt.run_one_unit()
    assert out["status"] == "ok" and out["unit"] == "profile"
    assert out["written"] is True

    assert len(vector.upserted) == 1, \
        f"one member in one workspace is ONE profile point, got {len(vector.upserted)}"
    assert calls == [6], f"one LLM call over all 6 memories, got calls of size {calls}"

    # The synthesis INPUT is what the defect corrupted: pre-fix the surviving
    # profile was built from one project's two memories. All three projects
    # must be represented in the text that was actually stored.
    text = next(iter(vector.upserted.values()))["text"]
    for project in ("alpha", "beta", "gamma"):
        assert f"project {project}" in text, f"{project} missing from the stored profile"

    # And the run is finished — no second profile group left to overwrite it.
    assert (await dt.run_one_unit())["status"] == "complete"


@pytest.mark.asyncio
async def test_profile_project_is_unset_when_the_group_does_not_agree(monkeypatch):
    """A profile spanning three projects cannot honestly claim any one of
    them. namespace is uniform here and IS derived; project is not and is
    left None rather than being stamped with whichever value came first."""
    points = _multi_project_points(["alpha", "beta", "gamma"])
    _r, vector, _settings = _profile_fixture(monkeypatch, points)

    out = await dt.run_one_unit()
    assert out["status"] == "ok" and out["unit"] == "profile"
    payload = next(iter(vector.upserted.values()))["payload"]
    assert payload["project"] is None, "non-uniform project must not be stamped"
    assert payload["namespace"] == "default", "uniform namespace is still derived"
    assert payload["workspace_id"] == "ws1"


@pytest.mark.asyncio
async def test_profile_namespace_is_unset_when_the_group_does_not_agree(monkeypatch):
    """The mirror case: same project throughout, namespaces disagree. Falls
    back to the "default" the payload builder uses, not to whichever
    namespace happened to be scrolled first."""
    points = _multi_project_points(
        ["alpha", "beta"], namespaces={"alpha": "acme", "beta": "globex"})
    for p in points:
        p.payload["project"] = "one-project"
    _r, vector, _settings = _profile_fixture(monkeypatch, points)

    out = await dt.run_one_unit()
    assert out["status"] == "ok" and out["unit"] == "profile"
    payload = next(iter(vector.upserted.values()))["payload"]
    assert payload["namespace"] == "default"
    assert payload["project"] == "one-project", "uniform project is still derived"


@pytest.mark.asyncio
async def test_lock_contention_returns_locked_without_touching_data(monkeypatch):
    r = fakeredis.FakeStrictRedis()
    r.set(dt.LOCK_KEY, "1", nx=True, ex=60)  # simulate another tick holding it
    points = [_FakePoint("m0", _candidate_payload("mem1", "ws1", 0))]
    vector = _FakeVector(points)
    settings = _dream_settings()

    async def fake_build_clients():
        return r, vector, settings

    monkeypatch.setattr(dt, "_build_clients", fake_build_clients)
    out = await dt.run_one_unit()
    assert out == {"status": "locked"}
    assert not vector.upserted
